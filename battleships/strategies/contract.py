"""Стратегии под контракт оболочки (docs/strategy-contract.md).

Запуск: python -m battleships --strategy battleships.strategies.contract:ProbabilityBot
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .. import model
from ..geometry import placement_distances
from ..placement import random_placement
from ..rules import Rules
from .adaptation import Plan, ProfileBook, choose_plan
from .probability import ProbabilityStrategy


def core_rules(rules: model.Rules) -> Rules:
    return Rules(
        rows=rules.rows,
        columns=rules.columns,
        fleet=rules.fleet,
        no_touching=rules.touch_rule == "NO_TOUCHING_EVEN_DIAGONALLY",
        hit_keeps_turn=rules.turn_after_hit_rule != "HIT_SPENDS_SHOT" and rules.modifier != "DUEL",
        shots_per_turn=rules.shots_per_turn,
        shot_limit=rules.shot_limit_per_player,
        modifier=rules.modifier,
    )


# Какой план выбран в каждой партии; читает python -m battleships.scouting --plans.
PLAN_LOG = Path(os.environ.get("BATTLESHIPS_PLAN_LOG", "logs/plans.jsonl"))
MINE_SPREAD = 0.15  # против чужих мин: выстрел — случайный из клеток с весом от 85% лучшего


class ProbabilityBot:
    """Карта вероятностей. Памяти между вызовами нет: знания каждый раз собираются из state.shots.

    Расстановка и поправка на края в стрельбе выбираются под соперника по его профилю
    из разведки (battleships/strategies/adaptation.py).
    """

    def __init__(self):
        self.rng = random.Random()
        self.book = ProfileBook()
        # Выстрелы, на которые ещё не пришёл ответ: клетка → сколько ходов мы его ждём. При задержках сети
        # платформа просит следующий ход раньше, чем отдаёт результат прошлого; без этой памяти бот стрелял
        # в ту же клетку повторно (в «Спидране» 100 выстрелов по 42 клеткам вместо 50).
        self._pending: dict[model.Cell, int] = {}
        self._seen_shots = 0

    def plan(self, state: model.GameState) -> Plan:
        typical = self.book.typical_early() if state.rules.modifier == "HUNGER_GAMES" else None
        return choose_plan(state.rules.modifier, state.opponent_info, self.book.lookup(state.opponent_info), typical)

    def place_ships(self, rules: model.Rules) -> list[list[model.Cell]]:
        """Расстановка, когда соперник неизвестен."""
        return self._place(rules, random_placement)

    def place_ships_for_match(self, state: model.GameState) -> list[list[model.Cell]]:
        plan = self.plan(state)
        self._record(state, plan, placed=True)
        return self._place(state.rules, plan.placement)

    def choose_action(self, state: model.GameState, time_budget_ms: int) -> model.Action:
        plan = self.plan(state)
        if state.ships is None and not state.shots:  # «Спидран»: расстановки нет, партия начинается с выстрела
            self._record(state, plan, placed=False)
        shooter = ProbabilityStrategy(core_rules(state.rules), self.rng, edge_bias=plan.edge_bias,
                                      spread=self._spread(state), sunk_checks=True, cell_prior=plan.cell_prior,
                                      single_prior=plan.single_prior,
                                      single_per_quadrant=plan.single_per_quadrant)
        for cell, result in state.shots:
            shooter.on_shot_result((cell.row, cell.column), result)
        answers = [r for r in state.bonus_results if r.get("bonus") == "DISTANCE" and r.get("distance") is not None]
        for answer in answers:
            asked = answer.get("requestedCell") or {}
            shooter.on_distance((asked.get("row", -1), asked.get("column", -1)), answer["distance"])
        shooter.distance_uses = self._distance_budget(state, len(answers))
        self._block_pending(state, shooter)
        kind, (row, column) = shooter.next_action()
        if kind == "DISTANCE":
            return model.Action("USE_BONUS", cell=model.Cell(row, column), bonus="DISTANCE")
        self._pending[model.Cell(row, column)] = 0
        return model.Action.shoot(model.Cell(row, column))

    PENDING_TURNS = 6  # дольше ответ не ждём: выстрел мог не дойти, клетку снова можно обстрелять

    def _block_pending(self, state: model.GameState, shooter: ProbabilityStrategy) -> None:
        """Клетки, по которым выстрел уже отправлен, а результат не пришёл, в этот ход не выбираем."""
        if len(state.shots) < self._seen_shots:  # началась новая партия
            self._pending.clear()
        self._seen_shots = len(state.shots)
        answered = {cell for cell, _ in state.shots}
        columns = state.rules.columns
        for cell in list(self._pending):
            self._pending[cell] += 1
            if cell in answered or self._pending[cell] > self.PENDING_TURNS:
                del self._pending[cell]
            elif 0 <= cell.row < state.rules.rows and 0 <= cell.column < columns:
                shooter.empty |= (1 << cell.row * columns + cell.column) & ~(shooter.hits | shooter.sunk)

    def _distance_budget(self, state: model.GameState, used: int) -> int:
        """Сколько запросов DISTANCE стратегия вправе потратить сейчас. Базовый бот бонусы не тратит."""
        return 0

    @staticmethod
    def _spread(state: model.GameState) -> float:
        """Пока у соперника есть мины, на которые мы ещё не наступили, не даём предсказать наш выстрел."""
        mines = sum(b.get("uses", 0) for b in (state.opponent or {}).get("bonuses") or [] if b.get("bonus") == "MINE")
        stepped = sum(result == "MINE" for _, result in state.shots)
        return MINE_SPREAD if mines > stepped else 0.0

    def _place(self, rules: model.Rules, placement) -> list[list[model.Cell]]:
        ships = placement(core_rules(rules), self.rng)
        return [[model.Cell(row, column) for row, column in ship] for ship in ships]

    def _record(self, state: model.GameState, plan: Plan, placed: bool) -> None:
        """Строка о выбранном плане. Сбой записи не должен стоить хода."""
        opponent = state.opponent_info
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "strategy": type(self).__name__,
            "opponent": opponent.key,
            "opponentRating": opponent.rating,
            "modifier": state.rules.modifier,
            "placement": plan.placement.__name__ if placed else None,
            "edgeBias": asdict(plan.edge_bias) if plan.edge_bias else None,
            "cellPrior": plan.cell_prior is not None,
            "reason": plan.reason,
        }
        try:
            PLAN_LOG.parent.mkdir(parents=True, exist_ok=True)
            with PLAN_LOG.open("a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


class ProbabilityBonusBot(ProbabilityBot):
    """ProbabilityBot, который тратит бонусы: DISTANCE в концовке и MINE в «Трёх мушкетёрах».

    Запуск: --strategy battleships.strategies.contract:ProbabilityBonusBot --allow-bonuses,
    боту в «Кабинете команды» должны быть выставлены использования DISTANCE и MINE.
    Без флага --allow-bonuses оболочка отклонит бонус и подставит запасной выстрел.
    """

    # DISTANCE возвращён решением Бориса 10.10 в 19:17 (с 17:58 был выключен): им в концовке играют три из
    # пяти верхних ботов. Цена с 17:30 — 2 очка за запрос; по оценке это около нуля в рейтинге.
    distance_enabled = True
    max_distance_queries = 2  # в «Шрёдингере» три: там запрос ещё и отвечает, потоплен ли корабль
    # Сработавшая мина сжигает сопернику остаток хода и весь следующий. В «Трёх мушкетёрах» это около
    # четырёх промахов (+14 п. п. к победе) за 1 очко; в остальных режимах — один выстрел, в минус.
    mine_modifiers = frozenset({"THREE_MUSKETEERS"})
    max_mines = 3
    # Мина стоит 1 очко и окупается, если сработает чаще чем в 35% партий. Половина мин «в дебютную
    # клетку соперника» не срабатывала, поэтому ставим только туда, куда этот соперник стрелял
    # в большинстве своих последних партий.
    mine_min_frequency = 0.7
    mine_min_matches = 8
    # Соперник намного слабее нас: победа и так почти ничего не даёт, бонусы на него не тратим.
    own_name = os.environ.get("BATTLESHIPS_BOT_NAME", "Нагибатор228")
    weak_gap = 150
    # Мы впереди на столько попаданий — партия и так выиграна, рейтинг не тратим. Мерить потерями своего
    # флота нельзя: быстрый соперник топил нас раньше, чем порог срабатывал, а в выигранных партиях мы платили.
    # В рейтинговой серии все запросы при перевесе в 5 попаданий ушли в партиях, выигранных с запасом 7–9.
    safe_lead = 5

    def __init__(self):
        super().__init__()
        for length in (1, 2, 3, 4):  # таблицы расстояний считаются десятки миллисекунд — не в свой ход
            placement_distances(10, 10, length)

    def choose_action(self, state: model.GameState, time_budget_ms: int) -> model.Action:
        mine = self._mine_cell(state)
        if mine is not None:
            return model.Action("USE_BONUS", cell=mine, bonus="MINE")
        return super().choose_action(state, time_budget_ms)

    def _weak_opponent(self, state: model.GameState) -> bool:
        rating, own = state.opponent_info.rating, self.book.rating_of(self.own_name)
        return rating is not None and own is not None and own - rating >= self.weak_gap

    def _mine_cell(self, state: model.GameState) -> model.Cell | None:
        """Клетка для очередной мины или None. Мины ставятся в первые же ходы: чем раньше, тем вероятнее сработают."""
        rules = state.rules
        if rules.modifier not in self.mine_modifiers or not state.ships or state.bonuses.get("MINE", 0) <= 0:
            return None
        sprung = sum(result == "MINE" for _, result in state.opponent_shots)
        if len(state.mines) + sprung >= self.max_mines or state.shots_left <= 5 or self._weak_opponent(state):
            return None
        profile = self.book.lookup(state.opponent_info) or {}
        habits = next((profile[section] for section in ("recentVsUs", "recent")
                       if (profile.get(section) or {}).get("matches", 0) >= self.mine_min_matches), None)
        if not habits:
            return None
        own = {cell for ship in state.ships for cell in ship}
        # Рядом с кораблём мину не ставим: после потопления соперник в такие клетки не стреляет.
        near = {model.Cell(c.row + dr, c.column + dc) for c in own for dr in (-1, 0, 1) for dc in (-1, 0, 1)}
        shot = {cell for cell, _ in state.opponent_shots}
        frequency = habits.get("shotCells") or {}
        free = [cell for cell in rules.cells() if cell not in near and cell not in shot and cell not in state.mines
                and frequency.get(f"{cell.row},{cell.column}", 0) >= self.mine_min_frequency]
        if not free:
            return None
        return max(free, key=lambda cell: (frequency[f"{cell.row},{cell.column}"], self.rng.random()))

    def _distance_budget(self, state: model.GameState, used: int) -> int:
        if not self.distance_enabled or state.shots_left <= 1:  # запрос списывает выстрел из лимита
            return 0
        if state.rules.modifier == "HUNGER_GAMES":  # выстрелов мало, а запрос стоит выстрела: считаются попадания
            return 0
        if self._weak_opponent(state):
            return 0
        if state.ships and state.rules.modifier != "BIRD_BOX":
            scored = len({cell for cell, result in state.shots if result in ("HIT", "SUNK")})
            lost = len({cell for cell, result in state.opponent_shots if result in ("HIT", "SUNK")})
            if scored - lost >= self.safe_lead:
                return 0
        limit = 3 if state.rules.modifier == "SCHRODINGER_SHIP" else self.max_distance_queries
        return max(0, min(state.bonuses.get("DISTANCE", 0), limit - used))
