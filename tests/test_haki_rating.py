import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from websockets.asyncio.server import serve
from battleships.client import run_bot
from battleships.haki_rating import HakiRatingGate
from battleships.haki_session import HAKI_STRATEGY, HakiSession
from battleships.model import GameState
from battleships.strategies.haki import HakiObservationBot
from tests.fixtures import started
from tests.test_client import MemoryJournal
from tests.test_haki_session import LocalWorker, match


class RatingTests(unittest.IsolatedAsyncioTestCase):
    async def test_warmup_then_positive_gain_continues_and_zero_stops(self):
        values = iter([1500, 1490, 1480, 1505, 1500])
        calls = []
        def lookup(_):
            calls.append(1)
            return {'rating': next(values), 'matchCount': 9 + len(calls)}
        gate = HakiRatingGate('unused', MemoryJournal(), lookup=lookup)
        await gate.start()
        self.assertIsNone(await gate.after_match())
        self.assertIsNone(await gate.after_match())
        self.assertIsNone(await gate.after_match())
        self.assertEqual(await gate.after_match(), 'RATING_GAIN_EXHAUSTED')

    async def test_stale_rating_is_not_a_zero_gain_decision(self):
        samples = iter([{'rating':1500, 'matchCount':10},
                        {'rating':1500, 'matchCount':10},
                        {'rating':1510, 'matchCount':11}])
        gate = HakiRatingGate('unused', MemoryJournal(), warmup=1, lookup=lambda _: next(samples))
        await gate.start()
        self.assertIsNone(await gate.after_match())

    async def test_unavailable_rating_stops_instead_of_playing_blindly(self):
        gate = HakiRatingGate('unused', MemoryJournal(), lookup=lambda _: {'rating':1500,'matchCount':10})
        await gate.start()
        with patch.object(gate, 'settle_seconds', 0):
            self.assertEqual(await gate.after_match(), 'RATING_UNAVAILABLE')

    async def test_ranked_socket_continues_after_two_losses_and_stops_at_zero(self):
        commands, errors = [], []
        snapshots = iter([1500,1490,1495,1505,1500])
        calls = []
        async def gate_start(gate):
            gate.lookup = lookup
            await original_start(gate)
        def lookup(_):
            calls.append(1)
            return {'rating':next(snapshots),'matchCount':100+len(calls)}
        original_start = HakiRatingGate.start
        async def server_handler(ws):
            try:
                commands.append(json.loads(await ws.recv()))
                await ws.send(json.dumps({'type':'MODE_CHANGED','mode':'RANKING'}))
                for index, outcome in enumerate(('LOSS','LOSS','WIN','LOSS')):
                    await ws.send(json.dumps(match({'kind':'REMOTE_BOT'})))
                    await ws.send(json.dumps({'type':'TURN_REQUESTED','moveNumber':1,
                        'shotsLeft':20,'shotsLeftInTurn':1}))
                    commands.append(json.loads(await ws.recv()))
                    await ws.send(json.dumps({'type':'MATCH_FINISHED','outcome':outcome,
                        'endReason':'SHOT_LIMIT'}))
                await ws.send(json.dumps(match()))
                async for unexpected in ws:
                    errors.append(unexpected)
            except Exception as exc:
                errors.append(type(exc).__name__)
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'HAKI_STOP_POLICY':'RATING_GAIN','HAKI_RATING_WARMUP':'3'}), \
                 patch('battleships.client.StrategyWorker', LocalWorker), \
                 patch.object(HakiRatingGate, 'start', gate_start):
                async with serve(server_handler,'127.0.0.1',0) as server:
                    await asyncio.wait_for(run_bot(url=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}',
                        token='test',strategy=HAKI_STRATEGY,mode='RANKING',opponent='EASY',
                        log_dir=directory,budget_ms=150,reserve_ms=250,allow_bonuses=False),10)
        self.assertEqual(errors, [])
        self.assertEqual(sum(c['type']=='SHOOT' for c in commands),4)

    def test_short_hunger_game_conserves_shots(self):
        message = started('HUNGER_GAMES')
        message['rules']['shotLimitPerPlayer'] = 20
        state = GameState.from_message(message)
        state.bonuses['DISTANCE'] = 3
        state.shots_left = 5
        bot = HakiObservationBot()
        self.assertEqual(state.rules.shot_limit_per_player,20)
        self.assertEqual(bot._distance_budget(state,0),0)
