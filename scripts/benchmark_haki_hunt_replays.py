"""Offline Haki hunt comparison with chronological replay holdout.

Uses complete historical fleets but hides SUNK for the active Schrodinger rules.
Bonuses/opponent turns are not simulated: this measures shots, not win rate/Elo.
No candidate in this script is enabled in the live bot.
"""
import argparse
import json
from pathlib import Path
import random
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from battleships import model
from battleships.rules import Rules
from battleships.simulator import play_solo
from battleships.strategies.haki import HakiEndgameStrategy, choose_haki_plan
from scripts.benchmark_haki_hunt import Normalized, Parity


def measure(records, classes, seeds):
    profiles = json.loads((ROOT/'scouting/profiles.json').read_text(encoding='utf-8'))
    rules = Rules.for_modifier('SCHRODINGER_SHIP')
    rows = []
    for record in records:
        info = model.OpponentInfo(key='bot:'+record['botId'], bot_id=record['botId'])
        plan = choose_haki_plan(rules.modifier, info, profiles.get(info.key))
        ships = [[tuple(c) for c in s] for s in record['ships']]
        result = {'matchId': record['matchId'], 'bot': record['botName'], 'at': record['at']}
        for cls in classes:
            outcomes = [play_solo(rules, cls(rules, random.Random(94100+s),
                                          edge_bias=plan.edge_bias, search_ms=0), ships)
                        for s in range(seeds)]
            result[cls.__name__] = {'shots': statistics.mean(v.shots for v in outcomes),
                                   'completed': sum(v.destroyed for v in outcomes)}
        rows.append(result)
    return rows


def summary(rows, candidate):
    differences = [r['HakiEndgameStrategy']['shots']-r[candidate]['shots'] for r in rows]
    mean = statistics.mean(differences)
    return {'fleets':len(rows), 'baselineShots':statistics.mean(r['HakiEndgameStrategy']['shots'] for r in rows),
            'candidateShots':statistics.mean(r[candidate]['shots'] for r in rows),
            'meanShotsSaved':mean,
            'lowerApprox95':mean-2*statistics.stdev(differences)/len(rows)**.5 if len(rows)>1 else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', type=int, default=5)
    parser.add_argument('--output', default='runtime/muzrob/haki-hunt-replay-benchmark.json')
    args = parser.parse_args()
    if args.seeds < 1:
        parser.error('--seeds must be positive')
    archive = json.loads((ROOT/'scouting/haki/matches.json').read_text(encoding='utf-8'))
    cutoff = max(r['at'] for r in archive)
    holdout = [r for r in json.loads((ROOT/'tests/haki_donatello_review.json').read_text(encoding='utf-8'))
               if r['at'] > cutoff]
    if not holdout:
        raise SystemExit('Holdout must be newer than the development archive')
    classes = [HakiEndgameStrategy, Normalized, Parity]
    train = measure(archive, classes, args.seeds)
    summaries = {cls.__name__:summary(train, cls.__name__) for cls in classes[1:]}
    print(json.dumps({'development':summaries}, ensure_ascii=False), flush=True)
    winner = max(summaries, key=lambda name:summaries[name]['meanShotsSaved'])
    selected = next(cls for cls in classes if cls.__name__ == winner)
    validation = measure(holdout, [HakiEndgameStrategy, selected], args.seeds)
    report = {'mode':'SCHRODINGER_SHIP', 'seedsPerFleet':args.seeds,
              'searchMs':0, 'bonuses':False, 'cutoff':cutoff,
              'limitations':'Historical pre-Schrodinger fleets; offline shot metric, not live Elo. Shared profile frozen for all arms. Exact singletons time search disabled equally for reproducibility.',
              'development':summaries, 'selected':winner,
              'holdout':summary(validation, winner), 'developmentRows':train, 'holdoutRows':validation}
    report['recommendedForIntegration'] = (report['holdout']['fleets'] >= 8
                                           and report['holdout']['meanShotsSaved'] >= 2
                                           and report['holdout']['lowerApprox95'] > 0)
    path = ROOT/args.output
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if not k.endswith('Rows')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
