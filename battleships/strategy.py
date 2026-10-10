"""Reference fallback only. Boris can provide another module:Class via --strategy."""
import random
import time
from typing import Protocol

from .model import Action, Cell, GameState, Rules


class Strategy(Protocol):
    def place_ships(self, rules: Rules) -> list[list[Cell]]: ...
    def choose_action(self, state: GameState, time_budget_ms: int) -> Action: ...


class BaselineStrategy:
    """Random hunt + adjacent targeting. Does not spend rating on bonuses/bets."""

    def __init__(self):
        self.rng = random.Random()

    def place_ships(self, rules: Rules) -> list[list[Cell]]:
        if rules.touch_rule != "NO_TOUCHING_EVEN_DIAGONALLY":
            raise ValueError("Unsupported touch rule")
        placements = {}
        for length in set(rules.fleet):
            placements[length] = [
                [Cell(r + i * dr, c + i * dc) for i in range(length)]
                for r in range(rules.rows) for c in range(rules.columns)
                for dr, dc in ([(0, 1)] if length == 1 else [(0, 1), (1, 0)])
                if r + (length - 1) * dr < rules.rows
                and c + (length - 1) * dc < rules.columns
            ]
            self.rng.shuffle(placements[length])
        deadline = time.monotonic() + 0.4

        def search(index: int, forbidden: set[Cell]) -> list[list[Cell]] | None:
            if time.monotonic() > deadline:
                raise TimeoutError("Placement budget exhausted")
            if index == len(rules.fleet):
                return []
            for ship in placements[rules.fleet[index]]:
                if any(c in forbidden for c in ship):
                    continue
                around = {Cell(c.row + dr, c.column + dc) for c in ship
                          for dr in (-1, 0, 1) for dc in (-1, 0, 1)}
                rest = search(index + 1, forbidden | around)
                if rest is not None:
                    return [ship] + rest
            return None

        result = search(0, set())
        if result is None:
            raise ValueError("No valid placement")
        return result

    def choose_action(self, state: GameState, time_budget_ms: int) -> Action:
        targets = state.legal_targets()
        if not targets:
            raise ValueError("No legal target")
        observed = state.observations()
        defended = [c for c in targets if observed.get(c) == "DEFENDED"]
        if defended:
            return Action.shoot(self.rng.choice(defended))
        adjacent = []
        for c in targets:
            if any(observed.get(Cell(c.row + dr, c.column + dc)) == "HIT"
                   for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                adjacent.append(c)
        return Action.shoot(self.rng.choice(adjacent or targets))
