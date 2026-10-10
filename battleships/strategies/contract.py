"""Стратегии под контракт оболочки (docs/strategy-contract.md).

Запуск: python -m battleships --strategy battleships.strategies.contract:ProbabilityBot
"""

from __future__ import annotations

import random

from .. import model
from ..placement import random_placement
from ..rules import Rules
from .adaptation import Plan, ProfileBook, choose_plan
from .probability import ProbabilityStrategy


def core_rules(rules: model.Rules) -> Rules:
    return Rules(
        rows=rules.rows,
        columns=rules.columns,
        fleet=rules.fleet,
        no_touching=rules.touch_rule == "NO_TOUCHING_EVEN_DIAGONALLY",
        hit_keeps_turn=rules.turn_after_hit_rule != "HIT_SPENDS_SHOT" and rules.modifier != "DUEL",
        shots_per_turn=rules.shots_per_turn,
        shot_limit=rules.shot_limit_per_player,
        modifier=rules.modifier,
    )


class ProbabilityBot:
    """Карта вероятностей. Памяти между вызовами нет: знания каждый раз собираются из state.shots.

    Расстановка и поправка на края в стрельбе выбираются под соперника по его профилю
    из разведки (battleships/strategies/adaptation.py).
    """

    def __init__(self):
        self.rng = random.Random()
        self.book = ProfileBook()

    def plan(self, state: model.GameState) -> Plan:
        return choose_plan(state.rules.modifier, state.opponent_info, self.book.lookup(state.opponent_info))

    def place_ships(self, rules: model.Rules) -> list[list[model.Cell]]:
        """Расстановка, когда соперник неизвестен."""
        return self._place(rules, random_placement)

    def place_ships_for_match(self, state: model.GameState) -> list[list[model.Cell]]:
        return self._place(state.rules, self.plan(state).placement)

    def choose_action(self, state: model.GameState, time_budget_ms: int) -> model.Action:
        shooter = ProbabilityStrategy(core_rules(state.rules), self.rng, edge_bias=self.plan(state).edge_bias)
        for cell, result in state.shots:
            shooter.on_shot_result((cell.row, cell.column), result)
        return model.Action.shoot(model.Cell(*shooter.next_shot()))

    def _place(self, rules: model.Rules, placement) -> list[list[model.Cell]]:
        ships = placement(core_rules(rules), self.rng)
        return [[model.Cell(row, column) for row, column in ship] for ship in ships]
