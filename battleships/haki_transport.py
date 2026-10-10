"""Haki-only correlation guard; the wire protocol has no command IDs.

A timed-out shot can still be accepted in a later turn. Never pipeline another
command while its result is unknown. Requests observed before that result are
discarded: replaying them could spend the same server turn twice.
"""
import os
import time
from pathlib import Path

from .client import Controller
from .model import Cell


class HakiTransportStopped(Exception):
    """A deliberate terminal stop, never an automatic reconnect."""


class HakiController(Controller):
    def __init__(self, *args, resync_seconds=2.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.resync_seconds = resync_seconds
        self.resync_deadline = None
        self.pending_turn = None
        self.sent_at = None
        self.requested_at = None
        self.last_move = -1
        self.discarded_turns = set()
        self.stop_file = os.environ.get("HAKI_STOP_FILE")

    def stop_requested(self):
        return bool(self.stop_file and Path(self.stop_file).is_file())

    def idle_stop_requested(self):
        return self.stop_requested() and (self.state is None or self.state.finished)

    def check_resync(self):
        if self.resync_deadline is not None and time.monotonic() >= self.resync_deadline:
            self.journal.write("haki_transport_stopped", {"reason": "UNRESOLVED_ACTION",
                "turn": self.pending_turn, "action": self.pending.wire() if self.pending else None})
            raise HakiTransportStopped("UNRESOLVED_ACTION")

    def wait_for_result(self):
        if self.resync_deadline is None:
            self.resync_deadline = time.monotonic() + self.resync_seconds

    async def act(self, *, fallback_only=False):
        if self.pending is not None:
            return
        await super().act(fallback_only=fallback_only)
        if self.pending is not None:
            self.pending_turn = self.turn_key
            self.sent_at = time.monotonic()
            self.journal.write("haki_action_timing", {"match_key": self.match_key,
                "turn": self.pending_turn, "action": self.pending.wire(),
                "request_to_send_ms": round(1000 * (self.sent_at - self.requested_at), 3)
                    if self.requested_at is not None else None})

    async def handle(self, msg):
        kind = msg.get("type")
        if kind in {"MATCH_STARTED", "MATCH_SNAPSHOT", "MATCH_FINISHED"}:
            self.resync_deadline = None
            self.pending_turn = self.sent_at = None
            self.last_move = -1
            self.discarded_turns.clear()
            await super().handle(msg)
            return
        self.check_resync()
        if kind == "TURN_REQUESTED" and self.state is not None:
            move = msg["moveNumber"]
            key = (move, msg["shotsLeft"], msg["shotsLeftInTurn"])
            if move < self.last_move or key in self.discarded_turns:
                self.journal.write("received", msg)
                self.journal.write("haki_stale_turn_ignored", {"move": move})
                return
            self.last_move = move
            if self.pending is not None:
                self.journal.write("received", msg)
                if key == self.pending_turn and self.resync_deadline is None:
                    self.journal.write("duplicate_turn_ignored")
                    return
                self.discarded_turns.add(key)
                self.journal.write("haki_turn_deferred", {"move": move,
                    "pending_turn": self.pending_turn, "action": self.pending.wire()})
                self.wait_for_result()
                return
            self.requested_at = time.monotonic()
        elif kind == "TURN_TIMED_OUT" and self.pending is not None:
            self.journal.write("received", msg)
            if self.pending_turn is not None and msg.get("moveNumber") != self.pending_turn[0]:
                self.journal.write("haki_stale_timeout_ignored", {"move": msg.get("moveNumber"),
                    "pending_turn": self.pending_turn})
                return
            # Keep the cell and send time: timeout does not reject the command.
            self.turn_deadline = 0.0
            self.wait_for_result()
            return
        elif kind in {"SHOT_RESOLVED", "SHOT_REJECTED"} and self.state is not None:
            cell = Cell.parse(msg["cell"])
            matches = (self.pending is not None and self.pending.type == "SHOOT"
                       and self.pending.cell == cell)
            if not matches:
                self.journal.write("received", msg)
                self.journal.write("haki_unmatched_result", {"type": kind, "cell": cell.wire(),
                    "pending_turn": self.pending_turn})
                # A duplicate/late result must never clear a newer action.
                if kind == "SHOT_RESOLVED" and not any(c == cell for c, _ in self.state.shots):
                    self.state.shots.append((cell, msg["result"]))
                return
            self.journal.write("haki_result_timing", {"turn": self.pending_turn,
                "cell": cell.wire(), "type": kind,
                "send_to_result_ms": round(1000 * (time.monotonic() - self.sent_at), 3),
                "resynchronizing": self.resync_deadline is not None})
            self.resync_deadline = None
        elif kind in {"BONUS_RESOLVED", "BONUS_REJECTED"}:
            if self.pending is None or self.pending.type != "USE_BONUS" or self.pending.bonus != msg.get("bonus"):
                self.journal.write("received", msg)
                self.journal.write("haki_unmatched_result", {"type": kind, "bonus": msg.get("bonus")})
                return
            self.resync_deadline = None
            if kind == "BONUS_REJECTED" and msg.get("reason") in {"NOT_YOUR_TURN", "NO_RUNNING_MATCH", "MATCH_FINISHED"}:
                self.journal.write("received", msg)
                self.pending = None
                self.turn_deadline = 0.0
                return
        elif kind == "COMMAND_REJECTED" and (self.resync_deadline is not None or
                (self.pending is not None and msg.get("reason") in {"NOT_YOUR_TURN", "NO_RUNNING_MATCH", "MATCH_FINISHED"})):
            # No cell/command ID in this message: attribution is ambiguous.
            self.journal.write("received", msg)
            raise HakiTransportStopped("AMBIGUOUS_REJECTION")
        await super().handle(msg)
