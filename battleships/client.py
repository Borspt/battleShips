import asyncio
import os
import json
import logging
import random
import time
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, InvalidStatus

from .model import Action, Cell, GameState, valid_action, valid_placement
from .strategy import BaselineStrategy
from .worker import StrategyWorker
from .opponents import OpponentDirectory
from .haki_session import HAKI_STRATEGY, HakiSession

LOG = logging.getLogger(__name__)
STOP_CODES = {4001, 4002, 4004, 4005}


async def session_messages(ws, session, controller=None):
    if session is None:
        async for raw in ws:
            yield raw
        return
    while True:
        if controller is not None:
            from .haki_transport import HakiTransportStopped
            controller.check_resync()
            if controller.idle_stop_requested():
                raise HakiTransportStopped("OPERATOR_STOP")
        try:
            # Нет ответа/матча: это не доказательство отключения тренировок.
            # Заканчиваем запуск, не переключаемся в рейтинг наугад.
            waiting_for_mode_or_training = not session.active and (
                not session.confirmed or session.mode == "TRAINING")
            if controller is None:
                raw = await asyncio.wait_for(ws.recv(),
                    session.idle_timeout if waiting_for_mode_or_training else None)
            else:
                wait_started = time.monotonic()
                while True:
                    controller.check_resync()
                    if controller.idle_stop_requested():
                        raise HakiTransportStopped("OPERATOR_STOP")
                    wait = 0.5
                    if controller.resync_deadline is not None:
                        wait = min(wait, max(0.001, controller.resync_deadline - time.monotonic()))
                    try:
                        raw = await asyncio.wait_for(ws.recv(), wait)
                        break
                    except TimeoutError:
                        if waiting_for_mode_or_training and time.monotonic() - wait_started >= session.idle_timeout:
                            raise
        except TimeoutError:
            session.stop_reason = "MODE_OR_MATCH_WAIT_TIMEOUT"
            return
        except ConnectionClosedOK:
            return
        yield raw


class Journal:
    def __init__(self, directory: str, token: str):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        self.path = path / f"session-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}.jsonl"
        self.file = self.path.open("a", encoding="utf-8")
        self.token = token

    def write(self, event: str, data=None):
        def redact(value):
            if isinstance(value, dict):
                return {k: ("[REDACTED]" if any(word in k.lower()
                        for word in ("token", "authorization", "password", "secret"))
                        else redact(v)) for k, v in value.items()}
            if isinstance(value, list):
                return [redact(v) for v in value]
            if isinstance(value, str) and self.token:
                return value.replace(self.token, "[REDACTED]")
            return value
        self.file.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                   "event": event, "data": redact(data)}, ensure_ascii=False) + "\n")
        self.file.flush()

    def close(self):
        self.file.close()


