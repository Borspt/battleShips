"""Сравнение стратегий в симуляторе.

    python -m battleships.simulate --a probability --b baseline --games 1000
    python -m battleships.simulate --a edges-biased --b edges --placement edges
"""

from __future__ import annotations

import argparse
import random
import statistics
import time

from .placement import PLACEMENTS
from .rules import Rules
from .simulator import play_match, play_solo
from .strategies import STRATEGIES

MODIFIERS = ["DUEL", "THREE_MUSKETEERS", "SCHRODINGER_SHIP", "HUNGER_GAMES", "SPEEDRUN", "BIRD_BOX", "OLD_SCHOOL"]


def solo_report(name: str, rules: Rules, games: int, seed: int, placement: str = "random") -> str:
    """Стрельба по расстановкам выбранного вида: число выстрелов до потопления флота и время на ход."""
    rng = random.Random(seed)
    shots, hits, destroyed, slowest = [], [], 0, 0.0
    started = time.perf_counter()
    for _ in range(games):
        ships = PLACEMENTS[placement](rules, rng)
        shooter = STRATEGIES[name](rules, random.Random(rng.random()))
        game_started = time.perf_counter()
        result = play_solo(rules, shooter, ships)
        slowest = max(slowest, (time.perf_counter() - game_started) / result.shots)
        shots.append(result.shots)
        hits.append(result.hits)
        destroyed += result.destroyed
    per_move = (time.perf_counter() - started) / sum(shots) * 1000
    ordered = sorted(shots)
    return (
        f"{name:12} выстрелов: среднее {statistics.mean(shots):5.1f}, медиана {statistics.median(shots):4.0f}, "
        f"90% {ordered[int(games * 0.9) - 1]:3d}, худшее {ordered[-1]:3d} | попаданий {statistics.mean(hits):4.1f} | "
        f"флот потоплен {destroyed / games:6.1%} | на ход {per_move:.2f} мс "
        f"(самая медленная партия {slowest * 1000:.2f} мс)"
    )


def duel_report(a: str, b: str, rules: Rules, games: int, seed: int) -> str:
    rng = random.Random(seed)
    wins, draws = [0, 0], 0
    for _ in range(games):
        result = play_match(rules, STRATEGIES[a], STRATEGIES[b], rng)
        if result.winner is None:
            draws += 1
        else:
            wins[result.winner] += 1
    return f"{a} против {b}: побед {wins[0] / games:.1%}, поражений {wins[1] / games:.1%}, ничьих {draws / games:.1%}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Сравнение стратегий морского боя в симуляторе")
    parser.add_argument("--a", default="probability", choices=STRATEGIES)
    parser.add_argument("--b", default="baseline", choices=STRATEGIES)
    parser.add_argument("--games", type=int, default=1000)
    parser.add_argument("--modifier", choices=MODIFIERS, default=None)
    parser.add_argument("--placement", choices=PLACEMENTS, default="random",
                        help="расстановка мишени в замере стрельбы; в партиях каждая стратегия расставляет сама")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    rules = Rules.for_modifier(args.modifier)
    print(f"Правила: модификатор {args.modifier or 'нет'}, лимит выстрелов {rules.shot_limit}, партий {args.games}, "
          f"мишень в замере стрельбы: {args.placement}")
    for name in dict.fromkeys([args.a, args.b]):
        print(solo_report(name, rules, args.games, args.seed, args.placement))
    print(duel_report(args.a, args.b, rules, args.games, args.seed))


if __name__ == "__main__":
    main()
