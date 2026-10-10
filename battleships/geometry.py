"""Геометрия поля на битовых масках: клетка (row, column) — бит row * columns + column."""

from __future__ import annotations

from functools import lru_cache

from .rules import Cell


@lru_cache(maxsize=None)
def neighbour_masks(rows: int, columns: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Для каждой клетки: маска соседей по стороне и маска соседей по диагонали."""
    side, diagonal = [], []
    for row in range(rows):
        for column in range(columns):
            masks = [0, 0]
            for d_row in (-1, 0, 1):
                for d_column in (-1, 0, 1):
                    r, c = row + d_row, column + d_column
                    if (d_row or d_column) and 0 <= r < rows and 0 <= c < columns:
                        masks[d_row != 0 and d_column != 0] |= 1 << (r * columns + c)
            side.append(masks[0])
            diagonal.append(masks[1])
    return tuple(side), tuple(diagonal)


def ring(mask: int, rows: int, columns: int) -> int:
    """Клетки, касающиеся маски стороной или углом, без самой маски."""
    side, diagonal = neighbour_masks(rows, columns)
    result = 0
    for index in bits(mask):
        result |= side[index] | diagonal[index]
    return result & ~mask


@lru_cache(maxsize=None)
def placements(rows: int, columns: int, length: int) -> tuple[tuple[int, int, tuple[int, ...]], ...]:
    """Все положения корабля данной длины: (маска, маска кольца вокруг, индексы клеток)."""
    result = []
    directions = ((0, 1),) if length == 1 else ((0, 1), (1, 0))
    for d_row, d_column in directions:
        for row in range(rows - d_row * (length - 1)):
            for column in range(columns - d_column * (length - 1)):
                cells = tuple((row + d_row * k) * columns + column + d_column * k for k in range(length))
                mask = sum(1 << index for index in cells)
                result.append((mask, ring(mask, rows, columns), cells))
    return tuple(result)


def border_mask(rows: int, columns: int) -> int:
    """Клетки крайних строк и столбцов."""
    return sum(
        1 << (row * columns + column)
        for row in range(rows)
        for column in range(columns)
        if row in (0, rows - 1) or column in (0, columns - 1)
    )


def along_border(cells: tuple[int, ...], rows: int, columns: int) -> bool:
    """Лежит ли корабль целиком в одной крайней строке или одном крайнем столбце."""
    cell_rows = {index // columns for index in cells}
    cell_columns = {index % columns for index in cells}
    return cell_rows <= {0} or cell_rows <= {rows - 1} or cell_columns <= {0} or cell_columns <= {columns - 1}


@lru_cache(maxsize=None)
def placement_priors(rows: int, columns: int, length: int, edge: float, corner: float, single_edge: float):
    """Априорный вес каждого положения из placements(): произведение весов его клеток.

    Клетка внутри поля весит 1, на краю — edge, в углу — corner; для однопалубных
    вместо этого действует single_edge на любой крайней клетке.
    """
    border = border_mask(rows, columns)
    corners = {0, columns - 1, (rows - 1) * columns, rows * columns - 1}
    result = []
    for mask, _, cells in placements(rows, columns, length):
        if length == 1:
            result.append(single_edge if mask & border else 1.0)
            continue
        weight = 1.0
        for index in cells:
            if index in corners:
                weight *= corner
            elif border >> index & 1:
                weight *= edge
        result.append(weight)
    return tuple(result)


@lru_cache(maxsize=None)
def placement_distances(rows: int, columns: int, length: int) -> tuple[tuple[int, ...], ...]:
    """Для каждого положения из placements(): манхэттенское расстояние от каждой клетки поля до корабля."""
    result = []
    for _, _, cells in placements(rows, columns, length):
        ship = [divmod(index, columns) for index in cells]
        result.append(tuple(
            min(abs(row - r) + abs(column - c) for r, c in ship)
            for row in range(rows) for column in range(columns)
        ))
    return tuple(result)


def bits(mask: int):
    """Индексы единичных битов маски по возрастанию."""
    while mask:
        low = mask & -mask
        yield low.bit_length() - 1
        mask ^= low


def to_cell(index: int, columns: int) -> Cell:
    return divmod(index, columns)
