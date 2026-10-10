"""Research only: bounded exact enumeration for four mixed ships."""
import json
import random
import statistics
import time
from pathlib import Path
from scripts.audit_haki_ranked import ROOT
from battleships.rules import Rules
from battleships.simulator import Board
from battleships.strategies.haki import HakiEndgameStrategy
from battleships.strategies import probability

class FourShips(HakiEndgameStrategy):
    def _endgame_configs(self):
        if len(self.remaining) == 4 and any(n != 1 for n in self.remaining):
            previous = probability.ENDGAME_SHIPS
            probability.ENDGAME_SHIPS = 4
            try:
                return probability.ProbabilityStrategy._endgame_configs(self)
            finally:
                probability.ENDGAME_SHIPS = previous
        return super()._endgame_configs()

def run(ships, cls, seed):
    board = Board(ships)
    s = cls(Rules.for_modifier('SCHRODINGER_SHIP'), random.Random(seed), distance_uses=2, search_ms=100000)
    shots = queries = 0
    max_ms = 0
    while not board.destroyed and shots+queries < 100:
        start = time.perf_counter()
        kind, cell = s.next_action()
        max_ms = max(max_ms, (time.perf_counter()-start)*1000)
        if kind == 'DISTANCE' and shots+queries < 99:
            s.on_distance(cell, board.distance(cell)); queries += 1
        else:
            if kind != 'SHOOT': cell = s.next_shot()
            assert cell not in board.shot
            result = board.receive(cell)
            s.on_shot_result(cell, 'HIT' if result=='SUNK' else result); shots += 1
        for c, owner in board.owner.items():
            if board.afloat[owner] and c not in board.shot:
                assert not (s.empty | s.sunk) & (1 << (c[0]*10+c[1]))
    assert board.destroyed
    return shots, queries, max_ms

report = {'limitations': 'Historical fleets, solo shots, no damage gate or network. No win-rate claim; old validation set was previously examined. Research only; 40k node cap retained, wall-clock cap disabled equally.'}
for label, name in [('development','scouting/haki/matches.json'),('validation','tests/haki_donatello_review.json')]:
    rows=[]
    for rec in json.loads((ROOT/name).read_text(encoding='utf-8')):
        ships = [[tuple(c) for c in ship] for ship in rec['ships']]
        row={'matchId':rec['matchId']}
        for key,cls in [('baseline',HakiEndgameStrategy),('four',FourShips)]:
            samples=[run(ships,cls,938000+i) for i in range(3)]
            row[key]={'shots':statistics.mean(v[0] for v in samples),'queries':statistics.mean(v[1] for v in samples),'maxActionMs':max(v[2] for v in samples)}
        rows.append(row)
    delta=[r['baseline']['shots']-r['four']['shots'] for r in rows]
    summary={'fleets':len(rows),'baselineShots':statistics.mean(r['baseline']['shots'] for r in rows),'candidateShots':statistics.mean(r['four']['shots'] for r in rows),'meanShotsSaved':statistics.mean(delta),'lowerApprox95':statistics.mean(delta)-2*statistics.stdev(delta)/len(delta)**.5,'candidateMaxMs':max(r['four']['maxActionMs'] for r in rows)}
    report[label]={'summary':summary,'rows':rows}
    print(label,json.dumps(summary),flush=True)
(ROOT/'runtime/haki-four-ships-audit.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
