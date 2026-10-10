"""Shared, pickleable contract between the transport and Boris's strategy.

Coordinates always use (row, column), both zero based. The controller owns state.
Strategies receive a separate copy of it in a worker process.
"""
from dataclasses import dataclass, field
from typing import Any


def positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"Invalid {name}")
    return value


@dataclass(frozen=True, order=True)
class Cell:
    row: int
    column: int

    @classmethod
    def parse(cls, value: dict) -> "Cell":
        row, column = value["row"], value["column"]
        if type(row) is not int or type(column) is not int:
            raise ValueError("Cell coordinates must be integers")
        return cls(row, column)

    def wire(self) -> dict:
        return {"row": self.row, "column": self.column}


@dataclass(frozen=True)
class Rules:
    rows: int
    columns: int
    fleet: tuple[int, ...]
    touch_rule: str
    turn_after_hit_rule: str
    move_timeout_seconds: int
    placement_window_seconds: int
    match_duration_seconds: int
    shot_limit_per_player: int
    shots_per_turn: int
    modifier: str | None

    @classmethod
    def parse(cls, data: dict) -> "Rules":
        fleet = tuple(
            length
            for item in data["fleet"]
            for length in [positive_int(item["length"], "ship length")]
            for _ in range(positive_int(item["count"], "ship count"))
        )
        rows = positive_int(data["rows"], "rows")
        columns = positive_int(data["columns"], "columns")
        if not fleet or sum(fleet) > rows * columns:
            raise ValueError("Invalid fleet")
        return cls(
            rows, columns, tuple(sorted(fleet, reverse=True)),
            data["touchRule"], data["turnAfterHitRule"],
            positive_int(data["moveTimeoutSeconds"], "move timeout"),
            positive_int(data["placementWindowSeconds"], "placement window"),
            positive_int(data["matchDurationSeconds"], "match duration"),
            positive_int(data["shotLimitPerPlayer"], "shot limit"),
            positive_int(data.get("shotsPerTurn", 1), "shots per turn"),
            data.get("modifier"),
        )

    def contains(self, cell: Cell) -> bool:
        return 0 <= cell.row < self.rows and 0 <= cell.column < self.columns

    def cells(self) -> list[Cell]:
        return [Cell(r, c) for r in range(self.rows) for c in range(self.columns)]


@dataclass(frozen=True)
class Action:
    type: str
    cell: Cell | None = None
    bonus: str | None = None

    @classmethod
    def shoot(cls, cell: Cell) -> "Action":
        return cls("SHOOT", cell)

    def wire(self) -> dict:
        value = {"type": self.type}
        if self.cell is not None:
            value["cell"] = self.cell.wire()
        if self.bonus is not None:
            value["bonus"] = self.bonus
        return value


@dataclass(frozen=True)
class OpponentInfo:
    """Identity and observed Elo; None means unavailable, never an assumed 1500."""
    key: str | None = None
    bot_id: str | None = None
    built_in_bot: str | None = None
    rating: float | None = None
    rating_source: str | None = None
    rating_observed_at: str | None = None

    @classmethod
    def from_wire(cls, opponent: dict) -> "OpponentInfo":
        bot_id = opponent.get("botId")
        level = opponent.get("builtInBot")
        if opponent.get("kind") == "REMOTE_BOT" and isinstance(bot_id, str) and bot_id:
            return cls(key=f"bot:{bot_id}", bot_id=bot_id)
        if opponent.get("kind") == "BUILT_IN_BOT" and level in {"EASY", "MEDIUM", "HARD"}:
            return cls(key=f"builtin:{level}", built_in_bot=level)
        return cls()


