"""Offline paired DUEL comparison: change only Haki's defence, not its shooting.

python scripts/benchmark_haki_defence.py --games 200
Synthetic opponents are not replicas of tournament bots.
"""
import argparse
from functools import partial
import json
from pathlib import Path
import random
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from battleships.rules import Rules
from battleships.simulator import play_match
from battleships.strategies.adaptation import choose_plan
from battleships.strategies.haki import choose_haki_plan, HakiEndgameStrategy
from battleships.strategies.probability import ProbabilityStrategy, HARD_BIAS


def run(games):
    rules = Rules.for_modifier('DUEL')
    results = []
    for label, share, attack in [('center', 0.1, None), ('edge', 0.95, HARD_BIAS),
                                 ('unknown', None, None)]:
        profile = {'matches': 200, 'openingOnBorder': 0.99, 'vsBots': {
            'matches': 10, 'openingShots': 150, 'openingOnBorder': share}}
        opponent = SimpleNamespace(built_in_bot=None)
        plans = [choose_plan('DUEL', opponent, profile), choose_haki_plan('DUEL', opponent, profile)]
        totals = []
        for plan in plans:
            wins = losses = draws = 0
            for seed in range(games):
                match = play_match(rules,
                    # Offline: avoid wall-clock fallback changing identical seeded runs.
                    # Node/combination limits remain; production retains its 40 ms cap.
                    partial(HakiEndgameStrategy, placement=plan.placement, edge_bias=plan.edge_bias,
                            search_ms=100_000),
                    partial(ProbabilityStrategy, edge_bias=attack), random.Random(481000 + seed))
                wins += match.winner == 0
                losses += match.winner == 1
                draws += match.winner is None
            totals.append({'placement': plan.placement.__name__, 'wins': wins,
                           'losses': losses, 'draws': draws})
        results.append({'opponent': label, 'old': totals[0], 'haki': totals[1]})
    return {'gamesPerVersionPerOpponent': games, 'results': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--games', type=int, default=200)
    args = parser.parse_args()
    if args.games < 1:
        parser.error('--games must be positive')
    print(json.dumps(run(args.games), indent=2))
