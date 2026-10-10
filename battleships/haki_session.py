"""Ограничения запуска только личного бота Музроба."""
from dataclasses import dataclass

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
                self.stop_reason = "FIRST_LOSS"
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
