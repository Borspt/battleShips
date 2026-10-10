import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from websockets.asyncio.server import serve

from battleships.client import run_bot
from battleships.haki_session import HAKI_STRATEGY, HakiSession
from battleships.strategy import BaselineStrategy
from tests.fixtures import started


class LocalWorker:
    """Тестируем транспорт и режимы без запуска процесса стратегии."""
    def __init__(self, strategy):
        self.bot = BaselineStrategy()

    async def start(self):
        pass

    async def close(self):
        pass

    async def call(self, method, args, timeout):
        return getattr(self.bot, method)(*args)


def match(opponent=None):
    msg = started('SPEEDRUN')
    msg['rules'].update(rows=1, columns=1, fleet=[{'length': 1, 'count': 1}])
    if opponent:
        msg['opponent'] = opponent
    return msg


class HakiSessionTests(unittest.TestCase):
    def test_three_wins_only_draws_and_duplicate_finishes_do_not_count(self):
        session = HakiSession('TRAINING', 'EASY')
        session.observe({'type': 'MODE_CHANGED', 'mode': 'TRAINING'})
        for outcome in ('WIN', 'DRAW', 'WIN', 'WIN'):
            session.observe(match())
            session.observe({'type': 'MATCH_FINISHED', 'outcome': outcome})
            wins = session.training_wins
            if wins < 3:
                session.observe({'type': 'MATCH_FINISHED', 'outcome': outcome})
                self.assertEqual(session.training_wins, wins)
        self.assertEqual(session.training_wins, 3)
        self.assertEqual(session.command(), {'type': 'CHANGE_MODE', 'mode': 'RANKING'})
        self.assertFalse(session.confirmed)

    def test_reconnect_keeps_training_progress_and_reconfirms_mode(self):
        session = HakiSession('TRAINING', 'EASY')
        session.observe({'type': 'MODE_CHANGED', 'mode': 'TRAINING'})
        session.observe(match())
        session.observe({'type': 'MATCH_FINISHED', 'outcome': 'WIN'})
        session.connected()
        self.assertEqual(session.training_wins, 1)
        self.assertFalse(session.confirmed)
        self.assertEqual(session.command()['mode'], 'TRAINING')
        session.mode = 'RANKING'
        session.connected()
        self.assertEqual(session.command()['mode'], 'RANKING')

    def test_wrong_level_or_unknown_outcome_stops(self):
        for opponent, outcome in [({'kind': 'BUILT_IN_BOT', 'builtInBot': 'HARD'}, 'WIN'),
                                   ({'kind': 'BUILT_IN_BOT', 'builtInBot': 'EASY'}, 'NEW')]:
            session = HakiSession('TRAINING', 'EASY')
            session.observe({'type': 'MODE_CHANGED', 'mode': 'TRAINING'})
            session.observe(match(opponent))
            self.assertFalse(session.should_stop)
            session.observe({'type': 'MATCH_FINISHED', 'outcome': outcome})
            self.assertTrue(session.should_stop)
            self.assertEqual(session.training_wins, 0)


class HakiTransportTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, scenario, timeout=30):
        errors, connections, commands = [], [], []

        async def read(ws):
            msg = json.loads(await asyncio.wait_for(ws.recv(), 2))
            commands.append(msg)
            return msg

        async def play(ws, outcome, remote=False):
            await ws.send(json.dumps(match({'kind': 'REMOTE_BOT'} if remote else None)))
            await ws.send(json.dumps({'type': 'TURN_REQUESTED', 'moveNumber': 1,
                                     'shotsLeft': 100, 'shotsLeftInTurn': 1}))
            self.assertEqual((await read(ws))['type'], 'SHOOT')
            await ws.send(json.dumps({'type': 'MATCH_FINISHED', 'outcome': outcome,
                                     'endReason': 'FLEET_DESTROYED'}))

        async def handler(ws):
            connections.append(1)
            try:
                self.assertEqual(await read(ws), {'type': 'CHANGE_MODE', 'mode': 'TRAINING', 'opponent': 'EASY'})
                if scenario == 'reject':
                    await ws.send(json.dumps({'type': 'MODE_REJECTED', 'mode': 'TRAINING', 'reason': 'UNKNOWN_MODE'}))
                elif scenario == 'timeout':
                    pass  # нет подтверждения; не считать это отключением тренировок
                else:
                    await ws.send(json.dumps({'type': 'MODE_CHANGED', 'mode': 'TRAINING'}))
                    if scenario == 'promote':
                        for _ in range(3):
                            await play(ws, 'WIN')
                        self.assertEqual(await read(ws), {'type': 'CHANGE_MODE', 'mode': 'RANKING'})
                        await ws.send(json.dumps({'type': 'MODE_CHANGED', 'mode': 'RANKING'}))
                        await play(ws, 'LOSS', remote=True)
                    elif scenario == 'race':
                        await play(ws, 'WIN', remote=True)
                    else:
                        await play(ws, 'LOSS')
                # Сообщения следующей игры, уже стоящие в очереди, не должны
                # запускать ещё одну партию после LOSS/отказа/несовпадения режима.
                if scenario not in {'timeout', 'reject'}:
                    await ws.send(json.dumps(match()))
                async for unexpected in ws:
                    errors.append(unexpected)
            except Exception as exc:
                errors.append(repr(exc))

        with tempfile.TemporaryDirectory() as temp:
            def session(mode, opponent):
                return HakiSession(mode, opponent, idle_timeout=timeout)
            with patch('battleships.client.StrategyWorker', LocalWorker), \
                 patch('battleships.client.HakiSession', session):
                async with serve(handler, '127.0.0.1', 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    # stop_on_loss намеренно не передан: у Хаки правило обязательно.
                    await asyncio.wait_for(run_bot(url=f'ws://127.0.0.1:{port}',
                        token='local-test-token', strategy=HAKI_STRATEGY, mode='TRAINING',
                        opponent='EASY', log_dir=temp, budget_ms=150, reserve_ms=250,
                        allow_bonuses=False), 10)
            records = [json.loads(line) for file in Path(temp).glob('*.jsonl')
                       for line in file.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(errors, [])
        self.assertEqual(len(connections), 1)
        return commands, records

    async def test_promotes_after_three_wins_then_first_loss_disconnects(self):
        commands, records = await self.run_case('promote')
        self.assertEqual(sum(c['type'] == 'SHOOT' for c in commands), 4)
        self.assertEqual(sum(r['event'] == 'stop_on_loss' for r in records), 1)
        self.assertEqual(sum(r['event'] == 'haki_training_passed' for r in records), 1)

    async def test_training_loss_never_promotes(self):
        commands, records = await self.run_case('loss')
        self.assertEqual(len(commands), 2)
        self.assertTrue(any(r['event'] == 'stop_on_loss' for r in records))

    async def test_rejection_and_idle_timeout_do_not_fall_back_to_ranking(self):
        for case in ('reject', 'timeout'):
            commands, records = await self.run_case(case, timeout=0.1)
            self.assertEqual(len(commands), 1)
            self.assertTrue(any(r['event'] == 'haki_session_stopped' for r in records))

    async def test_ranking_race_finishes_existing_match_then_stops(self):
        commands, records = await self.run_case('race')
        self.assertEqual(len(commands), 2)
        self.assertTrue(any(r['event'] == 'haki_session_stopped' and
                            r['data']['reason'] == 'UNEXPECTED_TRAINING_OPPONENT' for r in records))
