"""«Голодные игры»: стрельба, которая по ходу партии сама решает, прижат ли флот соперника к краям.

Профили отстают: лучшие боты в этом режиме против нас ставили у края 44–63% клеток длинных кораблей,
а профиль по их последним партиям показывал 30–41%. Ошибка в обе стороны дорогая (симулятор, 20 выстрелов):
при доле 0,30 поправка на края отнимает 2,8 попадания, при 0,60 её отсутствие — 3,2.

Поэтому держим две гипотезы — «флот где угодно» и «флот у краёв» — и после каждого поискового выстрела
пересчитываем, какая вероятнее: промах в клетке, где гипотеза ждала корабль, снижает её вес.
Стреляем по смеси карт. В симуляторе смесь не бывает худшей из двух: при доле 0,30 — 7,3 попадания
против 8,4 / 5,6, при 0,45 — 7,5 против 6,4 / 7,9, при 0,60 — 7,9 против 5,9 / 9,1, при 0,75 — 8,5–9,4 против 4,7 / 11,2.
"""

from __future__ import annotations

from dataclasses import replace

from ..geometry import to_cell
from .adaptation import MEDIUM_BIAS
from .probability import EdgeBias, ProbabilityStrategy

EDGE = replace(MEDIUM_BIAS, single_edge=1.0)  # длинные корабли у края; однопалубные — как придётся
PRIOR_EDGE = 0.4  # профиль ничего не говорит: края чуть менее вероятны, как у большинства ботов
PRIOR_EDGE_BY_PROFILE = 0.7  # профиль соперника уже говорит «у края»


class EdgeMixture:
    def __init__(self, rules, rng, edge_bias: EdgeBias | None = None, spread: float = 0.0):
        prior = PRIOR_EDGE_BY_PROFILE if edge_bias else PRIOR_EDGE
        self.neutral = ProbabilityStrategy(rules, rng, spread=spread)
        self.edge = ProbabilityStrategy(rules, rng, edge_bias=edge_bias or EDGE, spread=spread)
        self.p_edge = prior
        self.rng = rng
        self.columns = rules.columns

    def _maps(self):
        a, b = self.neutral, self.edge
        return (a._endgame_weights(a._endgame_configs()) or a._weights(),
                b._endgame_weights(b._endgame_configs()) or b._weights())

    def on_shot_result(self, cell, result: str) -> None:
        index = cell[0] * self.columns + cell[1]
        if not self.neutral.hits and 0 <= index < self.neutral.size and result in ("MISS", "HIT", "SUNK"):
            # Поисковый выстрел: вероятность корабля в клетке по каждой гипотезе — её доля в карте, умноженная
            # на число оставшихся клеток флота. Во время добивания вес гипотез не меняем: там всё решает попадание.
            neutral, edge = self._maps()
            left = sum(self.neutral.remaining)
            p_neutral = min(0.99, neutral[index] / (sum(neutral) or 1.0) * left)
            p_edge = min(0.99, edge[index] / (sum(edge) or 1.0) * left)
            if result == "MISS":
                p_neutral, p_edge = 1 - p_neutral, 1 - p_edge
            total = self.p_edge * p_edge + (1 - self.p_edge) * p_neutral
            if total > 0:
                self.p_edge = min(0.99, max(0.01, self.p_edge * p_edge / total))
        self.neutral.on_shot_result(cell, result)
        self.edge.on_shot_result(cell, result)

    def next_shot(self):
        if self.neutral.hits or self.neutral.known_ship:
            return self.neutral.next_shot()  # добивание и клетки под щитом — одинаково при обеих гипотезах
        neutral, edge = self._maps()
        total_neutral, total_edge = sum(neutral) or 1.0, sum(edge) or 1.0
        mixed = [self.p_edge * e / total_edge + (1 - self.p_edge) * n / total_neutral for n, e in zip(neutral, edge)]
        best = max(mixed)
        if best <= 0:
            return self.neutral.next_shot()
        return to_cell(self.rng.choice([i for i, w in enumerate(mixed) if w >= best * (1 - 1e-9)]), self.columns)
