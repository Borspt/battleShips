"""Стратегия только для «Хаки Наблюдения»: совместный поиск оставшихся однопалубных."""

from __future__ import annotations

import math
import time

from .. import model
from ..geometry import placement_distances
from ..placement import random_placement
from .adaptation import Plan, choose_plan
from .contract import ProbabilityBot, core_rules
from .probability import ProbabilityStrategy


class _SearchLimit(Exception):
    pass


def choose_haki_plan(modifier, opponent, profile) -> Plan:
    """Haki defence stays random against adaptable teams; shared attack priors remain."""
    base = choose_plan(modifier, opponent, profile)
    if getattr(opponent, "built_in_bot", None):
        return base
    # Opponents adapt to our public replays; mixed historical openings are not
    # reliable evidence for exposing a predictable border/interior fleet.
    return Plan(random_placement, base.edge_bias,
                "Хаки: случайный флот против команды; исторический дебют не определяет защиту; "
                + base.reason)


class HakiEndgameStrategy(ProbabilityStrategy):
    """Точные веса совместимых расстановок одиночек при ограниченном объёме поиска.

    Точность относится к модели: вес расстановки — произведение прежних весов клеток.
    Это не утверждение о настоящем распределении расстановок противника.
    """

    MAX_COMBINATIONS = 100_000
    MAX_NODES = 100_000

    def __init__(self, *args, search_ms=40.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.search_ms = search_ms

    def _weights(self):
        if (not self.rules.no_touching or self.hits or not self.remaining
                or any(length != 1 for length in self.remaining)
                or len(self.remaining) > 4):
            return super()._weights()
        available = ((1 << self.size) - 1) & ~(self.empty | self.hits | self.sunk)
        count = len(self.remaining)
        if (available.bit_count() < count or self.search_ms <= 0
                or math.comb(available.bit_count(), count) > self.MAX_COMBINATIONS):
            return super()._weights()

        deadline = time.monotonic() + self.search_ms / 1000
        priors = self._priors(1)
        scores = [0.0] * self.size
        chosen = []
        nodes = 0

        def visit(candidates, left, weight):
            nonlocal nodes
            nodes += 1
            if nodes > self.MAX_NODES or (nodes % 64 == 1 and time.monotonic() >= deadline):
                raise _SearchLimit
            if not left:
                for index in chosen:
                    scores[index] += weight
                return
            while candidates.bit_count() >= left:
                bit = candidates & -candidates
                candidates ^= bit
                index = bit.bit_length() - 1
                chosen.append(index)
                visit(candidates & ~(self.side[index] | self.diagonal[index]), left - 1,
                      weight * (priors[index] if priors else 1.0))
                chosen.pop()

        try:
            visit(available, count, 1.0)
        except _SearchLimit:
            # Частичный перебор смещён порядком клеток: его оценки не используем.
            return super()._weights()
        return scores if any(scores) else super()._weights()


class HakiObservationBot(ProbabilityBot):
    """Случайная защита против команд, совместная концовка и до двух DISTANCE."""

    def __init__(self):
        super().__init__()
        # Standard-board tables are warmed before a timed move, as in Boris's bot.
        for length in (1, 2, 3, 4):
            placement_distances(10, 10, length)

    def _distance_budget(self, state, used):
        if state.shots_left <= 1 or used >= 2:
            return 0
        if state.ships and state.rules.modifier != "BIRD_BOX":
            fleet = sum(map(len, state.ships))
            damaged = len({c for c, result in state.opponent_shots if result in ("HIT", "SUNK")})
            if fleet and (fleet - damaged) / fleet > 0.55:
                return 0
        return max(0, min(state.bonuses.get("DISTANCE", 0), 2 - used))

    def plan(self, state: model.GameState) -> Plan:
        return choose_haki_plan(state.rules.modifier, state.opponent_info,
                                self.book.lookup(state.opponent_info))

    def choose_action(self, state: model.GameState, time_budget_ms: int) -> model.Action:
        started = time.monotonic()
        plan = self.plan(state)
        if state.ships is None and not state.shots:
            self._record(state, plan, placed=False)
        shooter = HakiEndgameStrategy(core_rules(state.rules), self.rng, edge_bias=plan.edge_bias)
        for cell, result in state.shots:
            shooter.on_shot_result((cell.row, cell.column), result)
        answers = [r for r in state.bonus_results if r.get("bonus") == "DISTANCE"]
        for answer in answers:
            cell = answer.get("requestedCell") or {}
            row, column, distance = cell.get("row"), cell.get("column"), answer.get("distance")
            if (all(type(v) is int for v in (row, column, distance))
                    and 0 <= row < state.rules.rows and 0 <= column < state.rules.columns
                    and 0 <= distance <= state.rules.rows + state.rules.columns - 2):
                shooter.on_distance((row, column), distance)
        # Оставляем время на обычный расчёт, сериализацию и ответ процессу оболочки.
        remaining_ms = time_budget_ms - (time.monotonic() - started) * 1000 - 30
        shooter.search_ms = max(0.0, min(40.0, remaining_ms))
        if remaining_ms >= 50:
            shooter.distance_uses = self._distance_budget(state, len(answers))
        kind, cell = shooter.next_action()
        if kind == "DISTANCE":
            return model.Action("USE_BONUS", cell=model.Cell(*cell), bonus="DISTANCE")
        return model.Action.shoot(model.Cell(*cell))