class Controller:
    def __init__(self, send, worker, journal, *, budget_ms=150, reserve_ms=250,
                 allow_bonuses=False, opponent_directory=None, protect_turn_order=False):
        self.send = send
        self.worker = worker
        self.journal = journal
        self.budget_ms = budget_ms
        self.reserve_ms = reserve_ms
        self.allow_bonuses = allow_bonuses
        # Enabled only for Haki; other bots retain their existing transport behavior.
        self.protect_turn_order = protect_turn_order
        self.fallback = BaselineStrategy()
        self.state = None
        self.pending = None
        self.pending_ships = None
        self.turn_deadline = 0.0
        self.placement_deadline = 0.0
        self.turn_key = None
        self.retries = 0
        self.placement_attempts = 0
        self.opponent_directory = opponent_directory
        self.opponent_task = None
        self.match_key = None

    async def close(self):
        if self.opponent_task is not None:
            self.opponent_task.cancel()
            await asyncio.gather(self.opponent_task, return_exceptions=True)
            self.opponent_task = None

    async def load_opponent(self, state, match_key):
        try:
            info = await self.opponent_directory.lookup(state.opponent_info)
            if self.state is state and not state.finished:
                state.opponent_info = info
                self.journal.write("opponent_context", {"match_key": match_key, **asdict(info)})
        except Exception as exc:
            self.journal.write("opponent_lookup_failed", {"match_key": match_key,
                                                         "reason": type(exc).__name__})

    async def emit(self, message: dict, deadline: float | None = None):
        payload = json.dumps(message, separators=(",", ":"))
        if len(payload.encode("utf-8")) > 8192:
            raise ValueError("Outgoing message exceeds 8 KiB")
        remaining = deadline - time.monotonic() if deadline is not None else 2.0
        if remaining <= 0:
            self.journal.write("expired_action", {"type": message["type"]})
            return False
        await asyncio.wait_for(self.send(payload), timeout=remaining)
        self.journal.write("sent", message)
        return True

    async def place(self, *, fallback_only=False):
        if self.state.rules.modifier == "SPEEDRUN" or self.state.ships is not None:
            return
        self.placement_attempts += 1
        if self.placement_attempts > 2 or time.monotonic() >= self.placement_deadline:
            return
        ships = None
        if not fallback_only:
            try:
                budget = min(1.0, self.placement_deadline - time.monotonic())
                ships = await self.worker.call("place_ships_for_match", (self.state,), budget)
            except Exception as exc:
                self.journal.write("placement_fallback", {"reason": type(exc).__name__})
        if not valid_placement(ships, self.state.rules):
            # Runs outside the event loop, bounded inside the reference generator.
            try:
                ships = await asyncio.to_thread(self.fallback.place_ships, self.state.rules)
            except Exception as exc:
                self.journal.write("placement_failed", {"reason": type(exc).__name__})
                return
        if valid_placement(ships, self.state.rules):
            self.pending_ships = ships
            await self.emit({"type": "PLACE", "ships": [
                {"cells": [c.wire() for c in ship]} for ship in ships
            ]}, self.placement_deadline)

    async def act(self, *, fallback_only=False):
        if self.state is None or self.state.finished or self.state.shots_left <= 0:
            return
        remaining = self.turn_deadline - time.monotonic()
        if remaining <= 0:
            return
        action = None
        if not fallback_only:
            budget = min(self.budget_ms / 1000, max(0, remaining - 0.15))
            if budget > 0.01:
                try:
                    action = await self.worker.call(
                        "choose_action", (self.state, int(budget * 1000)), budget)
                except Exception as exc:
                    self.journal.write("strategy_fallback", {"reason": type(exc).__name__})
        if not valid_action(action, self.state, allow_bonuses=self.allow_bonuses):
            if action is not None:
                self.journal.write("invalid_strategy_action")
            try:
                action = self.fallback.choose_action(self.state, 0)
            except ValueError:
                self.journal.write("no_legal_target")
                return
        if await self.emit(action.wire(), self.turn_deadline):
            self.pending = action

    async def handle(self, msg: dict):
        self.journal.write("received", msg)
        kind = msg.get("type")
        if kind in {"MATCH_STARTED", "MATCH_SNAPSHOT"}:
            received_at = time.monotonic()
            await self.close()
            self.state = GameState.from_message(msg)
            # The protocol has no matchId; this key only correlates local log records.
            self.match_key = uuid4().hex
            self.journal.write("match_context", {"match_key": self.match_key,
                                                "restored": kind == "MATCH_SNAPSHOT",
                                                **asdict(self.state.opponent_info)})
            self.pending = self.pending_ships = self.turn_key = None
            self.turn_deadline = 0.0
            self.placement_attempts = 0
            # Snapshot has no current-turn deadline: never invent a turn on recovery.
            window = (self.state.rules.placement_window_seconds if kind == "MATCH_STARTED"
                      else msg.get("placementWindowLeftSeconds"))
            LOG.info("%s: %s", kind, self.state.rules.modifier or "CLASSIC")
            if window is not None:
                # Rounded-down zero still means an open sub-second placement window.
                usable = float(window) if window > 0 else 0.1
                self.placement_deadline = received_at + max(0.01, usable - self.reserve_ms / 1000)
            if self.opponent_directory and self.state.opponent_info.key:
                self.opponent_task = asyncio.create_task(self.load_opponent(self.state, self.match_key))
                # No HTTP in choose_action. Bound the initial wait; slow lookup continues
                # in the background and updates this match only, without delaying turns.
                wait = 0.1 if kind == "MATCH_SNAPSHOT" or self.state.rules.modifier == "SPEEDRUN" else 4.0
                if window is not None:
                    wait = min(wait, max(0, self.placement_deadline - time.monotonic() - 1.2))
                if wait > 0:
                    await asyncio.wait({self.opponent_task}, timeout=wait)
            if window is not None:
                await self.place()
            return
        if kind == "BET_REQUESTED":
            # «Казино „Рояль“»: ставка включается переменной BATTLESHIPS_BET="МОДИФИКАТОР:очки", иначе не ставим.
            wanted, _, amount = os.environ.get("BATTLESHIPS_BET", "").partition(":")
            try:
                amount = float(amount)
            except ValueError:
                amount = 0.0
            if wanted in (msg.get("modifiers") or []) and 0 < amount <= float(msg.get("maxAmount") or 0):
                await self.emit({"type": "PLACE_BET", "modifier": wanted, "amount": amount})
            else:
                self.journal.write("bet_skipped", {"reason": "rating spending disabled", "offered": msg.get("modifiers")})
            return
        if self.state is None:
            return
        if kind == "PLACEMENT_ACCEPTED":
            self.state.ships = self.pending_ships
            self.pending_ships = None
        elif kind == "PLACEMENT_REJECTED":
            if msg.get("reason") not in {"PLACEMENT_ALREADY_ACCEPTED", "NO_PLACEMENT_IN_MATCH", "MATCH_FINISHED"}:
                await self.place(fallback_only=True)
        elif kind == "TURN_REQUESTED":
            if self.state.finished:
                return
            key = (msg["moveNumber"], msg["shotsLeft"], msg["shotsLeftInTurn"])
            if key == self.turn_key:
                self.journal.write("duplicate_turn_ignored")
                return
            self.turn_key = key
            self.state.move_number, self.state.shots_left, self.state.shots_left_in_turn = key
            self.turn_deadline = time.monotonic() + max(
                0, self.state.rules.move_timeout_seconds - self.reserve_ms / 1000)
            self.pending = None
            self.retries = 0
            await self.act()
        elif kind == "SHOT_RESOLVED":
            cell = Cell.parse(msg["cell"])
            self.state.shots.append((cell, msg["result"]))
            # A result can arrive after a timeout and a newer TURN_REQUESTED.
            # Keep its board evidence, but do not acknowledge a different action.
            if (not self.protect_turn_order or self.pending is None or
                    (self.pending.type == "SHOOT" and self.pending.cell == cell)):
                self.pending = None
            else:
                self.journal.write("haki_unmatched_shot_result", {"cell": cell.wire()})
            # Always wait for TURN_REQUESTED, including after a HIT.
        elif kind == "OPPONENT_SHOT":
            cell, result = Cell.parse(msg["cell"]), msg["result"]
            self.state.opponent_shots.append((cell, result))
            if result == "DEFENDED":
                self.state.shield_active = False
            if result == "MINE":
                self.state.mines.discard(cell)
        elif kind == "BONUS_RESOLVED":
            bonus = msg["bonus"]
            self.state.bonuses[bonus] = max(0, self.state.bonuses.get(bonus, 0) - 1)
            result = dict(msg)
            if self.pending and self.pending.cell:
                result["requestedCell"] = self.pending.cell.wire()
            self.state.bonus_results.append(result)
            if bonus == "SHIELD":
                self.state.shield_active = bool(msg.get("shieldActive"))
            if bonus == "MINE" and msg.get("mineCell"):
                self.state.mines.add(Cell.parse(msg["mineCell"]))
            self.pending = None
            # SALVO can leave all three counters unchanged (e.g. a multi-shot mode).
            # A successful action still permits a new request for the same turn.
            self.turn_key = None
        elif kind == "OPPONENT_BONUS_USED":
            self.state.opponent_bonuses.append(msg["bonus"])
        elif kind in {"BONUS_REJECTED", "COMMAND_REJECTED"}:
            reason = msg.get("reason")
            recoverable = reason not in {"NOT_YOUR_TURN", "NO_RUNNING_MATCH", "MATCH_FINISHED"}
            if recoverable and self.pending is not None and self.retries < 1:
                self.retries += 1
                self.pending = None
                await self.act(fallback_only=True)
        elif kind in {"TURN_TIMED_OUT", "SHOT_REJECTED"}:
            if self.protect_turn_order:
                if (kind == "TURN_TIMED_OUT" and self.turn_key is not None and
                        isinstance(msg.get("moveNumber"), int) and
                        msg["moveNumber"] < self.turn_key[0]):
                    self.journal.write("haki_stale_timeout_ignored", msg)
                    return
                if (kind == "SHOT_REJECTED" and self.pending is not None and
                        msg.get("cell") is not None and
                        (self.pending.type != "SHOOT" or
                         self.pending.cell != Cell.parse(msg["cell"]))):
                    self.journal.write("haki_unmatched_shot_rejection", msg)
                    return
            self.pending = None
            self.turn_deadline = 0.0
        elif kind == "MATCH_FINISHED":
            self.state.finished = True
            self.journal.write("match_summary", {"match_key": self.match_key,
                "opponent": asdict(self.state.opponent_info),
                "outcome": msg.get("outcome"), "end_reason": msg.get("endReason"),
                "modifier": self.state.rules.modifier,
                "shots": [{"cell": c.wire(), "result": r} for c, r in self.state.shots],
                "opponent_shots": [{"cell": c.wire(), "result": r} for c, r in self.state.opponent_shots]})
            await self.close()
            self.pending = None
            self.turn_deadline = 0.0
            LOG.info("Match finished: %s (%s)", msg.get("outcome"), msg.get("endReason"))
        # Unknown types and extra fields do not terminate the connection.


