import time
from battleships.strategy import BaselineStrategy


class SlowStrategy(BaselineStrategy):
    def choose_action(self, state, time_budget_ms):
        time.sleep(5)
        return super().choose_action(state, time_budget_ms)


class CrashingStrategy(BaselineStrategy):
    def choose_action(self, state, time_budget_ms):
        raise RuntimeError("Deliberate test failure")


def rules(modifier=None):
    return {
        "columns": 10, "rows": 10,
        "fleet": [{"length": 4, "count": 1}, {"length": 3, "count": 2},
                  {"length": 2, "count": 3}, {"length": 1, "count": 4}],
        "touchRule": "NO_TOUCHING_EVEN_DIAGONALLY",
        "turnAfterHitRule": "SHOOTER_KEEPS_TURN",
        "matchDurationSeconds": 210, "shotLimitPerPlayer": 100,
        "moveTimeoutSeconds": 1, "placementWindowSeconds": 10,
        "modifier": modifier, "shotsPerTurn": 1,
    }


def started(modifier=None):
    return {"type": "MATCH_STARTED", "rules": rules(modifier), "bonuses": [],
            "opponent": {"kind": "BUILT_IN_BOT", "builtInBot": "EASY"}}
