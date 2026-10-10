import json
from pathlib import Path
import random
import unittest
from unittest.mock import patch

from battleships import model
from battleships.placement import random_placement
from battleships.rules import Rules
from battleships.simulator import Board
from battleships.strategies.haki import HakiEndgameStrategy, LegacyHakiObservationBot


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
        action=LegacyHakiObservationBot().choose_action(state,150)
        self.assertEqual(action.bonus,'DISTANCE')
        self.assertTrue(model.valid_action(action,state,allow_bonuses=True))

    def test_ambiguous_hit_does_not_trigger_distance(self):
        s=HakiEndgameStrategy(Rules(rows=5,columns=5,fleet=(2,1),modifier='SCHRODINGER_SHIP'),
                             distance_uses=2)
        s.on_shot_result((2,2),'HIT')
        self.assertTrue(s.hits)
        self.assertEqual(s.next_action()[0],'SHOOT')

    def test_single_hit_is_not_declared_sunk_with_one_possible_extension(self):
        s=HakiEndgameStrategy(Rules(rows=5,columns=5,fleet=(2,1),modifier='SCHRODINGER_SHIP'))
        s.on_shot_result((2,2),'HIT')
        for cell in ((1,2),(3,2),(2,1)):
            s.on_shot_result(cell,'MISS')
        self.assertTrue(s.hits)
        self.assertEqual(sorted(s.remaining),[1,2])
        s.on_shot_result((2,3),'MISS')
        self.assertFalse(s.hits)
        self.assertEqual(s.remaining,[2])

    def test_hidden_singleton_proved_when_free_extensions_cannot_fit_fleet(self):
        s=HakiEndgameStrategy(Rules(rows=5,columns=5,fleet=(4,1),modifier='SCHRODINGER_SHIP'))
        s.on_shot_result((2,2),'HIT')
        for cell in ((1,2),(3,2),(2,0)):
            s.on_shot_result(cell,'MISS')
        # A length-four ship still fits from column 1 through column 4.
        self.assertTrue(s.hits)
        s.on_shot_result((2,4),'MISS')
        # The two immediate horizontal neighbours remain unshot, but neither
        # direction can accommodate the only longer length left in the fleet.
        self.assertFalse(s.hits)
        self.assertEqual(s.remaining,[4])

    def test_geometry_proof_preserves_ship_with_hits_in_its_middle(self):
        s=HakiEndgameStrategy(Rules(rows=5,columns=5,fleet=(3,1),modifier='SCHRODINGER_SHIP'))
        s.on_shot_result((2,2),'HIT')
        for cell in ((1,2),(3,2),(2,0),(2,4)):
            s.on_shot_result(cell,'MISS')
        # Three cells 2,1--2,3 DO fit around the middle hit. This remains
        # ambiguous despite both two-cell extensions from the hit being blocked.
        self.assertTrue(s.hits)

    def test_geometry_proof_does_not_infer_in_announced_or_touching_games(self):
        for modifier,no_touching in ((None,True),('SCHRODINGER_SHIP',False)):
            with self.subTest(modifier=modifier,no_touching=no_touching):
                s=HakiEndgameStrategy(Rules(rows=5,columns=5,fleet=(4,1),
                                           modifier=modifier,no_touching=no_touching))
                s.on_shot_result((2,2),'HIT')
                for cell in ((1,2),(3,2),(2,0),(2,4)):
                    s.on_shot_result(cell,'MISS')
                self.assertTrue(s.hits)
                self.assertEqual(s.remaining,[4,1])

    def test_hidden_endgame_distance_constraints_keep_actual_live_fleet(self):
        rules=Rules(rows=5,columns=5,fleet=(2,1,1),modifier='SCHRODINGER_SHIP')
        ships=[[(0,0),(0,1)],[(2,2)],[(4,4)]]
        board=Board(ships)
        s=HakiEndgameStrategy(rules,random.Random(71),distance_uses=2)
        for cell in ((0,0),(0,1),(0,2)):
            result=board.receive(cell)
            s.on_shot_result(cell,'HIT' if result=='SUNK' else result)
        self.assertFalse(s.hits)
        s.on_distance((0,0),board.distance((0,0)))
        expected=(1 << 12) | (1 << 24)
        configs=s._endgame_configs()
        self.assertTrue(configs)
        self.assertIn(expected,[mask for mask,_,_ in configs])
        # The closest ship then sinks. Its old reading must not require a new
        # ship at the same distance or exclude the actual surviving singleton.
        for cell in ((2,2),(1,2),(3,2),(2,1),(2,3)):
            result=board.receive(cell)
            s.on_shot_result(cell,'HIT' if result=='SUNK' else result)
        self.assertFalse(s.hits)
        configs=s._endgame_configs()
        self.assertTrue(configs)
        self.assertIn(1 << 24,[mask for mask,_,_ in configs])

    def test_haki_passes_opponent_mines_to_shared_spread(self):
        state=hidden_endgame()
        state.opponent={'bonuses':[{'bonus':'MINE','uses':3}]}
        with patch('battleships.strategies.haki.HakiEndgameStrategy', wraps=HakiEndgameStrategy) as cls:
            LegacyHakiObservationBot().choose_action(state,150)
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
        worker=StrategyWorker('battleships.strategies.haki:LegacyHakiObservationBot')
        try:
            await worker.start()
            action=await worker.call('choose_action',(hidden_endgame(),150),.15)
            self.assertEqual(action.bonus,'DISTANCE')
        finally:
            await worker.close()
