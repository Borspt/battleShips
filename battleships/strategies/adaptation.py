"""Выбор расстановки и поправки в стрельбе под соперника по его профилю из разведки."""

from __future__ import annotations

import json
import os
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
# «Спидран»: где встроенный HARD ставит однопалубные — доли на 10 000 по 3999 его расстановкам из реплеев,
# усреднённые по симметриям поля. На краю их не бывает, чаще всего — клетки (2,3), (3,2) и симметричные.
# На 300 расстановках HARD это 50,2 выстрела до потопления вместо 52,6 с одной поправкой на края.
SPEEDRUN_SINGLES = (
      0,   0,   0,   0,   0,   0,   0,   0,   0,   0,
      0,  16,  87,  73,  61,  61,  73,  87,  16,   0,
      0,  87, 166, 329, 252, 252, 329, 166,  87,   0,
      0,  73, 329, 160, 228, 228, 160, 329,  73,   0,
      0,  61, 252, 228,  98,  98, 228, 252,  61,   0,
      0,  61, 252, 228,  98,  98, 228, 252,  61,   0,
      0,  73, 329, 160, 228, 228, 160, 329,  73,   0,
      0,  87, 166, 329, 252, 252, 329, 166,  87,   0,
      0,  16,  87,  73,  61,  61,  73,  87,  16,   0,
      0,   0,   0,   0,   0,   0,   0,   0,   0,   0,
)
SPEEDRUN_SINGLES_SMOOTHING = 0.1  # запас на случай, если платформа расставляет не совсем как HARD
# Во всех 1426 расстановках «Спидрана» из реплеев однопалубных ровно по одному в каждой четверти поля
# (строки и столбцы 1–4 и 5–8), на краю их нет. Так их и ищут лидеры режима: 19–22 выстрела внутрь поля
# против наших 26. Вес клетки внутри четверти — по расстоянию до двух ближних краёв поля, доли в процентах.
_QUADRANT_SHARE = {(1, 1): 1.1, (1, 2): 2.95, (1, 3): 2.3, (1, 4): 2.25, (2, 2): 10.8, (2, 3): 11.0,
                   (2, 4): 8.7, (3, 3): 10.8, (3, 4): 8.05, (4, 4): 6.7}
SPEEDRUN_QUADRANT_PRIOR = tuple(
    _QUADRANT_SHARE.get(tuple(sorted((min(row, 9 - row), min(column, 9 - column)))), 0.0)
    for row in range(10) for column in range(10))
# «Голодные игры»: у бота с любимыми местами бьём туда, где его флот стоял в последних партиях. На реплеях
# при разбросе от 0,095 это 8,1 попадания за 20 выстрелов вместо 6,5; у случайной расстановки — шум и минус.
FLEET_SPREAD = 0.095
MIN_FLEET_PLACEMENTS = 20
# Карта годится, только если она предсказывала последние расстановки бота. Боты, которые видят наши выстрелы
# в реплеях, уходят с прежних мест: против «Забудь весь контекст» карта давала 0–5 попаданий, 1 победа из 9.
FLEET_LIFT = 1.15
# На живых партиях карта не помогла: 48% очков с ней против 55% без неё (118 партий), попаданий поровну.
# Соперники меняют расстановку быстрее, чем обновляются профили. Выключена до нового замера.
FLEET_MAP_ENABLED = False
# Бот попадает по нам заметно чаще, чем по остальным, — значит, выучил, где мы прячемся: ставим флот случайно.
LEARNED_US_MARGIN = 0.5
FLEET_SMOOTHING = 0.1


@dataclass(frozen=True)
class Plan:
    placement: Callable
    edge_bias: EdgeBias | None
    reason: str
    cell_prior: tuple[float, ...] | None = None  # вес каждой клетки поля в стрельбе: где соперник ставит флот
    single_prior: tuple[float, ...] | None = None  # то же, но только для однопалубных
    single_per_quadrant: bool = False  # в каждой четверти поля ровно один однопалубный («Спидран»)


