"""Стрельба по карте вероятностей: бьём в клетку, которую накрывает больше всего допустимых положений кораблей."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..geometry import bits, neighbour_masks, placement_distances, placement_priors, placements, ring, to_cell
from ..rules import Cell
from .base import Strategy

# Во сколько раз положение, проходящее через подбитую клетку, весит больше обычного.
# Достаточно велико, чтобы добивание всегда шло раньше поиска.
HIT_WEIGHT = 10_000

# Концовка: когда кораблей осталось не больше ENDGAME_SHIPS, перебираем все их расстановки,
# совместимые с известным, и стреляем по точной доле расстановок, занимающих клетку.
ENDGAME_SHIPS = 2
ENDGAME_NODES = 40_000  # предел перебора; сверх него играем по обычной карте
QUERY_SAMPLE = 300  # по скольким расстановкам оцениваем, какой запрос DISTANCE информативнее
MIN_QUERY_BITS = 0.5  # запрос, ответ на который несёт меньше информации, не стоит своей цены


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
    def __init__(self, rules, rng=None, edge_bias: EdgeBias | None = None, distance_uses: int = 0, **kwargs):
        super().__init__(rules, rng, **kwargs)
        self.distance_uses = distance_uses  # сколько запросов DISTANCE можно потратить
        self.distances: list[tuple[int, int]] = []  # ответы DISTANCE: (индекс клетки, расстояние)
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
        weights = self._endgame_weights(self._endgame_configs()) or self._weights()
        best = max(weights)
        if best > 0:
            choices = [index for index, weight in enumerate(weights) if weight == best]
        else:  # знания противоречат правилам — стреляем в любую необстрелянную клетку
            choices = list(bits(open_cells)) or [0]
        return to_cell(self.rng.choice(choices), self.rules.columns)

    def next_action(self) -> tuple[str, Cell]:
        """("SHOOT", клетка) или ("DISTANCE", клетка запроса). Запрос — только в концовке без недобитых."""
        if self.distance_uses > 0 and not self.hits and not self.known_ship and self.rules.announces_sunk:
            configs = self._endgame_configs()
            if configs and len(configs) > 1:
                index, information = self._best_query(configs)
                if information >= MIN_QUERY_BITS:
                    return "DISTANCE", to_cell(index, self.rules.columns)
        return "SHOOT", self.next_shot()

    def on_distance(self, cell: Cell, distance: int) -> None:
        """Ответ DISTANCE: расстояние от клетки до ближайшей клетки непотопленного корабля."""
        row, column = cell
        self.distance_uses = max(0, self.distance_uses - 1)
        if 0 <= row < self.rules.rows and 0 <= column < self.rules.columns and distance is not None:
            self.distances.append((row * self.rules.columns + column, int(distance)))

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

    def _endgame_configs(self) -> list[tuple[int, float, tuple]] | None:
        """Все расстановки оставшихся кораблей, совместимые с известным: (маска, вес, положения).

        None — кораблей ещё много, перебор слишком велик или знания противоречивы.
        """
        if not self.remaining or len(self.remaining) > ENDGAME_SHIPS:
            return None
        rows, columns = self.rules.rows, self.rules.columns
        blocked, hits, touching = self.empty | self.sunk, self.hits, not self.rules.no_touching
        # Ответ DISTANCE мог относиться к кораблю, который с тех пор потоплен: если его клетка лежит
        # ровно на этом расстоянии, требовать такую же клетку от оставшихся кораблей нельзя.
        checks = []
        for query, distance in self.distances:
            row, column = divmod(query, columns)
            explained = any(abs(row - i // columns) + abs(column - i % columns) == distance for i in bits(self.sunk))
            checks.append((query, distance, explained))

        lengths = sorted(self.remaining, reverse=True)
        options = {}
        for length in set(lengths):
            distances, priors = placement_distances(rows, columns, length), self._priors(length)
            options[length] = [
                (mask, around, number, priors[number] if priors else 1.0)
                for number, (mask, around, _) in enumerate(placements(rows, columns, length))
                if not mask & blocked
                and (touching or not around & hits)
                and (mask & hits != mask or not self.rules.announces_sunk)
                and all(distances[number][query] >= distance for query, distance, _ in checks)
            ]

        configs, nodes = [], 0

        def extend(position: int, occupied: int, forbidden: int, weight: float, chosen: tuple) -> bool:
            nonlocal nodes
            if position == len(lengths):
                if hits & ~occupied:  # все недобитые попадания должны принадлежать оставшимся кораблям
                    return True
                for query, distance, explained in checks:
                    if not explained and min(placement_distances(rows, columns, length)[number][query]
                                             for length, number in chosen) != distance:
                        return True
                configs.append((occupied, weight, chosen))
                return True
            length = lengths[position]
            same = position > 0 and lengths[position - 1] == length  # одинаковые корабли — без перестановок
            for mask, around, number, prior in options[length]:
                if mask & forbidden or (same and number <= chosen[-1][1]):
                    continue
                nodes += 1
                if nodes > ENDGAME_NODES:
                    return False
                if not extend(position + 1, occupied | mask, forbidden | mask | (0 if touching else around),
                              weight * prior, chosen + ((length, number),)):
                    return False
            return True

        return configs if extend(0, 0, 0, 1.0, ()) and configs else None

    def _endgame_weights(self, configs) -> list[float] | None:
        if not configs:
            return None
        weights = [0.0] * self.size
        for mask, weight, _ in configs:
            for index in bits(mask & ~self.hits):
                weights[index] += weight
        return weights

    def _best_query(self, configs) -> tuple[int, float]:
        """Клетка, ответ DISTANCE в которой сильнее всего сокращает число расстановок, и его энтропия в битах."""
        rows, columns = self.rules.rows, self.rules.columns
        sample = configs if len(configs) <= QUERY_SAMPLE else self.rng.sample(configs, QUERY_SAMPLE)
        tables = [[placement_distances(rows, columns, length)[number] for length, number in chosen] for _, _, chosen in sample]
        total = sum(weight for _, weight, _ in sample)
        best_index, best_information = 0, -1.0
        for query in range(self.size):
            answers: dict[int, float] = {}
            for (_, weight, _), table in zip(sample, tables):
                distance = min(distances[query] for distances in table)
                answers[distance] = answers.get(distance, 0.0) + weight
            information = -sum(w / total * math.log2(w / total) for w in answers.values())
            if information > best_information:
                best_index, best_information = query, information
        return best_index, best_information

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
