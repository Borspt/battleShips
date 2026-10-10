"""Стрельба по карте вероятностей: бьём в клетку, которую накрывает больше всего допустимых положений кораблей."""

from __future__ import annotations

from dataclasses import dataclass

from ..geometry import bits, neighbour_masks, placement_priors, placements, ring, to_cell
from ..rules import Cell
from .base import Strategy

# Во сколько раз положение, проходящее через подбитую клетку, весит больше обычного.
# Достаточно велико, чтобы добивание всегда шло раньше поиска.
HIT_WEIGHT = 10_000


@dataclass(frozen=True)
class EdgeBias:
    """Поправка для соперника, который ставит корабли вдоль краёв поля."""

    edge: float = 1.0  # вес клетки на краю для кораблей длиннее одной клетки
    corner: float = 1.0  # вес угловой клетки
    single_edge: float = 1.0  # вес крайней клетки для однопалубных


# Подобрано в симуляторе против расстановки встроенного бота HARD: крупные корабли вдоль
# краёв, однопалубные внутри. 51 выстрел до потопления против 62 без поправки.
HARD_BIAS = EdgeBias(edge=4.0, corner=12.0, single_edge=0.5)


class ProbabilityStrategy(Strategy):
    def __init__(self, rules, rng=None, edge_bias: EdgeBias | None = None, **kwargs):
        super().__init__(rules, rng, **kwargs)
        self.edge_bias = edge_bias
        self.size = rules.rows * rules.columns
        self.side, self.diagonal = neighbour_masks(rules.rows, rules.columns)
        self.remaining = list(rules.fleet)  # длины ещё не потопленных кораблей
        self.empty = 0  # промахи и клетки, где корабля быть не может
        self.hits = 0  # подбитые клетки ещё не потопленных кораблей
        self.sunk = 0  # клетки потопленных кораблей
        self.known_ship = 0  # необстрелянные клетки, где точно корабль (отбито щитом)

    def next_shot(self) -> Cell:
        open_cells = ((1 << self.size) - 1) & ~(self.empty | self.hits | self.sunk)
        certain = self.known_ship & open_cells
        if certain:
            return to_cell(next(bits(certain)), self.rules.columns)
        weights = self._weights()
        best = max(weights)
        if best > 0:
            choices = [index for index, weight in enumerate(weights) if weight == best]
        else:  # знания противоречат правилам — стреляем в любую необстрелянную клетку
            choices = list(bits(open_cells)) or [0]
        return to_cell(self.rng.choice(choices), self.rules.columns)

    def on_shot_result(self, cell: Cell, result: str) -> None:
        row, column = cell
        if not (0 <= row < self.rules.rows and 0 <= column < self.rules.columns):
            return
        index = row * self.rules.columns + column
        bit = 1 << index
        if result == "DEFENDED":
            self.known_ship |= bit
            if self.rules.no_touching:
                self.empty |= self.diagonal[index] & ~(self.hits | self.sunk)
            return
        self.known_ship &= ~bit
        if result in ("HIT", "SUNK"):
            if (self.hits | self.sunk) & bit:
                return
            self.hits |= bit
            if self.rules.no_touching:  # корабли прямые и не касаются: по диагонали от попадания пусто
                self.empty |= self.diagonal[index] & ~(self.hits | self.sunk)
            if result == "SUNK":
                self._sink(self._group(index))
        else:  # MISS, MINE и незнакомые результаты: клетка обстреляна, корабля в ней нет
            self.empty |= bit & ~(self.hits | self.sunk)
        if not self.rules.announces_sunk:
            self._infer_sunk()

    def _weights(self) -> list[int]:
        weights = [0] * self.size
        blocked = self.empty | self.sunk
        hits = self.hits
        for length in set(self.remaining):
            count = self.remaining.count(length)
            options = placements(self.rules.rows, self.rules.columns, length)
            priors = self._priors(length)
            for number, (mask, around, cells) in enumerate(options):
                if mask & blocked or (self.rules.no_touching and around & hits):
                    continue
                covered = (mask & hits).bit_count()
                if covered == length:  # корабль целиком подбит: новых клеток не даёт
                    continue
                weight = count * HIT_WEIGHT**covered
                if priors:
                    weight *= priors[number]
                for index in cells:
                    if not hits >> index & 1:
                        weights[index] += weight
        return weights

    def _priors(self, length: int):
        bias = self.edge_bias
        if bias is None:
            return None
        return placement_priors(self.rules.rows, self.rules.columns, length, bias.edge, bias.corner, bias.single_edge)

    def _group(self, index: int) -> int:
        """Связная по стороне группа подбитых клеток, в которую входит клетка."""
        group, frontier = 0, 1 << index
        while frontier:
            group |= frontier
            reached = 0
            for i in bits(frontier):
                reached |= self.side[i]
            frontier = reached & self.hits & ~group
        return group

    def _sink(self, group: int) -> None:
        self.hits &= ~group
        self.sunk |= group
        if self.rules.no_touching:
            self.empty |= ring(group, self.rules.rows, self.rules.columns) & ~self.sunk
        length = group.bit_count()
        if length in self.remaining:
            self.remaining.remove(length)

    def _infer_sunk(self) -> None:
        """Без сообщений «убил»: корабль потоплен, если расти ему некуда или длиннее кораблей не осталось."""
        if not self.rules.no_touching:
            return
        changed = True
        while changed and self.hits:
            changed = False
            pending = self.hits
            while pending:
                group = self._group(next(bits(pending)))
                pending &= ~group
                growth = 0
                for index in bits(group):
                    growth |= self.side[index]
                growth &= ~(group | self.empty | self.sunk)
                if not growth or group.bit_count() >= max(self.remaining, default=0):
                    self._sink(group)
                    changed = True
