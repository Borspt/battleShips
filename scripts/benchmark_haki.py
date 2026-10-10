"""Офлайн-сравнение концовок на новых случайных полях, без токена и сети.

Из корня: .venv/Scripts/python.exe scripts/benchmark_haki.py --games 200
"""
import argparse
import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from battleships.placement import random_placement
from battleships.rules import Rules
from battleships.simulator import play_match, play_solo
from battleships.strategies.haki import HakiEndgameStrategy
from battleships.strategies.probability import ProbabilityStrategy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--games', type=int, default=200)
    args = parser.parse_args()
    if args.games < 1:
        parser.error('--games must be positive')
    rules = Rules.for_modifier('DUEL')
    old, new, wins = [], [], [0, 0]
    draws = 0
    for seed in range(args.games):
        ships = random_placement(rules, random.Random(seed + 87000))
        old.append(play_solo(rules, ProbabilityStrategy(rules, random.Random(seed + 99000)), ships).shots)
        new.append(play_solo(rules, HakiEndgameStrategy(rules, random.Random(seed + 99000)), ships).shots)
        match = play_match(rules, HakiEndgameStrategy, ProbabilityStrategy, random.Random(seed + 35000))
        if match.winner is None:
            draws += 1
        else:
            wins[match.winner] += 1
    print(json.dumps({'games': args.games, 'oldMeanShots': statistics.mean(old),
                      'hakiMeanShots': statistics.mean(new), 'hakiWins': wins[0],
                      'oldWins': wins[1], 'draws': draws}, indent=2))


if __name__ == '__main__':
    main()
