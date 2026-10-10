import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from websockets.asyncio.server import serve

from battleships.client import run_bot, session_messages
from battleships.haki_session import HAKI_STRATEGY, HakiSession
from battleships.haki_transport import HakiController, HakiTransportStopped
from battleships.model import Action, Cell
from tests.fixtures import started
from tests.test_client import FakeWorker, MemoryJournal


class HakiTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = []
        async def send(raw):
            self.sent.append(json.loads(raw))
        self.worker = FakeWorker(Action.shoot(Cell(6, 1)))
        self.log = MemoryJournal()
        self.client = HakiController(send, self.worker, self.log)
        await self.client.handle(started("SPEEDRUN"))

    async def turn(self, move):
        await self.client.handle({"type": "TURN_REQUESTED", "moveNumber": move,
            "shotsLeft": 100 - move, "shotsLeftInTurn": 1})

    async def resolved(self, cell):
        await self.client.handle({"type": "SHOT_RESOLVED", "cell": cell.wire(), "result": "MISS"})

    async def test_recorded_timeout_sequence_never_pipelines_b9(self):
        await self.turn(53)  # B7
        await self.client.handle({"type": "TURN_TIMED_OUT", "moveNumber": 53})
        self.worker.action = Action.shoot(Cell(8, 1))  # formerly sent B9
        await self.turn(55)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.client.pending.cell, Cell(6, 1))
        await self.resolved(Cell(6, 1))
        self.assertEqual(len(self.sent), 1)  # Never replay turn 55.
        await self.turn(55)
        self.assertEqual(len(self.sent), 1)
        self.worker.action = Action.shoot(Cell(8, 3))
        await self.turn(57)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.client.pending.cell, Cell(8, 3))
        await self.resolved(Cell(6, 1))  # Duplicate must not clear D9.
        self.assertEqual(self.client.pending.cell, Cell(8, 3))
        self.assertEqual(len(self.client.state.shots), 1)
        await self.resolved(Cell(8, 3))
        self.assertIsNone(self.client.pending)
        self.assertTrue(any(e == "haki_result_timing" for e, _ in self.log.events))

    async def test_unmatched_rejection_preserves_pending(self):
        await self.turn(1)
        await self.client.handle({"type": "SHOT_REJECTED", "cell": Cell(8, 3).wire(),
                                  "reason": "NOT_YOUR_TURN"})
        self.assertEqual(self.client.pending.cell, Cell(6, 1))

    async def test_normal_success_and_duplicate_request(self):
        await self.turn(1)
        await self.turn(1)
        self.assertEqual(len(self.sent), 1)
        await self.resolved(Cell(6, 1))
        await self.turn(2)
        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(self.sent[-1]["cell"], Cell(6, 1).wire())

    async def test_unresolved_action_stops_even_without_messages(self):
        class SilentSocket:
            async def recv(self):
                await asyncio.Future()
        await self.turn(1)
        self.client.resync_seconds = 0.01
        await self.client.handle({"type": "TURN_TIMED_OUT", "moveNumber": 1})
        session = HakiSession("RANKING", "EASY")
        session.confirmed = True
        session.active = True
        with self.assertRaises(HakiTransportStopped):
            await asyncio.wait_for(anext(session_messages(SilentSocket(), session, self.client)), 1)

    async def test_ambiguous_rejection_stops_during_resync(self):
        await self.turn(1)
        await self.client.handle({"type": "TURN_TIMED_OUT", "moveNumber": 1})
        with self.assertRaises(HakiTransportStopped):
            await self.client.handle({"type": "COMMAND_REJECTED", "reason": "NOT_YOUR_TURN"})

    async def test_matching_bonus_after_timeout_retains_requested_cell(self):
        self.client.allow_bonuses = True
        self.client.state.bonuses["DISTANCE"] = 2
        self.worker.action = Action("USE_BONUS", Cell(4, 4), "DISTANCE")
        await self.turn(1)
        await self.client.handle({"type": "TURN_TIMED_OUT", "moveNumber": 1})
        await self.turn(3)
        await self.client.handle({"type": "BONUS_RESOLVED", "bonus": "DISTANCE", "distance": 2})
        self.assertEqual(self.client.state.bonus_results[-1]["requestedCell"], Cell(4, 4).wire())
        self.assertIsNone(self.client.resync_deadline)
        self.assertEqual(len(self.sent), 1)

    async def test_stop_file_waits_for_current_match(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stop.request"
            self.client.stop_file = str(path)
            path.touch()
            self.assertTrue(self.client.stop_requested())
            self.assertFalse(self.client.idle_stop_requested())
            await self.client.handle({"type": "MATCH_FINISHED", "outcome": "WIN"})
            self.assertTrue(self.client.idle_stop_requested())

    async def test_terminal_bonus_rejection_allows_fresh_turn(self):
        self.client.allow_bonuses = True
        self.client.state.bonuses["DISTANCE"] = 2
        self.worker.action = Action("USE_BONUS", Cell(4, 4), "DISTANCE")
        await self.turn(1)
        await self.client.handle({"type": "BONUS_REJECTED", "bonus": "DISTANCE", "reason": "NOT_YOUR_TURN"})
        self.assertIsNone(self.client.pending)
        self.worker.action = Action.shoot(Cell(6, 1))
        await self.turn(3)
        self.assertEqual(len(self.sent), 2)
        self.assertIsNone(self.client.resync_deadline)

    async def test_idle_stop_file_interrupts_silent_receive(self):
        class SilentSocket:
            async def recv(self):
                await asyncio.Future()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stop.request"
            self.client.stop_file = str(path)
            self.client.state.finished = True
            path.touch()
            with self.assertRaisesRegex(HakiTransportStopped, "OPERATOR_STOP"):
                await anext(session_messages(SilentSocket(), HakiSession("RANKING", "EASY"), self.client))

    async def test_terminal_resync_does_not_reconnect_real_websocket(self):
        connections = []
        async def handler(ws):
            connections.append(True)
            await ws.recv()
            await ws.send(json.dumps({"type": "MODE_CHANGED", "mode": "RANKING"}))
            await ws.send(json.dumps(started("SPEEDRUN")))
            await ws.send(json.dumps({"type": "TURN_REQUESTED", "moveNumber": 1,
                "shotsLeft": 100, "shotsLeftInTurn": 1}))
            await ws.recv()
            await ws.send(json.dumps({"type": "TURN_TIMED_OUT", "moveNumber": 1}))
            await ws.wait_closed()
        with tempfile.TemporaryDirectory() as directory:
            async with serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                await asyncio.wait_for(run_bot(url=f"ws://127.0.0.1:{port}", token="test-only",
                    strategy=HAKI_STRATEGY, mode="RANKING", opponent="EASY", log_dir=directory,
                    budget_ms=150, reserve_ms=250, allow_bonuses=False), 8)
            records = [json.loads(line) for path in Path(directory).glob("*.jsonl")
                       for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(connections), 1)
        self.assertTrue(any(r["event"] == "haki_session_stopped"
            and r["data"]["reason"] == "UNRESOLVED_ACTION" for r in records))

    async def test_operator_stop_finishes_match_without_starting_queued_next_match(self):
        connections, extra_commands = [], []
        with tempfile.TemporaryDirectory() as directory:
            stop = Path(directory) / "stop.request"
            async def handler(ws):
                connections.append(True)
                await ws.recv()
                await ws.send(json.dumps({"type": "MODE_CHANGED", "mode": "RANKING"}))
                await ws.send(json.dumps(started("SPEEDRUN")))
                await ws.send(json.dumps({"type": "TURN_REQUESTED", "moveNumber": 1,
                    "shotsLeft": 100, "shotsLeftInTurn": 1}))
                command = json.loads(await ws.recv())
                stop.touch()
                await ws.send(json.dumps({"type": "SHOT_RESOLVED", "cell": command["cell"], "result": "MISS"}))
                await ws.send(json.dumps({"type": "MATCH_FINISHED", "outcome": "WIN", "endReason": "FLEET_DESTROYED"}))
                await ws.send(json.dumps(started("SPEEDRUN")))
                await ws.send(json.dumps({"type": "TURN_REQUESTED", "moveNumber": 1,
                    "shotsLeft": 100, "shotsLeftInTurn": 1}))
                async for command in ws:
                    extra_commands.append(command)
            with patch.dict("os.environ", {"HAKI_STOP_FILE": str(stop)}):
                async with serve(handler, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    await asyncio.wait_for(run_bot(url=f"ws://127.0.0.1:{port}", token="test-only",
                        strategy=HAKI_STRATEGY, mode="RANKING", opponent="EASY", log_dir=directory,
                        budget_ms=150, reserve_ms=250, allow_bonuses=False), 8)
            records = [json.loads(line) for path in Path(directory).glob("*.jsonl")
                       for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(connections), 1)
        self.assertEqual(extra_commands, [])
        self.assertTrue(any(r["event"] == "haki_session_stopped"
            and r["data"]["reason"] == "OPERATOR_STOP" for r in records))
