"""AchievementBot: ProbabilityBot с бонусами, которые окупаются в рейтинге, и ачивками по запросу.

Опирается на DOCUMENTS/«Бонусы - когда применять и окупается ли цена.md», «Ачивки - стратегия сбора.md»
и таблицу модификаторов. Правила каждой партии читаются из rules в MATCH_STARTED/MATCH_SNAPSHOT.

Цена бонуса списывается с нас при любом исходе и уходит сопернику. При K = 20 бонус ценой C окупается,
только если поднимает шанс победы на C/K (5 п. п. за очко). Отсюда политика по умолчанию — та же, что у
лидеров таблицы (реплеи 954 рейтинговых партий 10.10, 15:30–16:10):
  - DISTANCE ×2 в концовке (≤ 2 корабля у соперника), в том числе в «Шрёдингере». Экономит ~5 промахов
    на флот за 2 очка. У тройки лидеров — 1,8 запроса за партию на ~17-м попадании.
  - MINE — только в «Трёх мушкетёрах» (там подрыв сжигает многовыстрельный ход), не больше двух.
  - SHIELD, SALVO, SONAR — никогда: лидеры их не берут, в наших партиях они стоили −6…−9 очков.

Ачивки включаются явно: ACHIEVER_FOR=SHIELD,MINE,SALVO,BLITZ (через запятую). Тогда, как раньше:
  SHIELD «Щит и меч» — щит, когда у нашего корабля цела одна клетка;
  MINE   «Сапёр» — три мины в первые ходы в любом режиме со своим флотом;
  SALVO  «Залп-убийца» — залп, когда у соперника ровно 2 корабля и недобитых нет;
  BLITZ  «Блицкриг» — DISTANCE ×3 в концовке, даже при уверенном лидерстве.
ACHIEVER_SKIP=DISTANCE,MINE,... запрещает бонус совсем. Ставки «Казино» не делаются.

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


def _env_set(name: str) -> set[str]:
    return {x.strip().upper() for x in os.environ.get(name, "").split(",") if x.strip()}


class AchievementBot(ProbabilityBonusBot):
    # Значения для рейтинга; ачивки из ACHIEVER_FOR переопределяют их в __init__.
    max_distance_queries = 2  # третий запрос экономит меньше промаха — не окупает очко
    max_mines = 2
    mine_modifiers = frozenset({"THREE_MUSKETEERS"})

    def __init__(self):
        super().__init__()
        self.skip = _env_set("ACHIEVER_SKIP")
        self.hunt = _env_set("ACHIEVER_FOR")
        if "BLITZ" in self.hunt:  # «Блицкриг»: DISTANCE ×3, даже когда уверенно ведём
            self.max_distance_queries, self.safe_lead = 3, 1.01
        if "MINE" in self.hunt:  # «Сапёр»: три мины в любом режиме со своим флотом
            self.max_mines = 3

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

    def _hunting(self, state: model.GameState, bonus: str) -> bool:
        """Бонус, который тратится только ради ачивки: включён в ACHIEVER_FOR и есть в запасе."""
        return bonus in self.hunt and self._available(state, bonus)

    # ---- «Щит и меч» ------------------------------------------------------------------------------

    def _shield(self, state: model.GameState) -> model.Action | None:
        if (not self._hunting(state, "SHIELD") or state.shield_active or not state.ships
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
        if "MINE" not in self.hunt:  # для рейтинга — родительское правило: только «Три мушкетёра»
            return super()._mine_cell(state)
        # «Сапёр»: та же логика выбора клетки, но в любом режиме со своим флотом.
        saved = self.mine_modifiers
        self.mine_modifiers = frozenset({state.rules.modifier})
        try:
            return super()._mine_cell(state)
        finally:
            self.mine_modifiers = saved

    # ---- «Залп-убийца» ----------------------------------------------------------------------------

    def _salvo(self, state: model.GameState) -> model.Action | None:
        rules = state.rules
        if not self._hunting(state, "SALVO") or rules.modifier == "SCHRODINGER_SHIP":
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
