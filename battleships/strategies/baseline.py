"""Эталонный бот: случайные выстрелы, после попадания добивает по соседним клеткам."""

from __future__ import annotations

from ..rules import Cell
from .base import Strategy


class BaselineStrategy(Strategy):
    def __init__(self, rules, rng=None):
        super().__init__(rules, rng)
        self.untried = [(row, column) for row in range(rules.rows) for column in range(rules.columns)]
        self.rng.shuffle(self.untried)
        self.targets: list[Cell] = []

    def next_shot(self) -> Cell:
        untried = set(self.untried)
        while self.targets:
            cell = self.targets.pop()
            if cell in untried:
                return cell
        return self.untried[-1] if self.untried else (0, 0)

    def on_shot_result(self, cell: Cell, result: str) -> None:
        if result == "DEFENDED":  # клетка осталась необстрелянной, в ней корабль
            self.targets.append(cell)
            return
        if cell in self.untried:
            self.untried.remove(cell)
        if result == "SUNK":
            self.targets.clear()
        elif result == "HIT":
            row, column = cell
            around = [(row - 1, column), (row + 1, column), (row, column - 1), (row, column + 1)]
            self.rng.shuffle(around)
            self.targets += around
