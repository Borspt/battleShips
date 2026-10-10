import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from websockets.asyncio.server import serve

from battleships.client import Controller, Journal, run_bot
from battleships.model import Action, Cell, GameState, Rules, valid_placement
from battleships.strategy import BaselineStrategy
from battleships.worker import StrategyWorker
from tests.fixtures import started


class MemoryJournal:
    def __init__(self):
        self.events = []
    def write(self, event, data=None):
        self.events.append((event, data))


class FakeWorker:
    def __init__(self, action=None):
        self.action = action
    async def call(self, method, args, timeout):
        if method == "place_ships":
            return BaselineStrategy().place_ships(args[0])
        if method == "place_ships_for_match":
            return BaselineStrategy().place_ships(args[0].rules)
        return self.action


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = []
        async def send(raw):
            self.sent.append(json.loads(raw))
        self.log = MemoryJournal()
        self.worker = FakeWorker(Action.shoot(Cell(0, 0)))
        self.client = Controller(send, self.worker, self.log)

    async def turn(self, move=1, shots=100):
        await self.client.handle({"type": "TURN_REQUESTED", "moveNumber": move,
                                  "shotsLeft": shots, "shotsLeftInTurn": 1})

    async def test_full_state_machine_and_no_unsolicited_shots(self):
        await self.client.handle(started())
        self.assertEqual(self.sent[-1]["type"], "PLACE")
        self.assertIsNone(self.client.state.ships)
        await self.client.handle({"type": "PLACEMENT_ACCEPTED"})
        self.assertTrue(valid_placement(self.client.state.ships, self.client.state.rules))
        await self.turn()
        count = len(self.sent)
        await self.client.handle({"type": "SHOT_RESOLVED", "cell": {"row": 0, "column": 0}, "result": "HIT"})
        self.assertEqual(len(self.sent), count)
        await self.turn(move=2, shots=99)
        self.assertNotEqual(self.sent[-1]["cell"], {"row": 0, "column": 0})
        await self.client.handle({"type": "MATCH_FINISHED", "outcome": "WIN", "endReason": "FLEET_DESTROYED"})
        count = len(self.sent)
        await self.turn(move=3, shots=98)
        self.assertEqual(len(self.sent), count)

    async def test_speedrun_no_place_and_duplicate_turn(self):
        await self.client.handle(started("SPEEDRUN"))
        self.assertEqual(self.sent, [])
        await self.turn()
        await self.turn()
        self.assertEqual(len(self.sent), 1)

    async def test_bonus_rejection_immediately_falls_back(self):
        self.client.allow_bonuses = True
        self.worker.action = Action("USE_BONUS", Cell(4, 4), "SONAR")
        msg = started("SPEEDRUN")
        msg["bonuses"] = [{"bonus": "SONAR", "uses": 1}]
        await self.client.handle(msg)
        await self.turn()
        self.assertEqual(self.sent[-1]["type"], "USE_BONUS")
        await self.client.handle({"type": "BONUS_REJECTED", "bonus": "SONAR", "reason": "BONUS_NOT_AVAILABLE"})
        self.assertEqual(self.sent[-1]["type"], "SHOOT")
        self.assertEqual(self.client.state.bonuses["SONAR"], 1)

    async def test_successful_bonus_waits_for_request_and_tracks_cell(self):
        self.client.allow_bonuses = True
        self.worker.action = Action("USE_BONUS", Cell(4, 4), "SONAR")
        msg = started("SPEEDRUN")
        msg["bonuses"] = [{"bonus": "SONAR", "uses": 1}]
        await self.client.handle(msg)
        await self.turn()
        await self.client.handle({"type": "BONUS_RESOLVED", "bonus": "SONAR", "count": 2})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.client.state.bonus_results[0]["requestedCell"], {"row": 4, "column": 4})
        await self.turn(shots=99)
        self.assertEqual(self.sent[-1]["type"], "SHOOT")

    async def test_snapshot_does_not_invent_turn_or_placement(self):
        msg = started()
        msg.update(type="MATCH_SNAPSHOT", placementWindowLeftSeconds=None,
                   shots=[{"cell": {"row": 0, "column": 0}, "result": "MISS"}], shotsLeft=99)
        await self.client.handle(msg)
        self.assertEqual(self.sent, [])
        await self.turn(shots=99)
        self.assertNotEqual(self.sent[-1]["cell"], {"row": 0, "column": 0})

    async def test_salvo_new_request_can_have_same_counters(self):
        self.client.allow_bonuses = True
        self.worker.action = Action("USE_BONUS", bonus="SALVO")
        msg = started("THREE_MUSKETEERS")
        msg["bonuses"] = [{"bonus": "SALVO", "uses": 1}]
        msg["rules"]["shotsPerTurn"] = 3
        await self.client.handle(msg)
        turn = {"type": "TURN_REQUESTED", "moveNumber": 1, "shotsLeft": 100, "shotsLeftInTurn": 3}
        await self.client.handle(turn)
        self.assertEqual(self.sent[-1]["type"], "USE_BONUS")
        await self.client.handle({"type": "BONUS_RESOLVED", "bonus": "SALVO", "shotsInTurn": 3})
        await self.client.handle(turn)
        self.assertEqual(self.sent[-1]["type"], "SHOOT")

    async def test_turn_timeout_prevents_rejection_retry(self):
        await self.client.handle(started("SPEEDRUN"))
        await self.turn()
        await self.client.handle({"type": "TURN_TIMED_OUT", "moveNumber": 1})
        count = len(self.sent)
        await self.client.handle({"type": "COMMAND_REJECTED", "reason": "INVALID_COMMAND"})
        self.assertEqual(len(self.sent), count)

    async def test_each_documented_modifier_uses_server_request(self):
        for modifier in ("DUEL", "THREE_MUSKETEERS", "SCHRODINGER_SHIP", "HUNGER_GAMES", "BIRD_BOX", "OLD_SCHOOL"):
            msg = started(modifier)
            if modifier == "HUNGER_GAMES":
                msg["rules"].update(shotLimitPerPlayer=40, matchDurationSeconds=90)
            await self.client.handle(msg)
            self.assertEqual(self.sent[-1]["type"], "PLACE")
            count = len(self.sent)
            await self.turn(shots=msg["rules"]["shotLimitPerPlayer"])
            self.assertEqual(len(self.sent), count + 1)
            self.assertEqual(self.sent[-1]["type"], "SHOOT")

    async def test_unknown_message_and_betting_are_ignored(self):
        await self.client.handle({"type": "FUTURE_MESSAGE", "data": 5})
        await self.client.handle({"type": "BET_REQUESTED", "maxAmount": 40})
        self.assertEqual(self.sent, [])

    async def test_late_action_is_not_sent(self):
        await self.client.handle(started("SPEEDRUN"))
        self.client.turn_deadline = time.monotonic() - 1
        await self.client.act()
        self.assertEqual(self.sent, [])

    async def test_hanging_strategy_is_terminated_and_fallback_sent(self):
        worker = StrategyWorker("tests.fixtures:SlowStrategy")
        await worker.start()
        self.client.worker = worker
        self.client.budget_ms = 50
        try:
            await self.client.handle(started("SPEEDRUN"))
            start = time.monotonic()
            await self.turn()
            self.assertLess(time.monotonic() - start, 0.75)
            self.assertEqual(self.sent[-1]["type"], "SHOOT")
            self.assertIsNone(worker.process)
        finally:
            await worker.close()

    async def test_crashing_strategy_fallback(self):
        worker = StrategyWorker("tests.fixtures:CrashingStrategy")
        await worker.start()
        self.client.worker = worker
        try:
            await self.client.handle(started("SPEEDRUN"))
            await self.turn()
            self.assertEqual(self.sent[-1]["type"], "SHOOT")
        finally:
            await worker.close()


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_clients_work_concurrently(self):
        errors = []
        finished = []
        async def handler(ws):
            try:
                await ws.recv()
                msg = started("SPEEDRUN")
                msg["rules"].update(rows=1, columns=1, fleet=[{"length": 1, "count": 1}])
                await ws.send(json.dumps(msg))
                await ws.send(json.dumps({"type": "TURN_REQUESTED", "moveNumber": 1,
                                          "shotsLeft": 100, "shotsLeftInTurn": 1}))
                command = json.loads(await asyncio.wait_for(ws.recv(), 1))
                self.assertEqual(command, {"type": "SHOOT", "cell": {"row": 0, "column": 0}})
                await ws.send(json.dumps({"type": "SHOT_RESOLVED", "cell": command["cell"], "result": "SUNK"}))
                await ws.send(json.dumps({"type": "MATCH_FINISHED", "outcome": "WIN", "endReason": "SPEEDRUN_FINISHED"}))
                finished.append(ws.request.headers["Authorization"])
                await ws.wait_closed()
            except Exception as exc:
                errors.append(exc)
                await ws.close(code=4004)
        with tempfile.TemporaryDirectory() as temp:
            async with serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                await asyncio.wait_for(asyncio.gather(*(run_bot(
                    url=f"ws://127.0.0.1:{port}", token=f"local-bot-{i}",
                    strategy="battleships.strategy:BaselineStrategy", mode="TRAINING", opponent="EASY",
                    log_dir=str(Path(temp) / str(i)), budget_ms=150, reserve_ms=250,
                    allow_bonuses=False, stop_after=1) for i in range(3))), 15)
        self.assertEqual(errors, [])
        self.assertEqual(len(set(finished)), 3)

    async def test_two_complete_games_with_reconnect_over_real_websocket(self):
        """Full 10x10 fleets; transport, process worker, recovery and SPEEDRUN."""
        connections = 0
        shots = []
        own_ships = None
        target_ships = [set(s) for s in BaselineStrategy().place_ships(Rules.parse(started()["rules"]))]
        seen = set()
        errors = []
        durations = []

        async def handler(ws):
            nonlocal connections, own_ships, shots, seen
            try:
                connections += 1
                self.assertEqual(ws.request.headers["Authorization"], "Bearer local-test-token")
                mode = json.loads(await ws.recv())
                self.assertEqual(mode["type"], "CHANGE_MODE")
                await ws.send(json.dumps({"type": "MODE_CHANGED", "mode": "TRAINING"}))
                if connections == 1:
                    await ws.send("not-json")
                    await ws.send(json.dumps({"type": "FUTURE_MESSAGE"}))
                    await ws.send(json.dumps(started()))
                    placement = json.loads(await ws.recv())
                    self.assertEqual(placement["type"], "PLACE")
                    own_ships = placement["ships"]
                    parsed = [[Cell.parse(c) for c in s["cells"]] for s in own_ships]
                    self.assertTrue(valid_placement(parsed, Rules.parse(started()["rules"])))
                    await ws.send('{"type":"PLACEMENT_ACCEPTED"}')
                else:
                    snapshot = started()
                    snapshot.update(type="MATCH_SNAPSHOT", ships=own_ships, shots=shots,
                                    opponentShots=[], shotsLeft=100-len(shots), placementWindowLeftSeconds=None)
                    await ws.send(json.dumps(snapshot))
                for game_number in range(2):
                    if game_number == 1:
                        shots, seen = [], set()
                        await ws.send(json.dumps(started("SPEEDRUN")))
                    while not all(s <= seen for s in target_ships):
                        await ws.send(json.dumps({"type": "TURN_REQUESTED", "moveNumber": len(shots)+1,
                                                  "shotsLeft": 100-len(shots), "shotsLeftInTurn": 1}))
                        now = time.monotonic()
                        action = json.loads(await asyncio.wait_for(ws.recv(), 1))
                        durations.append(time.monotonic() - now)
                        self.assertEqual(action["type"], "SHOOT")
                        cell = Cell.parse(action["cell"])
                        self.assertNotIn(cell, seen)
                        self.assertTrue(Rules.parse(started()["rules"]).contains(cell))
                        seen.add(cell)
                        result = "MISS"
                        for ship in target_ships:
                            if cell in ship:
                                result = "SUNK" if ship <= seen else "HIT"
                        shot = {"cell": cell.wire(), "result": result}
                        shots.append(shot)
                        await ws.send(json.dumps({"type": "SHOT_RESOLVED", **shot}))
                        if connections == 1 and len(shots) == 4:
                            await ws.close(code=4007, reason="PLATFORM_UPDATING")
                            return
                        self.assertLessEqual(len(shots), 100)
                    await ws.send(json.dumps({"type": "MATCH_FINISHED", "outcome": "WIN",
                                              "endReason": "SPEEDRUN_FINISHED" if game_number else "FLEET_DESTROYED"}))
                await ws.wait_closed()
            except Exception as exc:
                errors.append(exc)
                await ws.close(code=4004)

        with tempfile.TemporaryDirectory() as temp:
            async with serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                await asyncio.wait_for(run_bot(
                    url=f"ws://127.0.0.1:{port}", token="local-test-token",
                    strategy="battleships.strategy:BaselineStrategy", mode="TRAINING", opponent="EASY",
                    log_dir=temp, budget_ms=150, reserve_ms=250, allow_bonuses=False, stop_after=2), 20)
            self.assertEqual(errors, [])
            self.assertEqual(connections, 2)
            self.assertTrue(durations)
            self.assertLess(max(durations), 1)
            text = "\n".join(p.read_text(encoding="utf-8") for p in Path(temp).glob("*.jsonl"))
            self.assertNotIn("local-test-token", text)
            self.assertIn("MATCH_SNAPSHOT", text)
            self.assertEqual(text.count('"type": "MATCH_FINISHED"'), 2)

    async def test_terminal_replacement_does_not_fight_other_client(self):
        calls = 0
        async def handler(ws):
            nonlocal calls
            calls += 1
            await ws.recv()
            await ws.close(code=4002, reason="CONNECTION_REPLACED")
        with tempfile.TemporaryDirectory() as temp:
            async with serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                await asyncio.wait_for(run_bot(
                    url=f"ws://127.0.0.1:{port}", token="local-token",
                    strategy="battleships.strategy:BaselineStrategy", mode="TRAINING", opponent="EASY",
                    log_dir=temp, budget_ms=150, reserve_ms=250, allow_bonuses=False), 10)
        self.assertEqual(calls, 1)


class JournalTests(unittest.TestCase):
    def test_redacts_credentials_recursively(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Journal(temp, "sensitive-token")
            log.write("test", {"Authorization": "Bearer sensitive-token", "nested": [{"password": "hidden"}],
                               "message": "sensitive-token"})
            path = log.path
            log.close()
            value = path.read_text(encoding="utf-8")
            self.assertNotIn("sensitive-token", value)
            self.assertNotIn("hidden", value)
