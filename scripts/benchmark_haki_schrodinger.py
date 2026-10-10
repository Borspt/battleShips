"""Offline paired hidden-sink audit; never connects to the game platform.

Run: python -m scripts.benchmark_haki_schrodinger --games 100 --output runtime/muzrob/schrodinger-audit.json
These are solo search costs, not estimated competitive win rates or Elo.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import statistics
import time

from battleships.placement import random_placement
from battleships.rules import Rules
from battleships.simulator import Board
from battleships.strategies.haki import HakiEndgameStrategy
from battleships.strategies.probability import ProbabilityStrategy


class LegacyHiddenStrategy(HakiEndgameStrategy):
    """Freeze the previous geometric inference for a paired comparison."""
    def _infer_sunk(self):
        return ProbabilityStrategy._infer_sunk(self)


def audit(games=100):
    results = {"announced": [], "hidden": [], "hidden_geometry": []}
    for seed in range(games):
        ships = random_placement(Rules(), random.Random(seed + 41000))
        for mode in results:
            rules = Rules.for_modifier("SCHRODINGER_SHIP" if mode != "announced" else None)
            board = Board(ships)
            strategy_class = LegacyHiddenStrategy if mode == "hidden" else HakiEndgameStrategy
            strategy = strategy_class(rules, random.Random(seed + 51000), distance_uses=2)
            shots = queries = 0
            times = []
            while shots + queries < 100 and not board.destroyed:
                start = time.perf_counter()
                kind, cell = strategy.next_action()
                times.append((time.perf_counter() - start) * 1000)
                if kind == "DISTANCE" and shots + queries < 99:
                    assert not strategy.hits, "Unsafe distance with ambiguous hidden sinks"
                    strategy.on_distance(cell, board.distance(cell))
                    queries += 1
                    continue
                if kind == "DISTANCE":
                    cell = strategy.next_shot()
                assert cell not in board.shot, "Repeated shot"
                response = board.receive(cell)
                strategy.on_shot_result(cell, "HIT" if mode != "announced" and response == "SUNK" else response)
                shots += 1
                for (row, column), owner in board.owner.items():
                    if board.afloat[owner] and (row, column) not in board.shot:
                        assert not (strategy.empty | strategy.sunk) & (1 << (row * 10 + column)), "Excluded live cell"
            results[mode].append({"seed": seed, "shots": shots, "queries": queries,
                                  "destroyed": board.destroyed, "times_ms": times})
    summary = {}
    for mode, rows in results.items():
        times = sorted(t for row in rows for t in row["times_ms"])
        summary[mode] = {"games": games, "completed": sum(r["destroyed"] for r in rows),
                         "mean_shots": statistics.mean(r["shots"] for r in rows),
                         "mean_queries": statistics.mean(r["queries"] for r in rows),
                         "p95_action_ms": times[int(.95 * (len(times) - 1))],
                         "max_action_ms": max(times)}
    summary["hidden_minus_announced_shots"] = statistics.mean(
        b["shots"] - a["shots"] for a, b in zip(results["announced"], results["hidden"]))
    return {"scope": "Solo random fleets; two distance uses; excludes live damage-budget gate and network latency",
            "summary": summary, "runs": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.games < 1:
        parser.error("--games must be positive")
    report = audit(args.games)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
