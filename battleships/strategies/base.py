"""Контракт стратегии: его вызывает и симулятор, и обёртка над протоколом платформы."""

from __future__ import annotations

import random

from typing import Callable

from ..placement import Ship, random_placement
from ..rules import Cell, Rules


class Strategy:
    """Один экземпляр на одну партию. Результаты — значения ShotResult платформы."""

    def __init__(self, rules: Rules, rng: random.Random | None = None,
                 placement: Callable[[Rules, random.Random], list[Ship]] = random_placement):
        self.rules = rules
        self.rng = rng or random.Random()
        self.placement = placement

    def place_ships(self) -> list[Ship]:
        """Расстановка для PLACE. В «Спидране» не вызывается."""
        return self.placement(self.rules, self.rng)

    def next_shot(self) -> Cell:
        """Клетка для SHOOT. Обязана вернуть клетку на поле и не бросать исключений."""
        raise NotImplementedError

    def next_action(self) -> tuple[str, Cell]:
        """("SHOOT", клетка) или ("DISTANCE", клетка запроса) — для стратегий, умеющих тратить бонус."""
        return "SHOOT", self.next_shot()

    def on_distance(self, cell: Cell, distance: int) -> None:
        """Ответ на запрос DISTANCE."""

    def on_shot_result(self, cell: Cell, result: str) -> None:
        """Результат своего выстрела: MISS, HIT, SUNK, DEFENDED или MINE."""

    def on_opponent_shot(self, cell: Cell, result: str) -> None:
        """Выстрел соперника по своему полю. В «Птичьем коробе» не вызывается."""
