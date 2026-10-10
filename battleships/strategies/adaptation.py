"""Выбор расстановки и поправки в стрельбе под соперника по его профилю из разведки."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Callable

from ..placement import edge_placement, evasive_placement, interior_placement, random_placement
from ..scouting import PROFILES, profile_for
from .probability import HARD_BIAS, EdgeBias

# Подобрано в симуляторе: против флота у краёв 52 выстрела до потопления вместо 63 без поправки,
# против флота, стоящего у краёв в половине партий, — 54 вместо 59, против случайного — 56 вместо 55.
MEDIUM_BIAS = EdgeBias(edge=3.0, corner=6.0, single_edge=0.7)

MIN_PLACEMENTS = 8  # меньше расстановок в профиле — привычкам соперника не верим
MIN_RECENT_VS_US = 5  # партий с нами хватает меньше: это ровно та игра, которую нам предстоит встретить
MEDIUM_SHARE = 0.45  # при случайной расстановке у края около трети клеток длинных кораблей
MIN_MATCHES = 5
# При случайной расстановке у края 47% однопалубных. На 164 расстановках «Низких ожиданий» (73% у края)
# поправка экономит 2,3 выстрела, против случайной расстановки она же стоит 2,5 — поэтому порог высокий.
SINGLES_SHARE = 0.6


@dataclass(frozen=True)
class Plan:
    placement: Callable
    edge_bias: EdgeBias | None
    reason: str


def choose_plan(modifier: str | None, opponent, profile: dict | None, typical_early: dict | None = None) -> Plan:
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
        # Однопалубные: во сколько раз крайняя клетка занята чаще внутренней. На краю 36% клеток поля.
        singles = habits.get("singlesOnBorderShare")
        single_edge = None
        if singles is not None and 0 < singles < 1:
            single_edge = round(min(4.0, max(0.5, (singles / 0.36) / ((1 - singles) / 0.64))), 2)
        if bias:
            if single_edge is not None:
                bias = replace(bias, single_edge=single_edge)
            reasons.append(f"{source} ставит {share:.0%} длинных кораблей у края")
        elif single_edge is not None and singles >= SINGLES_SHARE:
            # Длинные корабли стоят как попало, а однопалубные прижаты к краю — так играют «Низкие ожидания».
            bias = EdgeBias(single_edge=single_edge)
            reasons.append(f"{source} ставит {singles:.0%} однопалубных у края")
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

    # «Голодные игры»: выстрелов мало, флот никто не топит, считаются попадания. Прячем корабли от
    # первых выстрелов этого соперника, а если его не знаем — от первых выстрелов всех ботов в среднем.
    if modifier == "HUNGER_GAMES":
        early, origin = typical_early, "ботов в среднем"
        for section, minimum, label in (("recentVsUs", MIN_RECENT_VS_US, "соперника в партиях с нами"),
                                        ("recent", MIN_PLACEMENTS, "соперника в последних партиях")):
            candidate = (profile or {}).get(section) or {}
            if candidate.get("matches", 0) >= minimum and candidate.get("earlyCells"):
                early, origin = candidate["earlyCells"], label
                break
        if early:
            danger = {tuple(int(part) for part in cell.split(",")): share for cell, share in early.items()}
            placement = partial(evasive_placement, danger=danger)
            placement.__name__ = "evasive_placement"
            reasons.append(f"флот спрятан от первых выстрелов {origin}")

    return Plan(placement, bias, "; ".join(reasons) or "нейтрально: привычек у соперника не видно")


class ProfileBook:
    """Профили из scouting/profiles.json; файл перечитывается, когда он изменился (разведка или git pull)."""

    def __init__(self, path: Path = PROFILES):
        self.path = path
        self.loaded_at = None
        self.profiles: dict[str, dict] = {}
        self._typical, self._typical_for = None, object()

    def rating_of(self, bot_name: str) -> float | None:
        """Последний известный рейтинг бота по имени — так стратегия узнаёт собственный рейтинг."""
        self.lookup(None)
        return next((p.get("rating") for p in self.profiles.values() if p.get("botName") == bot_name), None)

    def typical_early(self) -> dict | None:
        """Куда боты команд в среднем бьют первыми выстрелами — для соперника, которого мы не знаем."""
        self.lookup(None)
        if self._typical_for != self.loaded_at:
            totals, bots = {}, 0
            for profile in self.profiles.values():
                early = (profile.get("recent") or {}).get("earlyCells")
                if profile.get("kind") == "REMOTE_BOT" and early:
                    bots += 1
                    for cell, share in early.items():
                        totals[cell] = totals.get(cell, 0.0) + share
            self._typical = {cell: total / bots for cell, total in totals.items()} if bots else None
            self._typical_for = self.loaded_at
        return self._typical

    def lookup(self, opponent) -> dict | None:
        try:
            modified = self.path.stat().st_mtime
            if modified != self.loaded_at:
                self.profiles = json.loads(self.path.read_text(encoding="utf-8"))
                self.loaded_at = modified
        except (OSError, ValueError):
            pass  # профилей нет или файл пишется прямо сейчас — играем по тому, что уже загружено
        return profile_for(self.profiles, opponent)
