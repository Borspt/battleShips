"""Offline one-hypothesis comparison: diagonal information tie-break in hunt."""
import sys, json, random, statistics, math, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from battleships.strategies.haki import HakiEndgameStrategy
from battleships.geometry import bits, to_cell
from scripts.benchmark_haki_hunt_replays import measure, summary
class DiagonalTiebreak(HakiEndgameStrategy):
    def next_shot(self):
        if self.hits or self.known_ship or len(self.remaining)<=4:
            return super().next_shot()
        weights=self._weights()
        best=max(weights)
        if best<=0:return super().next_shot()
        # Change only otherwise equally scored shots; no altered hit probabilities.
        choices=[i for i,w in enumerate(weights) if w==best]
        unknown=((1<<self.size)-1)&~(self.empty|self.hits|self.sunk)
        score=max((self.diagonal[i]&unknown).bit_count() for i in choices)
        choices=[i for i in choices if (self.diagonal[i]&unknown).bit_count()==score]
        return to_cell(self.rng.choice(choices),self.rules.columns)
if __name__=='__main__':
    archive=json.loads((ROOT/'scouting/haki/matches.json').read_text(encoding='utf-8'))
    cutoff=max(r['at'] for r in archive)
    holdout=[r for r in json.loads((ROOT/'tests/haki_donatello_review.json').read_text(encoding='utf-8')) if r['at']>cutoff]
    classes=[HakiEndgameStrategy,DiagonalTiebreak]
    out={'hypothesis':'Hunt maximum-score ties prefer more unknown diagonal cells; only >4 ships and no hits.','baseline':'a25096c','mode':'SCHRODINGER_SHIP','seedsPerFleet':5,'limitations':'Historical pre-Schrodinger real fleets, holdout previously examined for other hypotheses; frozen priors; no bonus, mine, shield, turn timing or Elo; exact singleton time search equally disabled.'}
    for name,records in [('development',archive),('holdout',holdout)]:
        t=time.monotonic(); rows=measure(records,classes,5)
        out[name]=summary(rows,'DiagonalTiebreak');out[name+'Rows']=rows
        print(name,json.dumps(out[name]),'seconds',time.monotonic()-t,flush=True)
    out['recommended']=out['holdout']['meanShotsSaved']>=2 and out['holdout']['lowerApprox95']>0
    (ROOT/'DOCUMENTS/HAKI-DIAGONAL-TIEBREAK-2026-10-10.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
