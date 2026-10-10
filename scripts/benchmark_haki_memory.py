"""Chronological replay holdout + actual loss endgame; no claim about online Elo."""
import json
from pathlib import Path
import random
import statistics
import sys
import time
from datetime import datetime
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from battleships.rules import Rules
from battleships.simulator import Board
from battleships.strategies.haki import HakiEndgameStrategy, choose_haki_plan
from battleships.strategies.haki_memory import adapt_recent, recent_records, archive_digest
from battleships import model
from scripts.benchmark_haki_distance import run


def solo(record, plan, seed):
    board=Board([[tuple(c) for c in s] for s in record['ships']])
    shooter=HakiEndgameStrategy(Rules.for_modifier(record['modifier']),random.Random(seed),
                              edge_bias=plan.edge_bias)
    for shots in range(1,101):
        cell=shooter.next_shot()
        shooter.on_shot_result(cell,board.receive(cell))
        if board.destroyed:
            return shots
    raise AssertionError('Incomplete game')


def main():
    records=json.loads((ROOT/'scouting/haki/matches.json').read_text(encoding='utf-8'))
    # Same shared prior in both arms. Test fleet is never part of training history.
    shared=json.loads((ROOT/'scouting/profiles.json').read_text(encoding='utf-8'))
    results=[]
    for r in records:
        prior=recent_records([v for v in records if v['at'] < r['at']],r['botId'],r['modifier'],
                             datetime.fromisoformat(r['at'].replace('Z','+00:00')))
        if len(prior)<8:
            continue
        info=model.OpponentInfo(key='bot:'+r['botId'],bot_id=r['botId'])
        base=choose_haki_plan(r['modifier'],info,shared.get(info.key))
        adapted=adapt_recent(base,prior)
        old=[solo(r,base,s) for s in range(20)]
        new=[solo(r,adapted,s) for s in range(20)]
        results.append(dict(bot=r['botName'],botId=r['botId'],modifier=r['modifier'],match=r['matchId'],prior=len(prior),
                            baseline=statistics.mean(old),adapted=statistics.mean(new)))
    fixture=json.loads((ROOT/'tests/haki_three_singletons.json').read_text())
    initial=model.GameState.from_message(fixture)
    endgame={}
    for uses in (0,2):
        started=time.perf_counter()
        values=[run(initial,fixture['actualRemaining'],uses,18000+i) for i in range(100)]
        endgame[str(uses)]={'shots':statistics.mean(v[0] for v in values),
                           'queries':statistics.mean(v[1] for v in values),
                           'elapsedSeconds':time.perf_counter()-started}
    result={'holdout':results,'lossEndgame100Seeds':endgame}
    policies={}
    for key in sorted({r['botId']+':'+r['modifier'] for r in results}):
        subset=[r for r in results if r['botId']+':'+r['modifier']==key]
        deltas=[r['baseline']-r['adapted'] for r in subset]
        gain=statistics.mean(deltas)
        lower=gain - 2*statistics.stdev(deltas)/len(deltas)**.5 if len(deltas)>1 else -float('inf')
        policies[key]={'approved':len(deltas)>=6 and gain>=2 and lower>0,
                       'heldOutFleets':len(deltas),'meanShotsSaved':gain}
    result['policies']=policies
    if '--write-policy' in sys.argv:
        policy={'dataHash':archive_digest(records),'policies':policies}
        (ROOT/'scouting/haki/policies.json').write_text(json.dumps(policy,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
