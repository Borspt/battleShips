"""Правила партии. В бою собираются из поля rules сообщения MATCH_STARTED."""

from __future__ import annotations

from dataclasses import dataclass, replace

Cell = tuple[int, int]  # (row, column), счёт с 0

DEFAULT_FLEET = (4, 3, 3, 2, 2, 2, 1, 1, 1, 1)


@dataclass(frozen=True)
class Rules:
    rows: int = 10
    columns: int = 10
    fleet: tuple[int, ...] = DEFAULT_FLEET  # длины кораблей по убыванию
    no_touching: bool = True  # корабли не касаются даже углом
    hit_keeps_turn: bool = True  # попадание не тратит выстрел хода
    shots_per_turn: int = 1
    shot_limit: int = 100
    modifier: str | None = None

    @property
    def announces_sunk(self) -> bool:
        return self.modifier != "SCHRODINGER_SHIP"

    @property
    def places_fleet(self) -> bool:
        return self.modifier != "SPEEDRUN"

    @property
    def sees_opponent_shots(self) -> bool:
        return self.modifier != "BIRD_BOX"

    @classmethod
    def from_message(cls, rules: dict) -> Rules:
        """Разбирает rules из MATCH_STARTED; незнакомые и пропущенные поля не ломают разбор."""
        fleet: list[int] = []
        for ship_class in rules.get("fleet") or []:
            fleet += [int(ship_class["length"])] * int(ship_class["count"])
        modifier = rules.get("modifier")
        return cls(
            rows=int(rules.get("rows", 10)),
            columns=int(rules.get("columns", 10)),
            fleet=tuple(sorted(fleet, reverse=True)) or DEFAULT_FLEET,
            no_touching=rules.get("touchRule", "NO_TOUCHING_EVEN_DIAGONALLY") == "NO_TOUCHING_EVEN_DIAGONALLY",
            hit_keeps_turn=rules.get("turnAfterHitRule") != "HIT_SPENDS_SHOT" and modifier != "DUEL",
            shots_per_turn=max(1, int(rules.get("shotsPerTurn", 1))),
            shot_limit=int(rules.get("shotLimitPerPlayer", 100)),
            modifier=modifier,
        )

    @classmethod
    def for_modifier(cls, modifier: str | None) -> Rules:
        """Правила по умолчанию для модификатора — для симулятора."""
        base = cls(modifier=modifier)
        if modifier == "DUEL":
            return replace(base, hit_keeps_turn=False)
        if modifier == "THREE_MUSKETEERS":
            return replace(base, shots_per_turn=3)
        if modifier == "HUNGER_GAMES":
            return replace(base, shot_limit=40)
        return base
