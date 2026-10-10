"""AchievementBot: ProbabilityBot, который тратит бонусы на ачивки и перестраивается под правила часа.

Опирается на DOCUMENTS/«Ачивки - стратегия сбора.md», «Бонусы - когда применять…» и таблицу модификаторов.
Правила каждой партии читаются из rules в MATCH_STARTED/MATCH_SNAPSHOT, ничего не зашито под конкретный час.

Порядок в свой ход (первое подходящее):
  1. SHIELD  «Щит и меч»: у нашего корабля цела одна клетка, щита нет.
  2. MINE    «Сапёр» (≥2 подрыва за партию): три мины в первые ходы, подальше от своего флота.
  3. SALVO   «Залп-убийца» (≥2 потопления за залп): у соперника осталось ровно 2 корабля, недобитых нет.
  4. DISTANCE в концовке — «Блицкриг» (≤45 выстрелов) и «Снайпер»; лимит 3.
  5. Выстрел по карте вероятностей (общая основа ProbabilityBot).

Каждый бонус стоит рейтинга и уменьшает «Высоту». Модуль отключается переменной окружения
ACHIEVER_SKIP=SHIELD,MINE,SALVO,DISTANCE (через запятую) — например, когда ачивка уже получена.
Ставки «Казино» не делаются: оболочка их пропускает.

Запуск: python -m battleships --strategy battleships.strategies.achiever:AchievementBot --allow-bonuses --mode RANKING
"""

from __future__ import annotations

import os

from .. import model
from .contract import ProbabilityBonusBot

# Где бонус бесполезен или запрещён. OLD_SCHOOL: бонусы выключены. SPEEDRUN: своего флота и выстрелов
# соперника нет. BIRD_BOX: выстрелов соперника не видно, мина может быть отклонена.
# HUNGER_GAMES: 40 выстрелов, каждый бонус кроме SALVO съедает выстрел — только стрельба.
NO_BONUSES = frozenset({"OLD_SCHOOL", "HUNGER_GAMES"})
NO_OWN_SIDE = frozenset({"SPEEDRUN", "BIRD_BOX"})


def _components(cells: set[model.Cell]) -> list[set[model.Cell]]:
    """Связные (по сторонам) группы клеток."""
    left, groups = set(cells), []
    while left:
        start = left.pop()
        group, pending = {start}, [start]
        while pending:
            cur = pending.pop()
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                other = model.Cell(cur.row + dr, cur.column + dc)
                if other in left:
                    left.discard(other)
                    group.add(other)
                    pending.append(other)
        groups.append(group)
    return groups


class AchievementBot(ProbabilityBonusBot):
    max_distance_queries = 3  # «Блицкриг»: DISTANCE ×3 в концовке поднимает шанс с 8% до 29%
    safe_lead = 1.01  # ради «Блицкрига» запросы в концовке тратим, даже когда уверенно ведём
    max_mines = 3
    mine_modifiers = frozenset()  # переопределено в _mine_cell: мины во всех режимах со своим флотом

    def __init__(self):
        super().__init__()
        self.skip = {x.strip().upper() for x in os.environ.get("ACHIEVER_SKIP", "").split(",") if x.strip()}

    # ---- выбор действия -------------------------------------------------------------------------

    def choose_action(self, state: model.GameState, time_budget_ms: int) -> model.Action:
        modifier = state.rules.modifier
        if modifier not in NO_BONUSES and state.shots_left > 1:
            for pick in (self._shield, self._mine, self._salvo):
                action = pick(state)
                if action is not None:
                    return action
        # DISTANCE и выстрел — из ProbabilityBot (через _distance_budget ниже).
        return super(ProbabilityBonusBot, self).choose_action(state, time_budget_ms)

    def _available(self, state: model.GameState, bonus: str) -> bool:
        return bonus not in self.skip and state.bonuses.get(bonus, 0) > 0

    # ---- «Щит и меч» ------------------------------------------------------------------------------

    def _shield(self, state: model.GameState) -> model.Action | None:
        if (not self._available(state, "SHIELD") or state.shield_active or not state.ships
                or state.rules.modifier in NO_OWN_SIDE):
            return None
        hit = {cell for cell, result in state.opponent_shots if result in ("HIT", "SUNK")}
        for ship in state.ships:
            alive = [c for c in ship if c not in hit]
            if len(ship) > 1 and len(alive) == 1:
                return model.Action("USE_BONUS", bonus="SHIELD")
        return None

    # ---- «Сапёр» ----------------------------------------------------------------------------------

    def _mine(self, state: model.GameState) -> model.Action | None:
        if not self._available(state, "MINE") or state.rules.modifier in NO_OWN_SIDE:
            return None
        cell = self._mine_cell(state)
        return model.Action("USE_BONUS", cell=cell, bonus="MINE") if cell is not None else None

    def _mine_cell(self, state: model.GameState) -> model.Cell | None:
        # Родительская логика выбора клетки, но без ограничения «только в Трёх мушкетёрах».
        saved = self.mine_modifiers
        self.mine_modifiers = frozenset({state.rules.modifier})
        try:
            return super()._mine_cell(state)
        finally:
            self.mine_modifiers = saved

    # ---- «Залп-убийца» ----------------------------------------------------------------------------

    def _salvo(self, state: model.GameState) -> model.Action | None:
        rules = state.rules
        if not self._available(state, "SALVO") or rules.modifier == "SCHRODINGER_SHIP":
            return None  # без SUNK потопления в залпе не посчитать
        # Залп возвращает полный ход. В «Трёх мушкетёрах» выгоднее всего, когда в ходе остался 1 выстрел.
        if rules.shots_per_turn > 1 and state.shots_left_in_turn > 1:
            return None
        if state.opponent_bonuses and state.opponent_bonuses[-1] in ("SHIELD", "MINE"):
            return None  # щит или мина соперника оборвут залп
        observed = state.observations()
        hits = {c for c, r in observed.items() if r in ("HIT", "SUNK")}
        groups = _components(hits)
        sunk = [g for g in groups if any(observed.get(c) == "SUNK" for c in g)]
        open_hits = [g for g in groups if g not in sunk]
        remaining = len(rules.fleet) - len(sunk)
        if remaining == 2 and not open_hits:
            return model.Action("USE_BONUS", bonus="SALVO")
        return None

    # ---- DISTANCE в концовке ----------------------------------------------------------------------

    def _distance_budget(self, state: model.GameState, used: int) -> int:
        if "DISTANCE" in self.skip or state.rules.modifier in NO_BONUSES:
            return 0
        return super()._distance_budget(state, used)
