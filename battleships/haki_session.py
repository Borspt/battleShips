"""Ограничения запуска только личного бота Музроба."""
from dataclasses import dataclass, field
import os

HAKI_STRATEGY = "battleships.strategies.haki:HakiObservationBot"


@dataclass
class HakiSession:
    mode: str
    opponent: str
    confirmed: bool = False
    active: bool = False
    stop_reason: str | None = None
    idle_timeout: float = 30.0
    training_wins: int = 0
    training_match: bool = False
    change_requested: bool = False
    loss_limit: int = field(default_factory=lambda: int(os.environ.get('HAKI_LOSS_LIMIT', '1')))
    losses: int = 0

    def __post_init__(self):
        if not 1 <= self.loss_limit <= 10:
            raise ValueError('HAKI_LOSS_LIMIT must be between 1 and 10')

    def connected(self):
        # При переподключении платформа снова начинает в RANKING.
        self.confirmed = False

    def command(self):
        self.change_requested = False
        self.confirmed = False
        return {"type": "CHANGE_MODE", "mode": self.mode,
                **({"opponent": self.opponent} if self.mode == "TRAINING" else {})}

    def observe(self, message: dict):
        kind = message.get("type")
        if kind == "MODE_CHANGED":
            self.confirmed = message.get("mode") == self.mode
            if not self.confirmed:
                self.stop_reason = "UNEXPECTED_MODE"
        elif kind == "MODE_REJECTED" or (
            kind == "COMMAND_REJECTED" and not self.confirmed
        ):
            self.stop_reason = "MODE_NOT_ACCEPTED"
        elif kind in {"MATCH_STARTED", "MATCH_SNAPSHOT"}:
            self.active = True
            opponent = message.get("opponent") or {}
            self.training_match = (self.mode == "TRAINING"
                and opponent.get("kind") == "BUILT_IN_BOT"
                and opponent.get("builtInBot") == self.opponent)
            if self.mode == "TRAINING" and not self.training_match:
                # Уже назначенную партию доигрываем, иначе получим техническое
                # поражение. Затем закрываем соединение, без следующей партии.
                self.stop_reason = "UNEXPECTED_TRAINING_OPPONENT"
        elif kind == "MATCH_FINISHED":
            was_active = self.active
            self.active = False
            if message.get("outcome") == "LOSS":
                if was_active:
                    self.losses += 1
                if self.losses >= self.loss_limit:
                    self.stop_reason = "FIRST_LOSS" if self.loss_limit == 1 else "LOSS_LIMIT"
                elif not self.confirmed:
                    self.stop_reason = self.stop_reason or "MODE_NOT_CONFIRMED"
            elif message.get("outcome") not in {"WIN", "DRAW"}:
                self.stop_reason = "UNKNOWN_OUTCOME"
            elif not self.confirmed:
                self.stop_reason = self.stop_reason or "MODE_NOT_CONFIRMED"
            elif (was_active and self.training_match and not self.stop_reason
                  and message.get("outcome") == "WIN"):
                self.training_wins += 1
                if self.training_wins >= 3:
                    self.mode = "RANKING"
                    self.change_requested = True
                    self.confirmed = False

    @property
    def should_stop(self):
        return self.stop_reason is not None and not self.active
