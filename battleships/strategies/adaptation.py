"""Выбор расстановки и поправки в стрельбе под соперника по его профилю из разведки."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from ..placement import edge_placement, interior_placement, random_placement
from ..scouting import PROFILES, profile_for
from .probability import HARD_BIAS, EdgeBias

# Подобрано в симуляторе: против флота у краёв 52 выстрела до потопления вместо 63 без поправки,
# против флота, стоящего у краёв в половине партий, — 54 вместо 59, против случайного — 56 вместо 55.
MEDIUM_BIAS = EdgeBias(edge=3.0, corner=6.0, single_edge=0.7)

MIN_PLACEMENTS = 8  # меньше расстановок в профиле — привычкам соперника не верим
MIN_RECENT_VS_US = 5  # партий с нами хватает меньше: это ровно та игра, которую нам предстоит встретить
MEDIUM_SHARE = 0.45  # при случайной расстановке у края около трети клеток длинных кораблей
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

    # Поправка в стрельбе — по расстановкам против ботов команд, и чем свежее, тем важнее: сначала
    # последние партии с нами, потом последние партии с кем угодно, потом всё за день. Соперники
    # меняют расстановку в течение дня: «Низкие ожидания» за день в среднем ставили у края 31%
    # клеток длинных кораблей, а против нас в «Трёх мушкетёрах» — 51–59%.
    habits, source = None, ""
    for section, minimum, label in (("recentVsUs", MIN_RECENT_VS_US, "в последних партиях с нами"),
                                    ("recent", MIN_PLACEMENTS, "в последних партиях"),
                                    ("vsBots", MIN_PLACEMENTS, "против ботов команд")):
        candidate = (profile or {}).get(section) or {}
        if candidate.get("placements", 0) >= minimum:
            habits, source = candidate, label
            break
    if habits:
        share = habits.get("longShipsOnBorder") or 0.0
        if share >= 0.9:
            bias = HARD_BIAS
        elif share >= MEDIUM_SHARE:
            bias = MEDIUM_BIAS
        if bias:
            # Однопалубные: во сколько раз крайняя клетка занята чаще внутренней. На краю 36% клеток поля.
            singles = habits.get("singlesOnBorderShare")
            if singles is not None and 0 < singles < 1:
                bias = replace(bias, single_edge=round(min(4.0, max(0.5, (singles / 0.36) / ((1 - singles) / 0.64))), 2))
            reasons.append(f"{source} ставит {share:.0%} длинных кораблей у края")
    elif getattr(opponent, "built_in_bot", None) == "HARD":
        bias = HARD_BIAS
        reasons.append("встроенный HARD ставит флот у краёв")
    if modifier == "SPEEDRUN":  # флот расставляет платформа, так же, как HARD
        bias = HARD_BIAS
        reasons.append("в «Спидране» флот стоит как у HARD")

    # Расстановку подстраиваем только под встроенных ботов: они не меняются. Боты команд видят
    # наши реплеи и подстраиваются сами: в «Трёх мушкетёрах» флот по краям проиграл 9 партий из 10
    # ботам, которые по профилю «стреляли от центра», а против нас начали бить только в края.
    # Против них расстановка всегда случайная — её нельзя выучить.
    if getattr(opponent, "built_in_bot", None):
        if profile and profile.get("matches", 0) >= MIN_MATCHES and (profile.get("openingOnBorder") or 1.0) <= 0.25:
            placement = edge_placement
            reasons.append(f"начинает стрельбу от центра ({profile['openingOnBorder']:.0%} дебютных выстрелов у края)")
        elif profile and profile.get("matches", 0) >= MIN_MATCHES and (profile.get("openingOnBorder") or 0.0) >= 0.85:
            placement = interior_placement
            reasons.append(f"начинает стрельбу с краёв ({profile['openingOnBorder']:.0%})")
        elif not profile and opponent.built_in_bot == "HARD":
            placement = edge_placement
            reasons.append("встроенный HARD стреляет от центра")

    return Plan(placement, bias, "; ".join(reasons) or "нейтрально: привычек у соперника не видно")


class ProfileBook:
    """Профили из scouting/profiles.json; файл перечитывается, когда он изменился (разведка или git pull)."""

    def __init__(self, path: Path = PROFILES):
        self.path = path
        self.loaded_at = None
        self.profiles: dict[str, dict] = {}

    def rating_of(self, bot_name: str) -> float | None:
        """Последний известный рейтинг бота по имени — так стратегия узнаёт собственный рейтинг."""
        self.lookup(None)
        return next((p.get("rating") for p in self.profiles.values() if p.get("botName") == bot_name), None)

    def lookup(self, opponent) -> dict | None:
        try:
            modified = self.path.stat().st_mtime
            if modified != self.loaded_at:
                self.profiles = json.loads(self.path.read_text(encoding="utf-8"))
                self.loaded_at = modified
        except (OSError, ValueError):
            pass  # профилей нет или файл пишется прямо сейчас — играем по тому, что уже загружено
        return profile_for(self.profiles, opponent)