@dataclass
class GameState:
    rules: Rules
    opponent: dict = field(default_factory=dict)
    ships: list[list[Cell]] | None = None
    shots: list[tuple[Cell, str]] = field(default_factory=list)
    opponent_shots: list[tuple[Cell, str]] = field(default_factory=list)
    bonuses: dict[str, int] = field(default_factory=dict)
    bonus_results: list[dict] = field(default_factory=list)
    opponent_bonuses: list[str] = field(default_factory=list)
    shield_active: bool = False
    mines: set[Cell] = field(default_factory=set)
    move_number: int = 0
    shots_left: int = 0
    shots_left_in_turn: int = 0
    finished: bool = False
    opponent_info: OpponentInfo = field(default_factory=OpponentInfo)

    @classmethod
    def from_message(cls, msg: dict) -> "GameState":
        rules = Rules.parse(msg["rules"])
        opponent = msg.get("opponent") or {}
        state = cls(rules, opponent=dict(opponent), opponent_info=OpponentInfo.from_wire(opponent))
        state.bonuses = {x["bonus"]: x["uses"] for x in msg.get("bonuses", [])}
        state.shots_left = msg.get("shotsLeft", rules.shot_limit_per_player)
        state.shots_left_in_turn = rules.shots_per_turn
        if msg.get("ships") is not None:
            state.ships = [[Cell.parse(c) for c in s["cells"]] for s in msg["ships"]]
        state.shots = [(Cell.parse(s["cell"]), s["result"]) for s in msg.get("shots", [])]
        state.opponent_shots = [
            (Cell.parse(s["cell"]), s["result"]) for s in msg.get("opponentShots", [])
        ]
        state.shield_active = msg.get("shieldActive", False)
        state.mines = {Cell.parse(c) for c in msg.get("mines", [])}
        return state

    def observations(self) -> dict[Cell, str]:
        result: dict[Cell, str] = {}
        for cell, outcome in self.shots:
            if self.rules.contains(cell):
                # A duplicate shot is MISS, but cannot erase evidence of a ship.
                if outcome == "MISS" and result.get(cell) in {"HIT", "SUNK", "DEFENDED"}:
                    continue
                result[cell] = outcome
        return result

    def legal_targets(self) -> list[Cell]:
        observed = self.observations()
        excluded = {cell for cell, result in observed.items() if result != "DEFENDED"}
        if self.rules.touch_rule == "NO_TOUCHING_EVEN_DIAGONALLY":
            occupied = {c for c, r in observed.items() if r in {"HIT", "SUNK", "DEFENDED"}}
            for c in occupied:
                excluded.update(Cell(c.row + dr, c.column + dc)
                                for dr in (-1, 1) for dc in (-1, 1))
            # SUNK supplies only the last cell. Find the connected hit component.
            for c, result in observed.items():
                if result != "SUNK":
                    continue
                group, pending = {c}, [c]
                while pending:
                    current = pending.pop()
                    for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        other = Cell(current.row + dr, current.column + dc)
                        if other in occupied and other not in group:
                            group.add(other)
                            pending.append(other)
                for hit in group:
                    excluded.update(Cell(hit.row + dr, hit.column + dc)
                                    for dr in (-1, 0, 1) for dc in (-1, 0, 1))
        return [c for c in self.rules.cells() if c not in excluded]


def valid_placement(ships: Any, rules: Rules) -> bool:
    if not isinstance(ships, list) or not all(isinstance(s, list) for s in ships):
        return False
    if sorted(map(len, ships), reverse=True) != list(rules.fleet):
        return False
    occupied: set[Cell] = set()
    for ship in ships:
        if not all(isinstance(c, Cell) and type(c.row) is int and type(c.column) is int
                   and rules.contains(c) for c in ship):
            return False
        cells = set(ship)
        if len(cells) != len(ship) or cells & occupied:
            return False
        rows, cols = {c.row for c in cells}, {c.column for c in cells}
        if not ((len(rows) == 1 and max(cols) - min(cols) + 1 == len(cells))
                or (len(cols) == 1 and max(rows) - min(rows) + 1 == len(cells))):
            return False
        # Only the following rule is documented. Don't guess future enum semantics.
        if rules.touch_rule != "NO_TOUCHING_EVEN_DIAGONALLY":
            return False
        if any(Cell(c.row + dr, c.column + dc) in occupied
               for c in cells for dr in (-1, 0, 1) for dc in (-1, 0, 1)):
            return False
        occupied.update(cells)
    return True


def valid_action(action: Any, state: GameState, *, allow_bonuses: bool = False) -> bool:
    if not isinstance(action, Action) or state.shots_left <= 0 or state.finished:
        return False
    if action.cell is not None and (not isinstance(action.cell, Cell)
            or type(action.cell.row) is not int or type(action.cell.column) is not int):
        return False
    if action.type == "SHOOT":
        return action.cell in state.legal_targets() and action.bonus is None
    if action.type != "USE_BONUS" or not allow_bonuses or not isinstance(action.bonus, str):
        return False
    if action.bonus not in {"DISTANCE", "SONAR", "SALVO", "SHIELD", "MINE"}:
        return False
    if state.bonuses.get(action.bonus, 0) <= 0:
        return False
    if action.bonus in {"DISTANCE", "SONAR", "MINE"}:
        if action.cell is None or not state.rules.contains(action.cell):
            return False
    if action.bonus == "SHIELD" and state.shield_active:
        return False
    if action.bonus == "MINE":
        if state.ships is None:
            return False
        blocked = {c for ship in state.ships for c in ship} | state.mines
        blocked.update(c for c, _ in state.opponent_shots)
        return action.cell not in blocked
    return True
