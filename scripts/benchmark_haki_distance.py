"""Replay a public loss endgame; also test every compatible singleton pair.
This measures remaining shots, not tournament win rate or net Elo.
"""
import copy
import itertools
import json
from pathlib import Path
import random
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from battleships import model
from battleships.simulator import Board
from battleships.strategies.haki import HakiObservationBot


def run(initial, fleet, uses, seed):
    state = copy.deepcopy(initial)
    state.bonuses = {'DISTANCE': uses}
    bot = HakiObservationBot()
    bot.rng = random.Random(seed)
    board = Board([[tuple(c)] for c in fleet])
    shots = queries = 0
    while not board.destroyed and state.shots_left > 0:
        action = bot.choose_action(state, 150)
        assert model.valid_action(action, state, allow_bonuses=True)
        cell = (action.cell.row, action.cell.column)
        state.shots_left -= 1
        if action.type == 'USE_BONUS':
            assert action.bonus == 'DISTANCE' and queries < 2
            state.bonuses['DISTANCE'] -= 1
            queries += 1
            state.bonus_results.append({'bonus': 'DISTANCE', 'requestedCell': action.cell.wire(),
                                        'distance': board.distance(cell)})
        else:
            shots += 1
            state.shots.append((action.cell, board.receive(cell)))
    assert board.destroyed
    return shots, queries


if __name__ == '__main__':
    fixture = json.loads((ROOT / 'tests/haki_endgame_position.json').read_text())
    initial = model.GameState.from_message(fixture)
    targets = initial.legal_targets()
    pairs = [(a, b) for a, b in itertools.combinations(targets, 2)
             if max(abs(a.row-b.row), abs(a.column-b.column)) > 1]
    datasets = {'actual_pair_100_seeds': [fixture['actualRemaining']] * 100,
                'all_compatible_pairs': [[(a.row, a.column), (b.row, b.column)] for a, b in pairs]}
    result = {}
    for label, fleets in datasets.items():
        result[label] = {}
        for uses in (0, 2):
            outcomes = [run(initial, f, uses, 27000+i) for i, f in enumerate(fleets)]
            result[label][str(uses)] = {'games': len(outcomes),
                'meanShots': statistics.mean(s for s, q in outcomes),
                'meanQueries': statistics.mean(q for s, q in outcomes)}
    print(json.dumps(result, indent=2))
