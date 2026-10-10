"""Offline paired endgames, not win-rate/Elo evidence. No platform calls.

Compare unchanged Haki except enabling complete four-mixed enumeration. Test
the recorded position, alternative fleets compatible with it, and independent
random fleets/observations. Hidden SUNK is preserved in every continuation.
"""
import copy
import json
from pathlib import Path
import random
import statistics
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from battleships import model
from battleships.geometry import bits, placements, ring
from battleships.placement import random_placement
from battleships.rules import Rules
from battleships.simulator import Board
from battleships.strategies.contract import core_rules
from battleships.strategies.haki import HakiEndgameStrategy, HakiObservationBot


class Experimental(HakiEndgameStrategy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, four_mixed=True, **kwargs)


class Before(HakiEndgameStrategy):
    def _endgame_configs(self):
        if len(self.remaining) == 4 and any(n != 1 for n in self.remaining):
            return None
        return super()._endgame_configs()


def run(initial, fleet, seed, strategy):
    state = copy.deepcopy(initial)
    bot = HakiObservationBot()
    bot.rng = random.Random(seed)
    board = Board([[tuple(c) for c in ship] for ship in fleet])
    shots = queries = 0
    timings = []
    with patch('battleships.strategies.haki.HakiEndgameStrategy', strategy):
        while not board.destroyed and state.shots_left > 0:
            started = time.perf_counter()
            action = bot.choose_action(state, 150)
            timings.append((time.perf_counter() - started) * 1000)
            assert model.valid_action(action, state, allow_bonuses=True)
            cell = (action.cell.row, action.cell.column)
            state.shots_left -= 1
            if action.type == 'USE_BONUS':
                queries += 1
                state.bonuses['DISTANCE'] -= 1
                state.bonus_results.append({'bonus': 'DISTANCE', 'requestedCell': action.cell.wire(),
                                            'distance': board.distance(cell)})
            else:
                shots += 1
                result = board.receive(cell)
                state.shots.append((action.cell, 'HIT' if result == 'SUNK' else result))
    return {'shots': shots, 'queries': queries, 'destroyed': board.destroyed,
            'maxMs': max(timings)}


def independent(seed, rules_wire):
    rng = random.Random(seed)
    fleet = random_placement(Rules(), rng)
    fleet.sort(key=len, reverse=True)
    sunk, live = fleet[:5] + fleet[6:7], fleet[5:6] + fleet[7:]
    occupied = {c for ship in fleet for c in ship}
    sunkmask = sum(1 << (r*10+c) for ship in sunk for r,c in ship)
    empty = {divmod(i,10) for i in bits(ring(sunkmask,10,10))} - occupied
    free = sorted({divmod(i,10) for i in range(100)} - occupied - empty)
    empty.update(rng.sample(free, min(8,len(free))))
    state = model.GameState.from_message({'rules': rules_wire,
        'bonuses':[{'bonus':'DISTANCE','uses':2}]})
    state.shots = [(model.Cell(r,c),'HIT') for ship in sunk for r,c in ship]
    state.shots += [(model.Cell(r,c),'MISS') for r,c in sorted(empty)]
    state.shots_left = 100-len(state.shots)
    return state, live


def main():
    fixture = json.loads((ROOT/'tests/haki_four_mixed.json').read_text(encoding='utf-8'))
    state = model.GameState.from_message(fixture)
    s = HakiEndgameStrategy(core_rules(state.rules), search_ms=2000, four_mixed=True)
    for cell,result in state.shots:
        s.on_shot_result((cell.row,cell.column),result)
    configs = s._endgame_configs()
    cases = {'actual': [(state,fixture['actualRemaining'])]*20,
             'compatible_alternative_fleets': [], 'independent_fleets': []}
    for _,_,chosen in random.Random(10610).sample(configs,24):
        fleet = [[divmod(i,10) for i in placements(10,10,n)[j][2]] for n,j in chosen]
        cases['compatible_alternative_fleets'].append((state,fleet))
    cases['independent_fleets'] = [independent(81200+i,fixture['rules']) for i in range(24)]
    output = {'scope':'remaining shots and queries; not match wins or Elo', 'groups':{}}
    for name, dataset in cases.items():
        result = {}
        for label,strategy in [('before',Before),('fourMixed',Experimental)]:
            outcomes = [run(initial,fleet,99100+i,strategy) for i,(initial,fleet) in enumerate(dataset)]
            result[label] = {'cases':len(outcomes),'meanShots':statistics.mean(x['shots'] for x in outcomes),
                'meanQueries':statistics.mean(x['queries'] for x in outcomes),
                'destroyed':sum(x['destroyed'] for x in outcomes),
                'maxActionMs':max(x['maxMs'] for x in outcomes)}
        output['groups'][name] = result
    print(json.dumps(output,indent=2))


if __name__ == '__main__':
    main()
