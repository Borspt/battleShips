"""Расстановка флота: случайная генерация и проверка по правилам платформы."""

from __future__ import annotations

import random

from .geometry import along_border, border_mask, placements, to_cell
from .rules import Cell, Rules

Ship = list[Cell]


def random_placement(rules: Rules, rng: random.Random) -> list[Ship]:
    """Случайная допустимая расстановка: каждый корабль равновероятно среди свободных положений."""
    for _ in range(1000):
        forbidden = 0
        ships: list[Ship] = []
        for length in rules.fleet:
            options = [p for p in placements(rules.rows, rules.columns, length) if not p[0] & forbidden]
            if not options:
                break
            mask, around, cells = rng.choice(options)
            forbidden |= mask | (around if rules.no_touching else 0)
            ships.append([to_cell(index, rules.columns) for index in cells])
        else:
            return ships
    raise ValueError("не удалось расставить флот: он не помещается на поле")


def edge_placement(rules: Rules, rng: random.Random) -> list[Ship]:
    """Корабли длиннее одной клетки — вдоль краёв поля, однопалубные — внутри.

    Так расставляет встроенный бот HARD. Если на краях или внутри места нет, корабль встаёт где получится.
    """
    border = border_mask(rules.rows, rules.columns)
    for _ in range(1000):
        forbidden = 0
        ships: list[Ship] = []
        for length in rules.fleet:
            free = [p for p in placements(rules.rows, rules.columns, length) if not p[0] & forbidden]
            if length == 1:
                preferred = [p for p in free if not p[0] & border]
            else:
                preferred = [p for p in free if along_border(p[2], rules.rows, rules.columns)]
            if not free:
                break
            mask, around, cells = rng.choice(preferred or free)
            forbidden |= mask | (around if rules.no_touching else 0)
            ships.append([to_cell(index, rules.columns) for index in cells])
        else:
            return ships
    raise ValueError("не удалось расставить флот: он не помещается на поле")


def interior_placement(rules: Rules, rng: random.Random) -> list[Ship]:
    """Все корабли — не на крайних строках и столбцах: против стрелка, который начинает с краёв."""
    border = border_mask(rules.rows, rules.columns)
    for _ in range(1000):
        forbidden = 0
        ships: list[Ship] = []
        for length in rules.fleet:
            free = [p for p in placements(rules.rows, rules.columns, length) if not p[0] & forbidden]
            if not free:
                break
            mask, around, cells = rng.choice([p for p in free if not p[0] & border] or free)
            forbidden |= mask | (around if rules.no_touching else 0)
            ships.append([to_cell(index, rules.columns) for index in cells])
        else:
            return ships
    raise ValueError("не удалось расставить флот: он не помещается на поле")


def evasive_placement(rules: Rules, rng: random.Random, danger: dict[Cell, float],
                      candidates: int = 240, keep: int = 24) -> list[Ship]:
    """Расстановка подальше от клеток, куда соперник бьёт первыми выстрелами; для партий с коротким лимитом.

    danger — доля партий, в которых клетка попадала в первые выстрелы соперника. Цена расстановки —
    ожидаемые потери: найденный корабль добивают целиком, поэтому длина умножается на шанс, что его
    заденут. Берём случайную из лучших, чтобы расстановку нельзя было выучить.
    """
    def cost(ships: list[Ship]) -> float:
        total = 0.0
        for ship in ships:
            missed = 1.0
            for cell in ship:
                missed *= 1.0 - min(0.95, danger.get(cell, 0.0))
            total += len(ship) * (1.0 - missed)
        return total

    makers = (random_placement, random_placement, edge_placement, interior_placement)
    pool = [makers[number % len(makers)](rules, rng) for number in range(candidates)]
    pool.sort(key=cost)
    return rng.choice(pool[:keep])


PLACEMENTS = {"random": random_placement, "edges": edge_placement, "interior": interior_placement}


def validate_placement(rules: Rules, ships: list[Ship]) -> str | None:
    """Возвращает причину отказа в терминах платформы (RejectionReason) или None."""
    occupied: set[Cell] = set()
    for ship in ships:
        cells = sorted(ship)
        if not cells or len(set(cells)) != len(cells):
            return "SHIP_NOT_STRAIGHT_LINE"
        rows = {row for row, _ in cells}
        columns = {column for _, column in cells}
        line = sorted(columns) if len(rows) == 1 else sorted(rows) if len(columns) == 1 else None
        if line is None or line != list(range(line[0], line[0] + len(cells))):
            return "SHIP_NOT_STRAIGHT_LINE"
        if any(not (0 <= row < rules.rows and 0 <= column < rules.columns) for row, column in cells):
            return "SHIP_OUT_OF_BOARD"
    if sorted((len(ship) for ship in ships), reverse=True) != sorted(rules.fleet, reverse=True):
        return "FLEET_MISMATCH"
    for ship in ships:
        if occupied & set(ship):
            return "SHIPS_OVERLAP"
        occupied |= set(ship)
    if rules.no_touching:
        owner = {cell: number for number, ship in enumerate(ships) for cell in ship}
        for (row, column), number in owner.items():
            for d_row in (-1, 0, 1):
                for d_column in (-1, 0, 1):
                    if owner.get((row + d_row, column + d_column), number) != number:
                        return "SHIPS_TOUCH"
    return None
