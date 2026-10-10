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


class ProbabilityBot:
    """Карта вероятностей. Памяти между вызовами нет: знания каждый раз собираются из state.shots.

    Расстановка и поправка на края в стрельбе выбираются под соперника по его профилю
    из разведки (battleships/strategies/adaptation.py).
    """

    def __init__(self):
        self.rng = random.Random()
        self.book = ProfileBook()

    def plan(self, state: model.GameState) -> Plan:
        return choose_plan(state.rules.modifier, state.opponent_info, self.book.lookup(state.opponent_info))

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
        shooter = ProbabilityStrategy(core_rules(state.rules), self.rng, edge_bias=plan.edge_bias)
        for cell, result in state.shots:
            shooter.on_shot_result((cell.row, cell.column), result)
        answers = [r for r in state.bonus_results if r.get("bonus") == "DISTANCE" and r.get("distance") is not None]
        for answer in answers:
            asked = answer.get("requestedCell") or {}
            shooter.on_distance((asked.get("row", -1), asked.get("column", -1)), answer["distance"])
        shooter.distance_uses = self._distance_budget(state, len(answers))
        kind, (row, column) = shooter.next_action()
        if kind == "DISTANCE":
            return model.Action("USE_BONUS", cell=model.Cell(row, column), bonus="DISTANCE")
        return model.Action.shoot(model.Cell(row, column))

    def _distance_budget(self, state: model.GameState, used: int) -> int:
        """Сколько запросов DISTANCE стратегия вправе потратить сейчас. Базовый бот бонусы не тратит."""
        return 0

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

    max_distance_queries = 2  # третий запрос даёт 2–4 п. п. к победе и окупается только при K = 40
    # Сработавшая мина сжигает сопернику остаток хода и весь следующий. В «Трёх мушкетёрах» это около
    # четырёх промахов (+14 п. п. к победе) за 1 очко; в остальных режимах — один выстрел, в минус.
    mine_modifiers = frozenset({"THREE_MUSKETEERS"})
    max_mines = 3
    safe_lead = 0.55  # цела большая доля нашего флота — партия и так выиграна, рейтинг не тратим

    def __init__(self):
        super().__init__()
        for length in (1, 2, 3, 4):  # таблицы расстояний считаются десятки миллисекунд — не в свой ход
            placement_distances(10, 10, length)

    def choose_action(self, state: model.GameState, time_budget_ms: int) -> model.Action:
        mine = self._mine_cell(state)
        if mine is not None:
            return model.Action("USE_BONUS", cell=mine, bonus="MINE")
        return super().choose_action(state, time_budget_ms)

    def _mine_cell(self, state: model.GameState) -> model.Cell | None:
        """Клетка для очередной мины или None. Мины ставятся в первые же ходы: чем раньше, тем вероятнее сработают."""
        rules = state.rules
        if rules.modifier not in self.mine_modifiers or not state.ships or state.bonuses.get("MINE", 0) <= 0:
            return None
        sprung = sum(result == "MINE" for _, result in state.opponent_shots)
        if len(state.mines) + sprung >= self.max_mines or state.shots_left <= 5:
            return None
        own = {cell for ship in state.ships for cell in ship}
        # Рядом с кораблём мину не ставим: после потопления соперник в такие клетки не стреляет.
        near = {model.Cell(c.row + dr, c.column + dc) for c in own for dr in (-1, 0, 1) for dc in (-1, 0, 1)}
        shot = {cell for cell, _ in state.opponent_shots}
        free = [cell for cell in rules.cells() if cell not in near and cell not in shot and cell not in state.mines]
        if not free:
            return None
        profile = self.book.lookup(state.opponent_info) or {}
        heat = profile.get("openingCells") or {}

        def score(cell: model.Cell) -> tuple:
            # Сначала — куда этот соперник бьёт в дебюте; если данных нет — ближе к центру, где охотятся чаще.
            centre = min(cell.row, rules.rows - 1 - cell.row) + min(cell.column, rules.columns - 1 - cell.column)
            return heat.get(f"{cell.row},{cell.column}", 0), centre, self.rng.random()

        return max(free, key=score)

    def _distance_budget(self, state: model.GameState, used: int) -> int:
        if state.shots_left <= 1:  # запрос списывает выстрел из лимита: последний оставляем на выстрел
            return 0
        if state.ships and state.rules.modifier != "BIRD_BOX":
            fleet = sum(len(ship) for ship in state.ships)
            lost = len({cell for cell, result in state.opponent_shots if result in ("HIT", "SUNK")})
            if fleet and (fleet - lost) / fleet > self.safe_lead:
                return 0
        return max(0, min(state.bonuses.get("DISTANCE", 0), self.max_distance_queries - used))
