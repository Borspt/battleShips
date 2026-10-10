"""Выбор расстановки и поправки в стрельбе под соперника по его профилю из разведки."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from ..placement import edge_placement, interior_placement, random_placement
from ..scouting import STORE, profile_for
from .probability import HARD_BIAS, EdgeBias

# Подобрано в симуляторе: против флота у краёв 52 выстрела до потопления вместо 63 без поправки,
# против флота, стоящего у краёв в половине партий, — 54 вместо 59, против случайного — 56 вместо 55.
MEDIUM_BIAS = EdgeBias(edge=3.0, corner=6.0, single_edge=0.7)

MIN_PLACEMENTS = 8  # меньше расстановок в профиле — привычкам соперника не верим
MIN_MATCHES = 5


@dataclass(frozen=True)
class Plan:
    placement: Callable
    edge_bias: EdgeBias | None
    reason: str


def choose_plan(modifier: str | None, opponent, profile: dict | None) -> Plan:
    """opponent — state.opponent_info оболочки; profile — профиль из разведки или None.

    Мы для соперника — бот команды, поэтому смотрим, как он играет против ботов команд
    (profile["vsBots"]): против встроенного HARD многие играют иначе. Рейтинг соперника
    на выбор пока не влияет: связи между рейтингом и привычками в данных не видно.
    """
    placement, bias, reasons = random_placement, None, []
    versus_bots = (profile or {}).get("vsBots") or {}

    # Поправка в стрельбе — только по расстановкам против ботов команд: по реплеям видно, что
    # против встроенного HARD лидеры ставят флот у краёв, а против ботов команд — нет.
    if versus_bots.get("placements", 0) >= MIN_PLACEMENTS:
        share = versus_bots.get("longShipsOnBorder") or 0.0
        if share >= 0.9:
            bias = HARD_BIAS
        elif share >= 0.55:
            bias = MEDIUM_BIAS
        if bias:
            # Однопалубные на крайних клетках при случайной расстановке — 36% случаев.
            if (versus_bots.get("singlesOnBorderShare") or 0.0) >= 0.36:
                bias = replace(bias, single_edge=1.0)
            reasons.append(f"против ботов команд ставит {share:.0%} длинных кораблей у края")
    elif getattr(opponent, "built_in_bot", None) == "HARD":
        bias = HARD_BIAS
        reasons.append("встроенный HARD ставит флот у краёв")
    if modifier == "SPEEDRUN":  # флот расставляет платформа, так же, как HARD
        bias = HARD_BIAS
        reasons.append("в «Спидране» флот стоит как у HARD")

    # Расстановка внутри поля проигрывает стрелку от центра 6 выстрелов, поэтому ставим её, только
    # если соперник начинает с краёв именно против ботов команд. Стрельба от краёв против HARD
    # ничего не значит: так выгодно бить по его флоту.
    if versus_bots.get("matches", 0) >= MIN_MATCHES and (versus_bots.get("openingOnBorder") or 0.0) >= 0.85:
        placement = interior_placement
        reasons.append(f"против ботов команд начинает стрельбу с краёв ({versus_bots['openingOnBorder']:.0%})")
    elif profile and profile.get("matches", 0) >= MIN_MATCHES and (profile.get("openingOnBorder") or 1.0) <= 0.25:
        if versus_bots.get("matches", 0) < MIN_MATCHES or (versus_bots.get("openingOnBorder") or 0.0) <= 0.25:
            placement = edge_placement
            reasons.append(f"начинает стрельбу от центра ({profile['openingOnBorder']:.0%} дебютных выстрелов у края)")
    elif not profile and getattr(opponent, "built_in_bot", None) == "HARD":
        placement = edge_placement
        reasons.append("встроенный HARD стреляет от центра")

    return Plan(placement, bias, "; ".join(reasons) or "нейтрально: привычек у соперника не видно")


class ProfileBook:
    """Профили из runtime/scouting/profiles.json; файл перечитывается, когда разведка его обновила."""

    def __init__(self, path: Path = STORE / "profiles.json"):
        self.path = path
        self.loaded_at = None
        self.profiles: dict[str, dict] = {}

    def lookup(self, opponent) -> dict | None:
        try:
            modified = self.path.stat().st_mtime
            if modified != self.loaded_at:
                self.profiles = json.loads(self.path.read_text(encoding="utf-8"))
                self.loaded_at = modified
        except (OSError, ValueError):
            pass  # профилей нет или файл пишется прямо сейчас — играем по тому, что уже загружено
        return profile_for(self.profiles, opponent)
