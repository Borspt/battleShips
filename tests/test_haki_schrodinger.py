import json
from pathlib import Path
import random
import unittest
from unittest.mock import patch

from battleships import model
from battleships.placement import random_placement
from battleships.rules import Rules
from battleships.simulator import Board
from battleships.strategies.haki import HakiEndgameStrategy, HakiObservationBot


def hidden_endgame():
    fixture=json.loads(Path('tests/haki_three_singletons.json').read_text())
    fixture['rules']['modifier']='SCHRODINGER_SHIP'
    fixture['rules']['shotsPerTurn']=1
    for shot in fixture['shots']:
        if shot['result']=='SUNK':
            shot['result']='HIT'
    return model.GameState.from_message(fixture)


class SchrodingerTest(unittest.TestCase):
    def test_three_mixed_ships_inherit_boris_endgame_threshold(self):
        shooter=HakiEndgameStrategy(Rules(rows=4,columns=5,fleet=(2,1,1)),distance_uses=2)
        self.assertTrue(shooter._endgame_configs())
        self.assertEqual(shooter.next_action()[0],'DISTANCE')

    def test_distance_works_after_proved_sinks_without_any_sunk_message(self):
        state=hidden_endgame()
        action=HakiObservationBot().choose_action(state,150)
        self.assertEqual(action.bonus,'DISTANCE')
        self.assertTrue(model.valid_action(action,state,allow_bonuses=True))

    def test_ambiguous_hit_does_not_trigger_distance(self):
        s=HakiEndgameStrategy(Rules(rows=5,columns=5,fleet=(2,1),modifier='SCHRODINGER_SHIP'),
                             distance_uses=2)
        s.on_shot_result((2,2),'HIT')
        self.assertTrue(s.hits)
        self.assertEqual(s.next_action()[0],'SHOOT')

    def test_haki_passes_opponent_mines_to_shared_spread(self):
        state=hidden_endgame()
        state.opponent={'bonuses':[{'bonus':'MINE','uses':3}]}
        with patch('battleships.strategies.haki.HakiEndgameStrategy', wraps=HakiEndgameStrategy) as cls:
            HakiObservationBot().choose_action(state,150)
            self.assertGreater(cls.call_args.kwargs['spread'],0)

    def test_complete_games_without_sunk_do_not_exclude_live_cells(self):
        rules=Rules.for_modifier('SCHRODINGER_SHIP')
        for seed in range(30):
            board=Board(random_placement(rules,random.Random(seed)))
            s=HakiEndgameStrategy(rules,random.Random(seed+100),distance_uses=2)
            shots=set()
            for _ in range(100):
                kind,cell=s.next_action()
                if kind=='DISTANCE':
                    self.assertFalse(s.hits)
                    s.on_distance(cell,board.distance(cell))
                    continue
                self.assertNotIn(cell,shots)
                shots.add(cell)
                result=board.receive(cell)
                s.on_shot_result(cell,'HIT' if result=='SUNK' else result)
                live_unshot={c for c,n in board.owner.items() if board.afloat[n] and c not in shots}
                self.assertTrue(all(not ((s.empty|s.sunk) >> (r*10+c) & 1) for r,c in live_unshot))
                if board.destroyed:
                    break
            self.assertTrue(board.destroyed)


class SchrodingerWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_hidden_sinks_within_deadline(self):
        from battleships.worker import StrategyWorker
        worker=StrategyWorker('battleships.strategies.haki:HakiObservationBot')
        try:
            await worker.start()
            action=await worker.call('choose_action',(hidden_endgame(),150),.15)
            self.assertEqual(action.bonus,'DISTANCE')
        finally:
            await worker.close()
