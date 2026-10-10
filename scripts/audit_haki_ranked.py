"""Offline paired audit. Historical fleets are not current opponent policies."""
import json
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from battleships.rules import Rules
from battleships.simulator import Board
from battleships.strategies.haki import HakiEndgameStrategy
from battleships.placement import random_placement, edge_placement, interior_placement
from battleships.strategies.probability import HARD_BIAS


def search(ships, seed, early=False, bias=None):
    rules = Rules.for_modifier('SCHRODINGER_SHIP')
    board = Board(ships)
    s = HakiEndgameStrategy(rules, random.Random(seed), distance_uses=2,
                           sunk_checks=early, edge_bias=bias, search_ms=100000)
    shots = queries = 0
    first_query = None
    while not board.destroyed and shots + queries < 100:
        kind, cell = s.next_action()
        if kind == 'DISTANCE' and shots + queries < 99:
            if first_query is None:
                first_query = shots
            s.on_distance(cell, board.distance(cell))
            queries += 1
        else:
            if kind != 'SHOOT':
                cell = s.next_shot()
            assert cell not in board.shot, 'Repeated shot'
            result = board.receive(cell)
            s.on_shot_result(cell, 'HIT' if result == 'SUNK' else result)
            shots += 1
        for c, owner in board.owner.items():
            if board.afloat[owner] and c not in board.shot:
                assert not (s.empty | s.sunk) & (1 << (c[0]*10+c[1])), 'Excluded live cell'
    assert board.destroyed, 'Fleet not destroyed'
    return {'shots': shots, 'queries': queries, 'firstQueryAfterShot': first_query}


def summarize(rows):
    savings = [r['baseline']['shots']-r['early']['shots'] for r in rows]
    return {'fleets': len(rows), 'baselineShots': statistics.mean(r['baseline']['shots'] for r in rows),
            'earlyShots': statistics.mean(r['early']['shots'] for r in rows),
            'meanShotsSaved': statistics.mean(savings),
            'lowerApprox95': statistics.mean(savings)-2*statistics.stdev(savings)/len(savings)**.5,
            'baselineQueries': statistics.mean(r['baseline']['queries'] for r in rows),
            'earlyQueries': statistics.mean(r['early']['queries'] for r in rows)}


def main():
    report = {'limitations': 'Solo costs, not win rate or Elo. Historical pre-Schrodinger fleets. No damage gate, network, shields or mines. Both arms use two queries and no wall-clock cutoff. Validation fleets were used by an earlier different experiment; not a fresh blind holdout.', 'attack': {}}
    for label, path in [('development', 'scouting/haki/matches.json'),
                        ('validation', 'tests/haki_donatello_review.json')]:
        rows = []
        for rec in json.loads((ROOT/path).read_text(encoding='utf-8')):
            ships = [[tuple(c) for c in ship] for ship in rec['ships']]
            row = {'matchId': rec['matchId'], 'bot': rec['botName']}
            for name, early in [('baseline', False), ('early', True)]:
                samples = [search(ships, 913000+i, early) for i in range(3)]
                row[name] = {k: statistics.mean(v[k] for v in samples) for k in ('shots','queries')}
            rows.append(row)
        report['attack'][label] = {'summary': summarize(rows), 'rows': rows}
        print(label, json.dumps(summarize(rows)), flush=True)
    # Stress-test defence against both unbiased and edge-biased synthetic shooters.
    # A placement wins only if it resists BOTH; this is not a real competitor clone.
    report['defence'] = {}
    for place in (random_placement, edge_placement, interior_placement):
        result = {}
        for label, bias in [('unbiased', None), ('edge_aware', HARD_BIAS)]:
            values = [search(place(Rules(), random.Random(827000+i)), 829000+i, True, bias)['shots'] for i in range(30)]
            result[label] = {'meanShotsToDestroyUs': statistics.mean(values), 'p10': sorted(values)[2]}
        report['defence'][place.__name__] = result
        print(place.__name__, json.dumps(result), flush=True)
    out = ROOT/'runtime/haki-ranked-audit.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
