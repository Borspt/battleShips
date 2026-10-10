"""Regression for the observed move 53 timeout / delayed shot response sequence."""
import json
import unittest

from battleships.client import Controller
from battleships.model import Action, Cell
from tests.fixtures import started
from tests.test_client import FakeWorker, MemoryJournal


class HakiTurnOrderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = []

        async def send(raw):
            self.sent.append(json.loads(raw))

        self.worker = FakeWorker()
        self.client = Controller(send, self.worker, MemoryJournal(),
                                 allow_bonuses=True, protect_turn_order=True)
        message = started("SPEEDRUN")
        message["bonuses"] = [{"bonus": "DISTANCE", "uses": 2}]
        await self.client.handle(message)

    async def turn(self, number, action):
        self.worker.action = action
        await self.client.handle({"type": "TURN_REQUESTED", "moveNumber": number,
                                  "shotsLeft": 100-number, "shotsLeftInTurn": 1})

    async def test_recorded_timeout_then_late_result_preserves_new_shot(self):
        old = Action.shoot(Cell(6, 1))
        current = Action.shoot(Cell(8, 1))
        await self.turn(53, old)
        await self.client.handle({"type": "TURN_TIMED_OUT", "moveNumber": 53})
        await self.client.handle({"type": "OPPONENT_SHOT", "cell": Cell(9, 2).wire(),
                                  "result": "MISS"})
        await self.turn(55, current)
        deadline = self.client.turn_deadline
        await self.client.handle({"type": "SHOT_RESOLVED", "cell": old.cell.wire(),
                                  "result": "MISS"})
        self.assertEqual(self.client.pending, current)
        self.assertIn((old.cell, "MISS"), self.client.state.shots)
        self.assertEqual(self.client.turn_deadline, deadline)
        self.assertEqual(len(self.sent), 2)  # no speculative retry or extra shot
        third = Action.shoot(Cell(8, 3))
        await self.client.handle({"type": "OPPONENT_SHOT", "cell": Cell(7, 6).wire(),
                                  "result": "MISS"})
        await self.turn(57, third)
        await self.client.handle({"type": "SHOT_RESOLVED", "cell": current.cell.wire(),
                                  "result": "MISS"})
        self.assertEqual(self.client.pending, third)
        self.assertIn((current.cell, "MISS"), self.client.state.shots)
        self.assertEqual(len(self.sent), 3)
        await self.client.handle({"type": "SHOT_REJECTED", "cell": third.cell.wire(),
                                  "reason": "NOT_YOUR_TURN"})
        self.assertIsNone(self.client.pending)
        self.assertEqual(self.client.turn_deadline, 0)
        self.assertEqual(len(self.sent), 3)

    async def test_matching_result_acknowledges_shot_without_extra_action(self):
        current = Action.shoot(Cell(8, 1))
        await self.turn(55, current)
        await self.client.handle({"type": "SHOT_RESOLVED", "cell": current.cell.wire(),
                                  "result": "HIT"})
        self.assertIsNone(self.client.pending)
        self.assertEqual(len(self.sent), 1)

    async def test_late_shot_does_not_erase_distance_requested_cell(self):
        bonus = Action("USE_BONUS", Cell(4, 4), "DISTANCE")
        await self.turn(55, bonus)
        await self.client.handle({"type": "SHOT_RESOLVED", "cell": Cell(6, 1).wire(),
                                  "result": "MISS"})
        await self.client.handle({"type": "BONUS_RESOLVED", "bonus": "DISTANCE",
                                  "distance": 2})
        self.assertEqual(self.client.state.bonus_results[-1]["requestedCell"],
                         bonus.cell.wire())
        self.assertEqual(self.client.state.bonuses["DISTANCE"], 1)

    async def test_stale_timeout_and_other_cell_rejection_keep_current_action(self):
        current = Action.shoot(Cell(8, 1))
        await self.turn(55, current)
        deadline = self.client.turn_deadline
        await self.client.handle({"type": "TURN_TIMED_OUT", "moveNumber": 53})
        await self.client.handle({"type": "SHOT_REJECTED", "cell": Cell(6, 1).wire(),
                                  "reason": "NOT_YOUR_TURN"})
        self.assertEqual(self.client.pending, current)
        self.assertEqual(self.client.turn_deadline, deadline)
        self.assertEqual(len(self.sent), 1)
        await self.client.handle({"type": "SHOT_REJECTED", "cell": current.cell.wire(),
                                  "reason": "NOT_YOUR_TURN"})
        self.assertIsNone(self.client.pending)
        self.assertEqual(self.client.turn_deadline, 0)

    async def test_current_timeout_still_closes_turn(self):
        await self.turn(55, Action.shoot(Cell(8, 1)))
        await self.client.handle({"type": "TURN_TIMED_OUT", "moveNumber": 55})
        self.assertIsNone(self.client.pending)
        self.assertEqual(self.client.turn_deadline, 0)

    async def test_default_controller_behavior_is_unchanged(self):
        self.client.protect_turn_order = False
        await self.turn(55, Action.shoot(Cell(8, 1)))
        await self.client.handle({"type": "SHOT_RESOLVED", "cell": Cell(6, 1).wire(),
                                  "result": "MISS"})
        self.assertIsNone(self.client.pending)
