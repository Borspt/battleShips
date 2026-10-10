"""Офлайн-проверка гипотез поиска. Кандидаты не подключены к живому боту.

python scripts/benchmark_haki_hunt.py --games 300
Сравнивает одинаковые поля и seed; лимит времени концовки может влиять на повторы.
Метрика — выстрелы до потопления, не доля турнирных побед. Бонусы не моделируются.
"""
import sys, random, statistics, json, argparse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from battleships.strategies.haki import HakiEndgameStrategy
from battleships.geometry import placements
from battleships.placement import random_placement, edge_placement, interior_placement
from battleships.rules import Rules
from battleships.simulator import play_solo

class Normalized(HakiEndgameStrategy):
    def _weights(self):
        if self.hits or all(n==1 for n in self.remaining):
            return super()._weights()
        scores=[0.0]*self.size
        for length in set(self.remaining):
            prior=self._priors(length)
            valid=[(cells,prior[i] if prior else 1.) for i,(mask,around,cells) in enumerate(placements(self.rules.rows,self.rules.columns,length)) if not mask & (self.empty|self.sunk)]
            total=sum(w for cells,w in valid)
            if total:
                for cells,w in valid:
                    for i in cells:scores[i]+=w*self.remaining.count(length)/total
        return scores

class Parity(HakiEndgameStrategy):
    def _weights(self):
        scores=super()._weights()
        if self.hits or not any(n>1 for n in self.remaining):return scores
        # One fixed lattice; randomized by symmetries in live experiment later.
        for i in range(self.size):
            if (i//self.rules.columns+i%self.rules.columns)%2:scores[i]=0
        return scores if any(scores) else super()._weights()

if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--games', type=int, default=300)
    args = parser.parse_args()
    if args.games < 1:
        parser.error('--games must be positive')
    rules=Rules.for_modifier('DUEL')
    for place in [random_placement,edge_placement,interior_placement]:
        out={}
        for cls in [HakiEndgameStrategy,Normalized,Parity]:
            a=[play_solo(rules,cls(rules,random.Random(seed+98000)),place(rules,random.Random(seed+77000))).shots for seed in range(args.games)]
            out[cls.__name__]={'mean':statistics.mean(a),'p90':sorted(a)[int((len(a)-1)*.9)]}
        print(place.__name__,json.dumps(out),flush=True)
