"""Офлайн-симулятор партий по правилам платформы. Бонусы не моделируются."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

from .placement import Ship, edge_placement, validate_placement
from .rules import Cell, Rules
from .strategies import Strategy

StrategyFactory = Callable[[Rules, random.Random], Strategy]


class Board:
    """Поле защищающегося: принимает выстрелы и отвечает, как платформа."""

    def __init__(self, ships: list[Ship]):
        self.owner = {cell: number for number, ship in enumerate(ships) for cell in ship}
        self.afloat = [len(ship) for ship in ships]
        self.shot: set[Cell] = set()
        self.hits = 0

    def receive(self, cell: Cell) -> str:
        if cell in self.shot or cell not in self.owner:
            self.shot.add(cell)
            return "MISS"
        self.shot.add(cell)
        self.hits += 1
        number = self.owner[cell]
        self.afloat[number] -= 1
        return "HIT" if self.afloat[number] else "SUNK"

    def distance(self, cell: Cell) -> int | None:
        """Бонус DISTANCE: манхэттенское расстояние до ближайшей клетки непотопленного корабля."""
        alive = [c for c, number in self.owner.items() if self.afloat[number]]
        return min((abs(cell[0] - row) + abs(cell[1] - column) for row, column in alive), default=None)

    @property
    def destroyed(self) -> bool:
        return not any(self.afloat)


@dataclass
class SoloResult:
    shots: int  # сделано выстрелов
    hits: int
    destroyed: bool  # потоплен ли флот в пределах лимита выстрелов
    queries: int = 0  # применено DISTANCE; каждое списывает выстрел из лимита, но не тратит ход


@dataclass
class MatchResult:
    winner: int | None  # 0 или 1, None — ничья
    reason: str  # значение MatchEndReason платформы
    shots: tuple[int, int]
    hits: tuple[int, int]


def _shoot(rules: Rules, shooter: Strategy, board: Board, budget: int) -> tuple[str, Cell, int]:
    """Один выстрел; перед ним стратегия может потратить запросы DISTANCE, пока в лимите есть запас."""
    queries = 0
    kind, cell = shooter.next_action()
    while kind == "DISTANCE" and budget - queries > 1:
        shooter.on_distance(cell, board.distance(cell))
        queries += 1
        kind, cell = shooter.next_action()
    if kind != "SHOOT":
        cell = shooter.next_shot()
    result = board.receive(cell)
    reported = "HIT" if result == "SUNK" and not rules.announces_sunk else result
    shooter.on_shot_result(cell, reported)
    return reported, cell, queries


def play_solo(rules: Rules, shooter: Strategy, ships: list[Ship]) -> SoloResult:
    """Стрельба по одной расстановке без соперника: метрика качества стрельбы и модель «Спидрана»."""
    board = Board(ships)
    shots = queries = 0
    while shots + queries < rules.shot_limit and not board.destroyed:
        queries += _shoot(rules, shooter, board, rules.shot_limit - shots - queries)[2]
        shots += 1
    return SoloResult(shots, board.hits, board.destroyed, queries)


def play_match(rules: Rules, first: StrategyFactory, second: StrategyFactory, rng: random.Random) -> MatchResult:
    """Партия двух стратегий. Кто ходит первым, выбирается случайно, как на платформе."""
    players = [first(rules, random.Random(rng.random())), second(rules, random.Random(rng.random()))]

    if not rules.places_fleet:
        ships = edge_placement(rules, rng)  # платформа расставляет флот, как встроенный HARD
        solo = [play_solo(rules, player, ships) for player in players]
        score = [(not s.destroyed, s.shots, -s.hits) for s in solo]  # меньше — лучше
        winner = None if score[0] == score[1] else int(score[1] < score[0])
        return MatchResult(winner, "SPEEDRUN_FINISHED", (solo[0].shots, solo[1].shots), (solo[0].hits, solo[1].hits))

    fleets = [player.place_ships() for player in players]
    valid = [validate_placement(rules, fleet) is None for fleet in fleets]
    if not all(valid):
        winner = None if not any(valid) else valid.index(True)
        return MatchResult(winner, "NO_PLACEMENT" if any(valid) else "NEITHER_PLACED", (0, 0), (0, 0))

    boards = [Board(fleet) for fleet in fleets]  # boards[i] — поле игрока i
    shots = [0, 0]
    current = rng.randrange(2)
    while shots[0] < rules.shot_limit or shots[1] < rules.shot_limit:
        target = boards[1 - current]
        left_in_turn = rules.shots_per_turn
        while left_in_turn and shots[current] < rules.shot_limit:
            result, cell, queries = _shoot(rules, players[current], target, rules.shot_limit - shots[current])
            shots[current] += 1 + queries
            if rules.sees_opponent_shots:
                players[1 - current].on_opponent_shot(cell, result)
            if target.destroyed:
                return MatchResult(current, "FLEET_DESTROYED", tuple(shots), (boards[1].hits, boards[0].hits))
            if result == "MISS" or not rules.hit_keeps_turn:
                left_in_turn -= 1
        current = 1 - current

    hits = (boards[1].hits, boards[0].hits)
    winner = None if hits[0] == hits[1] else int(hits[1] > hits[0])
    return MatchResult(winner, "SHOT_LIMIT", tuple(shots), hits)