def choose_plan(modifier: str | None, opponent, profile: dict | None, typical_early: dict | None = None) -> Plan:
    """opponent — state.opponent_info оболочки; profile — профиль из разведки или None.

    Мы для соперника — бот команды, поэтому смотрим, как он играет против ботов команд
    (profile["vsBots"]): против встроенного HARD многие играют иначе. Рейтинг соперника
    на выбор пока не влияет: связи между рейтингом и привычками в данных не видно.
    """
    placement, bias, reasons, cell_prior, single_prior = random_placement, None, [], None, None
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
    # BATTLESHIPS_FRESH_PROFILE=1: «последние партии с нами» могут быть на часы старше «последних
    # партий» (с соперником давно не играли) — тогда берём более свежий раздел.
    if habits is not None and source == "в последних партиях с нами" and os.environ.get("BATTLESHIPS_FRESH_PROFILE") == "1":
        newer = (profile or {}).get("recent") or {}
        if newer.get("placements", 0) >= MIN_PLACEMENTS and str(newer.get("since", "")) > str(habits.get("since", "")):
            habits, source = newer, "в последних партиях"
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
        single_prior = SPEEDRUN_QUADRANT_PRIOR

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
    # BATTLESHIPS_EVASIVE=1: прятать флот от первых выстрелов соперника во всех режимах, где флот
    # ставим мы (кроме «Спидрана»). 10.10 в «Птичьем коробе» «Твоя бабушка» почти не стреляла по
    # краям (1 выстрел из 20) и попадала по нашему флоту внутри поля 9,4 раза из 20.
    evasive_everywhere = os.environ.get("BATTLESHIPS_EVASIVE") == "1" and modifier != "SPEEDRUN"
    if modifier == "HUNGER_GAMES" or evasive_everywhere:
        early, origin = typical_early, "ботов в среднем"
        for section, minimum, label in (("recentVsUs", MIN_RECENT_VS_US, "соперника в партиях с нами"),
                                        ("recent", MIN_PLACEMENTS, "соперника в последних партиях")):
            candidate = (profile or {}).get(section) or {}
            if candidate.get("matches", 0) >= minimum and candidate.get("earlyCells"):
                early, origin = candidate["earlyCells"], label
                break
        recent = (profile or {}).get("recent") or {}
        if (FLEET_MAP_ENABLED and recent.get("placements", 0) >= MIN_FLEET_PLACEMENTS and (recent.get("fleetSpread") or 0.0) >= FLEET_SPREAD
                and (recent.get("fleetLift") or 0.0) >= FLEET_LIFT):
            occupied = recent.get("fleetCells") or {}
            cell_prior = tuple((occupied.get(f"{row},{column}", 0.0) + FLEET_SMOOTHING) / (0.2 + FLEET_SMOOTHING)
                               for row in range(10) for column in range(10))
            bias = None  # карта клеток точнее поправки на края и заменяет её
            reasons.append(f"стрельба по карте его расстановок (разброс {recent['fleetSpread']:.3f})")
        with_us = (profile or {}).get("recentVsUs") or {}
        hits_us, hits_all = with_us.get("earlyHits"), recent.get("earlyHits")
        if (with_us.get("matches", 0) >= MIN_RECENT_VS_US and hits_us is not None and hits_all is not None
                and hits_us > hits_all + LEARNED_US_MARGIN):
            early = None
            reasons.append(f"расстановка случайная: по нам он попадает {hits_us:.1f} раза, по остальным {hits_all:.1f}")
        if early:
            danger = {tuple(int(part) for part in cell.split(",")): share for cell, share in early.items()}
            placement = partial(evasive_placement, danger=danger)
            placement.__name__ = "evasive_placement"
            reasons.append(f"флот спрятан от первых выстрелов {origin}")

    return Plan(placement, bias, "; ".join(reasons) or "нейтрально: привычек у соперника не видно", cell_prior,
                single_prior, modifier == "SPEEDRUN")


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
