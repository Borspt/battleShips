import copy
from datetime import datetime, timedelta, timezone
import itertools
import json
from pathlib import Path
import random
import unittest
import tempfile
from types import SimpleNamespace

from battleships import model
from battleships.placement import random_placement
from battleships.rules import Rules
from battleships.strategies.adaptation import Plan
from battleships.strategies.haki import HakiEndgameStrategy, HakiObservationBot
from battleships.strategies.haki_memory import recent_records, adapt_recent, HakiBook


class MemoryTest(unittest.TestCase):
    def test_no_approval_or_mismatched_archive_keeps_attack_unchanged(self):
        base=Plan(random_placement,None,'baseline')
        now=datetime.now(timezone.utc).isoformat()
        records=[dict(botId='a',matchId=str(i),modifier='DUEL',kind='RANKING',
                      complete=True,at=now,ships=random_placement(Rules(),random.Random(i)))
                 for i in range(8)]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'matches.json'
            path.write_text(json.dumps(records),encoding='utf-8')
            for policy in ({}, {'dataHash':'wrong','policies':{'a:DUEL':{'approved':True}}}):
                path.with_name('policies.json').write_text(json.dumps(policy),encoding='utf-8')
                plan=HakiBook(path).adapt(base,SimpleNamespace(bot_id='a'), 'DUEL')
                self.assertIsNone(plan.edge_bias)
                self.assertIs(plan.placement,random_placement)

    def test_filters_mode_identity_age_training_incomplete_and_duplicates(self):
        now = datetime(2026, 10, 10, 12, 30, tzinfo=timezone.utc)
        r = dict(botId='a', matchId='one', modifier='DUEL', kind='RANKING',
                 complete=True, at=(now-timedelta(minutes=1)).isoformat(),
                 ships=random_placement(Rules(), random.Random(1)))
        invalid = [dict(r, botId='b'), dict(r, modifier='THREE_MUSKETEERS'),
                   dict(r, kind='TRAINING'), dict(r, complete=False),
                   dict(r, at=(now-timedelta(hours=3)).isoformat()),
                   dict(r, at=(now+timedelta(minutes=1)).isoformat())]
        self.assertEqual(recent_records([r, r]+invalid, 'a', 'DUEL', now), [r])

    def test_sparse_data_keeps_base_and_only_haki_bias_changes(self):
        base = Plan(random_placement, None, 'baseline')
        ships = random_placement(Rules(), random.Random(12))
        records = [dict(ships=ships, outcome='WIN') for _ in range(8)]
        self.assertIs(adapt_recent(base, records[:7]), base)
        changed = adapt_recent(base, records)
        self.assertIs(changed.placement, base.placement)
        self.assertIsNone(base.edge_bias)
        self.assertGreaterEqual(changed.edge_bias.single_edge, .75)
        self.assertLessEqual(changed.edge_bias.single_edge, 2)


class EarlierDistanceTest(unittest.TestCase):
    def test_three_and_four_singletons_match_exhaustive_oracle(self):
        for count in (3, 4):
            rules = Rules(rows=3, columns=5, fleet=(1,)*count)
            shooter = HakiEndgameStrategy(rules, search_ms=500)
            shooter.on_distance((0, 0), 1)
            configs = shooter._endgame_configs() or []
            actual = {mask for mask, weight, chosen in configs}
            expected = set()
            for cells in itertools.combinations(range(15), count):
                pairs = [divmod(i,5) for i in cells]
                if min(r+c for r,c in pairs) != 1:
                    continue
                if any(max(abs(a[0]-b[0]),abs(a[1]-b[1])) <= 1
                       for a,b in itertools.combinations(pairs,2)):
                    continue
                expected.add(sum(1 << i for i in cells))
            self.assertEqual(actual,expected)

    def test_last_loss_now_requests_bonus_with_three_ships(self):
        fixture=json.loads(Path('tests/haki_three_singletons.json').read_text())
        state=model.GameState.from_message(fixture)
        action=HakiObservationBot().choose_action(state,150)
        self.assertEqual(action.bonus,'DISTANCE')
        self.assertTrue(model.valid_action(action,state,allow_bonuses=True))

    def test_limit_discards_partial_configs(self):
        shooter=HakiEndgameStrategy(Rules(rows=4,columns=5,fleet=(1,1,1)))
        shooter.MAX_NODES=1
        self.assertIsNone(shooter._endgame_configs())
        self.assertEqual(shooter.next_action()[0],'SHOOT')

    def test_new_reading_after_sinking_uses_remaining_fleet(self):
        shooter=HakiEndgameStrategy(Rules(rows=5,columns=5,fleet=(1,1,1,1)))
        shooter.on_shot_result((0,0),'SUNK')
        shooter.on_distance((4,4),0)
        configs=shooter._endgame_configs()
        self.assertTrue(configs)
        self.assertTrue(all(mask & (1 << 24) for mask,_,_ in configs))


class EarlierDistanceWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_position_within_worker_deadline(self):
        from battleships.worker import StrategyWorker
        fixture=json.loads(Path('tests/haki_three_singletons.json').read_text())
        worker=StrategyWorker('battleships.strategies.haki:HakiObservationBot')
        try:
            await worker.start()
            action=await worker.call('choose_action',(model.GameState.from_message(fixture),150),.15)
            self.assertEqual(action.bonus,'DISTANCE')
        finally:
            await worker.close()
