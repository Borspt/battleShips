import asyncio
import io
import json
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from battleships.client import Controller
from battleships.model import Cell, GameState, OpponentInfo, valid_placement
from battleships.opponents import OpponentDirectory
from battleships.worker import StrategyWorker
from tests.fixtures import started
from tests.test_client import FakeWorker, MemoryJournal

BOT_ID = "3f2b8c1e-6a4d-4f0b-9c5e-2d7a1b8e4c90"


def remote_start(modifier=None):
    msg = started(modifier)
    msg["opponent"] = {"kind": "REMOTE_BOT", "botId": BOT_ID, "teamName": "Kraken"}
    return msg


class DirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_statistics_request_and_exact_elo(self):
        directory = OpponentDirectory("wss://platform.example/api/v1/bots/connection")
        info = GameState.from_message(remote_start()).opponent_info
        with patch("battleships.opponents.urlopen", return_value=io.BytesIO(
                json.dumps({"value": {"rating": 1642.375}, "error": None}).encode())) as request:
            result = await directory.lookup(info)
        self.assertEqual(result.rating, 1642.375)
        self.assertEqual(result.bot_id, BOT_ID)
        self.assertEqual(result.rating_source, "bot_statistics")
        self.assertIsNotNone(result.rating_observed_at)
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, f"https://platform.example/api/v1/bots/{BOT_ID}/statistics?kind=ALL")
        self.assertIsNone(sent.get_header("Authorization"))

    async def test_invalid_rating_is_not_used(self):
        info = GameState.from_message(remote_start()).opponent_info
        for rating in (None, True, "1500", float("nan"), float("inf")):
            with self.subTest(rating=rating), patch("battleships.opponents.urlopen", return_value=io.BytesIO(
                    json.dumps({"value": {"rating": rating}, "error": None}).encode())):
                with self.assertRaises(ValueError):
                    await OpponentDirectory("wss://platform.example").lookup(info)

    async def test_builtin_identity_and_fresh_file_only(self):
        info = GameState.from_message(started()).opponent_info
        self.assertEqual(info.key, "builtin:EASY")
        self.assertIsNone(info.bot_id)
        self.assertIsNone((await OpponentDirectory("wss://platform.example").lookup(info)).rating)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ratings.json"
            directory = OpponentDirectory("wss://platform.example", str(path))
            for age, expected in ((5, 998.5), (61, None), (-60, None)):
                stamp = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
                path.write_text(json.dumps({"builtin:EASY": {"rating": 998.5, "observed_at": stamp}}), encoding="utf-8")
                self.assertEqual((await directory.lookup(info)).rating, expected)


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = []
        async def send(raw):
            self.sent.append(json.loads(raw))
        self.journal = MemoryJournal()
        self.client = Controller(send, FakeWorker(), self.journal)

    async def asyncTearDown(self):
        await self.client.close()

    async def test_context_reaches_real_strategy_process_before_placement_and_shot(self):
        class Directory:
            async def lookup(self, info):
                return replace(info, rating=1642.375, rating_source="test")
        worker = StrategyWorker("tests.fixtures:ContextStrategy")
        await worker.start()
        self.client.worker = worker
        self.client.opponent_directory = Directory()
        try:
            await self.client.handle(remote_start())
            ships = [[Cell.parse(c) for c in s["cells"]] for s in self.sent[-1]["ships"]]
            self.assertTrue(valid_placement(ships, self.client.state.rules))
            # Hook puts the single-cell ship at a known corner.
            self.assertIn([Cell(9, 9)], ships)
            await self.client.handle({"type": "TURN_REQUESTED", "moveNumber": 1,
                                      "shotsLeft": 100, "shotsLeftInTurn": 1})
            self.assertEqual(self.sent[-1]["cell"], {"row": 4, "column": 5})
            self.assertFalse(any("fallback" in event for event, _ in self.journal.events))
            await self.client.handle({"type": "MATCH_FINISHED", "outcome": "WIN", "endReason": "FLEET_DESTROYED"})
            summary = next(data for event, data in self.journal.events if event == "match_summary")
            self.assertEqual(summary["opponent"]["key"], f"bot:{BOT_ID}")
            self.assertEqual(summary["opponent"]["rating"], 1642.375)
        finally:
            await worker.close()

    async def test_slow_lookup_does_not_block_speedrun_or_leak_to_next_match(self):
        release = asyncio.Event()
        class Directory:
            async def lookup(self, info):
                await release.wait()
                return replace(info, rating=9999)
        self.client.opponent_directory = Directory()
        # Проверяем ограничение ожидания и незавершённый lookup, а не скорость
        # планировщика Windows CI: нагруженный runner может проснуться позже.
        with patch("battleships.client.asyncio.wait", wraps=asyncio.wait) as wait:
            await asyncio.wait_for(self.client.handle(remote_start("SPEEDRUN")), timeout=5)
        self.assertEqual(wait.call_count, 1)
        self.assertEqual(wait.call_args.kwargs["timeout"], 0.1)
        self.assertFalse(self.client.opponent_task.done())
        self.assertFalse(release.is_set())
        self.assertIsNone(self.client.state.opponent_info.rating)
        await self.client.handle(started("SPEEDRUN"))
        release.set()
        await self.client.opponent_task
        self.assertEqual(self.client.state.opponent_info.key, "builtin:EASY")
        self.assertIsNone(self.client.state.opponent_info.bot_id)

    async def test_snapshot_without_identity_does_not_reuse_previous_opponent(self):
        await self.client.handle(remote_start("SPEEDRUN"))
        snapshot = started("SPEEDRUN")
        snapshot.pop("opponent")
        snapshot.update(type="MATCH_SNAPSHOT", placementWindowLeftSeconds=None)
        await self.client.handle(snapshot)
        self.assertEqual(self.client.state.opponent_info, OpponentInfo())
        self.assertEqual(self.sent, [])

    async def test_short_placement_window_does_not_wait_for_rating(self):
        class Directory:
            async def lookup(self, info):
                await asyncio.sleep(10)
                return info
        self.client.opponent_directory = Directory()
        msg = remote_start()
        msg["rules"]["placementWindowSeconds"] = 1
        before = time.monotonic()
        await self.client.handle(msg)
        self.assertLess(time.monotonic() - before, 0.5)
        self.assertEqual(self.sent[-1]["type"], "PLACE")
        self.assertIsNone(self.client.state.opponent_info.rating)

    async def test_failed_lookup_still_places_and_keeps_identity(self):
        class Directory:
            async def lookup(self, info):
                raise TimeoutError()
        self.client.opponent_directory = Directory()
        await self.client.handle(remote_start())
        self.assertEqual(self.sent[-1]["type"], "PLACE")
        self.assertEqual(self.client.state.opponent_info.bot_id, BOT_ID)
        self.assertIsNone(self.client.state.opponent_info.rating)
        self.assertIn("opponent_lookup_failed", [event for event, _ in self.journal.events])