async def run_bot(*, url: str, token: str, strategy: str, mode: str, opponent: str,
                  log_dir: str, budget_ms: int, reserve_ms: int, allow_bonuses: bool,
                  stop_after: int = 0, opponent_ratings_file: str | None = None,
                  stop_on_loss: bool = False):
    from .haki_transport import HakiController, HakiTransportStopped
    session = HakiSession(mode, opponent) if strategy == HAKI_STRATEGY else None
    # Для Хаки это обязательное правило, даже если забыли флаг в ручной команде.
    stop_on_loss = stop_on_loss or session is not None
    worker = StrategyWorker(strategy)
    journal = Journal(log_dir, token)
    completed = 0
    delay = 0.5
    controller = None
    directory = OpponentDirectory(url, opponent_ratings_file)
    rating_gate = None
    try:
        if session and session.stop_policy == 'RATING_GAIN':
            import os
            from .haki_rating import HakiRatingGate
            rating_gate = HakiRatingGate(directory.base_url, journal,
                warmup=int(os.environ.get('HAKI_RATING_WARMUP', '3')))
            try:
                await rating_gate.start()
            except (OSError, ValueError, KeyError, TypeError):
                journal.write('haki_session_stopped', {'reason': 'RATING_UNAVAILABLE'})
                return
        try:
            await worker.start()
        except Exception as exc:
            journal.write("strategy_start_failed", {"reason": type(exc).__name__})
        while True:
            rating_reconnect = False
            if controller is not None:
                await controller.close()
            opened_at = time.monotonic()
            try:
                async with connect(url, additional_headers={"Authorization": f"Bearer {token}"},
                                   open_timeout=10, ping_interval=20, ping_timeout=20,
                                   close_timeout=2, max_size=4 * 1024 * 1024, proxy=None) as ws:
                    journal.write("connected")
                    LOG.info("Connected; requested mode: %s", session.mode if session else mode)
                    controller_type = HakiController if session else Controller
                    controller = controller_type(ws.send, worker, journal, budget_ms=budget_ms,
                                            reserve_ms=reserve_ms, allow_bonuses=allow_bonuses,
                                            opponent_directory=directory,
                                            protect_turn_order=session is not None)
                    if session:
                        session.connected()
                    await controller.emit(session.command() if session else
                        {"type": "CHANGE_MODE", "mode": mode,
                         **({"opponent": opponent} if mode == "TRAINING" else {})})
                    async for raw in session_messages(ws, session, controller if session else None):
                        try:
                            message = json.loads(raw)
                            if not isinstance(message, dict):
                                raise ValueError("Expected JSON object")
                        except (ValueError, TypeError, UnicodeError):
                            journal.write("invalid_platform_json")
                            continue
                        # Transport/send errors must leave this loop and reconnect.
                        was_active = session.active if session else False
                        if session:
                            session.observe(message)
                        try:
                            await controller.handle(message)
                        except (KeyError, TypeError, ValueError) as exc:
                            journal.write("invalid_platform_message", {"reason": type(exc).__name__,
                                                                      "type": message.get("type")})
                        if message.get("type") == "MATCH_FINISHED":
                            completed += 1
                            delay = 0.5
                            if session and controller.stop_requested():
                                journal.write("haki_session_stopped", {"reason": "OPERATOR_STOP", "completed": completed})
                                return
                            if rating_gate and was_active:
                                # Stop matchmaking before doing HTTP work. Keeping
                                # this socket open can assign another match during
                                # the rating lookup, then disconnect it unplaced.
                                await ws.close()
                                if session.should_stop:
                                    journal.write('haki_session_stopped', {'reason': session.stop_reason})
                                    return
                                reason = await rating_gate.after_match()
                                if reason:
                                    journal.write('haki_session_stopped', {'reason': reason})
                                    return
                                if controller.stop_requested():
                                    journal.write('haki_session_stopped', {'reason': 'OPERATOR_STOP'})
                                    return
                                rating_reconnect = True
                                break
                            if (stop_on_loss and message.get("outcome") == "LOSS"
                                    and (session is None or
                                         (session.stop_policy == 'LOSSES' and session.should_stop))):
                                details = {"completed": completed, "end_reason": message.get("endReason")}
                                if session:
                                    details.update(losses=session.losses, loss_limit=session.loss_limit)
                                journal.write("stop_on_loss", details)
                                LOG.info("Haki loss limit reached; stopping without reconnect" if session
                                         else "First loss received; stopping without reconnect")
                                return
                            if stop_after and completed >= stop_after:
                                return
                        if session and session.should_stop:
                            journal.write("haki_session_stopped", {"reason": session.stop_reason})
                            LOG.warning("Haki stopped: %s", session.stop_reason)
                            return
                        if session and session.change_requested:
                            journal.write("haki_training_passed", {"wins": session.training_wins})
                            LOG.info("Haki: three training wins; requesting RANKING")
                            await controller.emit(session.command())
                    if session and session.should_stop:
                        journal.write("haki_session_stopped", {"reason": session.stop_reason})
                        LOG.warning("Haki stopped: %s", session.stop_reason)
                        return
                    # A normal close can also carry a terminal platform code.
                    if ws.close_code in STOP_CODES:
                        journal.write("terminal_close", {"code": ws.close_code})
                        LOG.error("Connection stopped by platform (code %s)", ws.close_code)
                        return
            except HakiTransportStopped as exc:
                journal.write("haki_session_stopped", {"reason": str(exc)})
                LOG.warning("Haki stopped without reconnect: %s", exc)
                return
            except InvalidStatus as exc:
                code = exc.response.status_code
                journal.write("handshake_failed", {"status": code})
                if code in {401, 403}:
                    raise RuntimeError(f"Connection rejected (HTTP {code}); check bot token/status") from None
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
                journal.write("disconnected", {"code": code})
                if code in STOP_CODES:
                    LOG.error("Connection stopped by platform (code %s); automatic retry disabled", code)
                    return
            except (OSError, TimeoutError) as exc:
                journal.write("connection_error", {"reason": type(exc).__name__})
            if rating_reconnect:
                journal.write('haki_rating_continue')
                continue
            if time.monotonic() - opened_at > 30:
                delay = 0.5
            await asyncio.sleep(delay + random.uniform(0, delay * 0.2))
            delay = min(10, delay * 2)
    finally:
        if controller is not None:
            await controller.close()
        await worker.close()
        journal.close()
