"""Разведка: статистика по каждому боту платформы и его последний рейтинг.

Платформа отдаёт участнику реплей любого завершённого матча — расстановки обеих сторон и
все выстрелы. Модуль скачивает матчи, которых ещё нет на диске, и собирает из них профили ботов.

    python -m battleships.scouting            # докачать новые матчи, пересобрать профили, показать таблицу
    python -m battleships.scouting --offline  # только пересобрать из уже скачанного
    python -m battleships.scouting --bot Kraken

Вход — учётная запись участника из BATTLESHIPS_LOGIN и BATTLESHIPS_PASSWORD (файл .env).
Данные лежат в runtime/scouting/, в репозиторий не попадают.

Для выбора стратегии под соперника:

    profiles = load_profiles()
    profile = profile_for(profiles, state.opponent_info)   # None, если бота ещё не видели
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .model import OpponentInfo

BASE_URL = "https://hackathon.no-offence.ru/api/v1"
STORE = Path("runtime/scouting")
OPENING = 15  # сколько первых выстрелов партии считаем «дебютом» стрелка


class Platform:
    """HTTP API платформы под сессией участника."""

    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url
        try:
            import certifi  # на macOS у Python с python.org нет корневых сертификатов

            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def request(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Accept": "application/json"} | ({"Content-Type": "application/json"} if data else {})
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=20) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"{method} {path}: HTTP {error.code}") from None
        return payload.get("value")

    def get(self, path: str):
        return self.request("GET", path)

    def login(self, login: str, password: str) -> None:
        self.request("POST", "/session", {"login": login, "password": password})

    def logout(self) -> None:
        self.request("DELETE", "/session")


def sync(platform: Platform, store: Path = STORE, max_new: int = 400, full: bool = False) -> int:
    """Скачивает завершённые матчи, которых нет на диске, и свежий лидерборд. Возвращает число новых."""
    matches_dir = store / "matches"
    matches_dir.mkdir(parents=True, exist_ok=True)
    new, page, page_count = 0, 1, 1
    while page <= page_count and new < max_new:
        listing = platform.get(f"/matches?page={page}")
        page_count = listing["pageCount"]
        finished = [m["matchId"] for m in listing["matches"] if m["status"] == "FINISHED"]
        missing = [match_id for match_id in finished if not (matches_dir / f"{match_id}.json").exists()]
        for match_id in missing[: max_new - new]:
            detail = platform.get(f"/matches/{match_id}")
            (matches_dir / f"{match_id}.json").write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")
            new += 1
            time.sleep(0.05)  # не грузим платформу
        if finished and not missing and not full:
            break  # список идёт от новых к старым: дальше всё уже скачано
        page += 1
    snapshot = {"fetchedAt": datetime.now(timezone.utc).isoformat(), "bots": platform.get("/leaderboard")}
    (store / "leaderboard.json").write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    return new


def _key(side: dict) -> str:
    """Тот же ключ, что OpponentInfo.key в оболочке: bot:<UUID> или builtin:<уровень>."""
    return f"bot:{side['botId']}" if side["kind"] == "REMOTE_BOT" else f"builtin:{side['builtInBot']}"


def _new_profile(side: dict) -> dict:
    return {
        "key": _key(side), "kind": side["kind"], "botId": side.get("botId"), "builtInBot": side.get("builtInBot"),
        "team": side.get("name"), "botName": side.get("botName") or side.get("builtInBot"),
        "rating": None, "ratingAt": None, "rankingMatches": None,
        "matches": 0, "wins": 0, "losses": 0, "draws": 0,
        "ranking": {"matches": 0, "wins": 0},
        "vsUs": {"matches": 0, "wins": 0, "losses": 0, "draws": 0},
        "modifiers": Counter(), "endReasons": Counter(),
        "placements": 0, "uniquePlacements": set(), "placementCells": Counter(),
        "longCells": 0, "longCellsOnBorder": 0, "singles": 0, "singlesOnBorder": 0,
        "shots": 0, "hits": 0, "openingShots": 0, "openingShotsOnBorder": 0, "openingCells": Counter(),
        "firstShots": Counter(), "shotsToDestroy": [], "lastSeen": None,
        # То же, но только в партиях против ботов команд: против встроенных боты могут играть иначе.
        "vsBots": {"matches": 0, "placements": 0, "longCells": 0, "longCellsOnBorder": 0, "singles": 0,
                   "singlesOnBorder": 0, "openingShots": 0, "openingShotsOnBorder": 0},
    }


def build_profiles(store: Path = STORE, own_team: str | None = None) -> dict[str, dict]:
    """Собирает профили всех ботов из скачанных матчей и последнего лидерборда."""
    profiles: dict[str, dict] = {}
    for path in sorted((store / "matches").glob("*.json")):
        match = json.loads(path.read_text(encoding="utf-8"))
        events = match.get("events") or []
        rules = next((e["rules"] for e in events if e["type"] == "MATCH_STARTED" and e.get("rules")), None)
        if not rules:
            continue
        rows, columns = rules["rows"], rules["columns"]
        on_border = lambda cell: cell["row"] in (0, rows - 1) or cell["column"] in (0, columns - 1)
        sides = {side["seat"]: side for side in match["sides"]}
        kind = "RANKING" if any((s.get("rating") or {}).get("kFactor") not in (None, 10.0) for s in sides.values()) else "TRAINING"
        shots = {seat: [e for e in events if e["type"] == "SHOT" and e.get("seat") == seat] for seat in sides}
        for seat, side in sides.items():
            profile = profiles.setdefault(_key(side), _new_profile(side))
            opponent = next(s for other, s in sides.items() if other != seat)
            profile["team"], profile["botName"] = side.get("name"), side.get("botName") or side.get("builtInBot")
            profile["matches"] += 1
            buckets = [profile] + ([profile["vsBots"]] if opponent["kind"] == "REMOTE_BOT" else [])
            profile["vsBots"]["matches"] += len(buckets) - 1
            outcome = "draws" if match.get("winner") is None else "wins" if match["winner"] == seat else "losses"
            profile[outcome] += 1
            if kind == "RANKING":
                profile["ranking"]["matches"] += 1
                profile["ranking"]["wins"] += outcome == "wins"
            if own_team and opponent.get("name") == own_team:
                profile["vsUs"]["matches"] += 1
                profile["vsUs"][outcome] += 1
            profile["modifiers"][rules.get("modifier") or "CLASSIC"] += 1
            profile["endReasons"][match.get("endReason") or "?"] += 1
            if not profile["lastSeen"] or match["startedAt"] > profile["lastSeen"]:
                profile["lastSeen"] = match["startedAt"]
                formula = (side.get("rating") or {}).get("formula")
                if formula:
                    profile["rating"], profile["ratingAt"] = formula["ratingAfter"], match.get("finishedAt")

            placed = next((e["ships"] for e in events if e["type"] == "PLACEMENT_ACCEPTED" and e.get("seat") == seat), None)
            if placed:
                for bucket in buckets:
                    bucket["placements"] += 1
                ships = [[(c["row"], c["column"]) for c in ship["cells"]] for ship in placed]
                profile["uniquePlacements"].add(json.dumps(sorted(sorted(ship) for ship in ships)))
                for ship in placed:
                    for cell in ship["cells"]:
                        profile["placementCells"][f"{cell['row']},{cell['column']}"] += 1
                    border = sum(on_border(cell) for cell in ship["cells"])
                    for bucket in buckets:
                        if len(ship["cells"]) == 1:
                            bucket["singles"] += 1
                            bucket["singlesOnBorder"] += border
                        else:
                            bucket["longCells"] += len(ship["cells"])
                            bucket["longCellsOnBorder"] += border

            own = shots[seat]
            profile["shots"] += len(own)
            profile["hits"] += sum(e["result"] in ("HIT", "SUNK") for e in own)
            for event in own[:OPENING]:
                for bucket in buckets:
                    bucket["openingShots"] += 1
                    bucket["openingShotsOnBorder"] += on_border(event["cell"])
                profile["openingCells"][f"{event['cell']['row']},{event['cell']['column']}"] += 1
            if own:
                profile["firstShots"][f"{own[0]['cell']['row']},{own[0]['cell']['column']}"] += 1
            if match.get("winner") == seat and match.get("endReason") == "FLEET_DESTROYED" and not rules.get("modifier"):
                profile["shotsToDestroy"].append(len(own))

    leaderboard = store / "leaderboard.json"
    if leaderboard.exists():
        snapshot = json.loads(leaderboard.read_text(encoding="utf-8"))
        for bot in snapshot["bots"]:
            profile = profiles.setdefault(f"bot:{bot['botId']}", _new_profile(
                {"kind": "REMOTE_BOT", "botId": bot["botId"], "name": bot["teamName"], "botName": bot["botName"]}))
            profile.update(team=bot["teamName"], botName=bot["botName"], rating=bot["rating"],
                           ratingAt=snapshot["fetchedAt"], rankingMatches=bot["rankingMatchCount"])

    for profile in profiles.values():
        share = lambda part, whole: round(part / whole, 3) if whole else None
        destroy = profile.pop("shotsToDestroy")
        profile["uniquePlacements"] = len(profile["uniquePlacements"])
        profile["winRate"] = share(profile["wins"], profile["matches"])
        profile["accuracy"] = share(profile["hits"], profile["shots"])
        for bucket in (profile, profile["vsBots"]):
            bucket["longShipsOnBorder"] = share(bucket["longCellsOnBorder"], bucket["longCells"])
            bucket["singlesOnBorderShare"] = share(bucket["singlesOnBorder"], bucket["singles"])
            bucket["openingOnBorder"] = share(bucket["openingShotsOnBorder"], bucket["openingShots"])
        profile["shotsToDestroyMean"] = round(sum(destroy) / len(destroy), 1) if destroy else None
        profile["shotsToDestroyGames"] = len(destroy)
        for counter in ("modifiers", "endReasons", "placementCells", "openingCells", "firstShots"):
            profile[counter] = dict(profile[counter].most_common())
    return profiles


def save_profiles(profiles: dict[str, dict], store: Path = STORE) -> Path:
    path = store / "profiles.json"
    path.write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_profiles(store: Path = STORE) -> dict[str, dict]:
    path = store / "profiles.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def profile_for(profiles: dict[str, dict], opponent) -> dict | None:
    """Профиль по state.opponent_info оболочки или по полю opponent из MATCH_STARTED."""
    if not opponent:
        return None
    if isinstance(opponent, dict):
        opponent = OpponentInfo.from_wire(opponent)
    return profiles.get(opponent.key) if opponent.key else None


def _percent(value) -> str:
    return "  —" if value is None else f"{value * 100:3.0f}"


def table(profiles: dict[str, dict]) -> str:
    lines = [
        "рейтинг  матчей побед% | выстрелов | расстановки: всего разных  длинные  одиночные | дебют    | против ботов команд:       | бот",
        "                        | до победы |                          у края%   у края% | у края%  | матчей длинные% дебют%     |",
    ]
    for p in sorted(profiles.values(), key=lambda p: -(p["rating"] or 0)):
        destroy = f"{p['shotsToDestroyMean']:5.1f}" if p["shotsToDestroyMean"] else "    —"
        rating = f"{p['rating']:7.1f}" if p["rating"] else "      —"
        lines.append(
            f"{rating} {p['matches']:7d}   {_percent(p['winRate'])} |     {destroy} | {p['placements']:18d} {p['uniquePlacements']:6d}"
            f"      {_percent(p['longShipsOnBorder'])}       {_percent(p['singlesOnBorderShare'])} |     {_percent(p['openingOnBorder'])}  | "
            f"{p['vsBots']['matches']:6d}     {_percent(p['vsBots']['longShipsOnBorder'])}    {_percent(p['vsBots']['openingOnBorder'])}     | "
            f"{p['botName']} ({p['team'] or 'встроенный'})"
        )
    return "\n".join(lines)


def card(profile: dict, rows: int = 10, columns: int = 10) -> str:
    """Подробная карточка бота: куда ставит корабли и куда бьёт в дебюте."""
    def grid(counts: dict, total: int) -> list[str]:
        return ["  " + " ".join(f"{round(100 * counts.get(f'{r},{c}', 0) / total) if total else 0:3d}" for c in range(columns))
                for r in range(rows)]
    head = {k: profile[k] for k in ("botName", "team", "rating", "matches", "wins", "losses", "draws", "ranking", "vsUs",
                                    "modifiers", "endReasons", "accuracy", "shotsToDestroyMean", "placements", "uniquePlacements")}
    lines = [json.dumps(head, ensure_ascii=False)]
    lines += ["Клетка занята кораблём, % расстановок:"] + grid(profile["placementCells"], profile["placements"])
    lines += [f"Клетка обстреляна в первые {OPENING} выстрелов, % партий:"] + grid(profile["openingCells"], profile["matches"])
    lines += ["Первый выстрел: " + ", ".join(f"({cell}) ×{n}" for cell, n in list(profile["firstShots"].items())[:6])]
    return "\n".join(lines)


def main() -> None:
    from .__main__ import load_env

    parser = argparse.ArgumentParser(description="Разведка ботов по реплеям платформы")
    parser.add_argument("--offline", action="store_true", help="не ходить на платформу, пересобрать из скачанного")
    parser.add_argument("--max-new", type=int, default=400, help="сколько новых матчей скачать за запуск")
    parser.add_argument("--full", action="store_true", help="пройти весь список матчей, а не только новые сверху")
    parser.add_argument("--bot", help="показать карточку бота: часть имени бота или команды")
    parser.add_argument("--team", default="Нейро Мугивары", help="наша команда — для счёта личных встреч")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    if not args.offline:
        load_env(args.env_file)
        login, password = os.environ.get("BATTLESHIPS_LOGIN"), os.environ.get("BATTLESHIPS_PASSWORD")
        if not login or not password:
            parser.error("Нужны BATTLESHIPS_LOGIN и BATTLESHIPS_PASSWORD в окружении или в .env")
        platform = Platform()
        platform.login(login, password)
        try:
            print(f"Скачано новых матчей: {sync(platform, STORE, args.max_new, args.full)}")
        finally:
            platform.logout()

    profiles = build_profiles(STORE, args.team)
    path = save_profiles(profiles)
    total = len(list((STORE / "matches").glob("*.json")))
    print(f"Матчей на диске: {total}, ботов: {len(profiles)}, профили: {path}")
    if args.bot:
        needle = args.bot.lower()
        for profile in profiles.values():
            if needle in f"{profile['botName']} {profile['team']}".lower():
                print(card(profile))
    else:
        print(table(profiles))


if __name__ == "__main__":
    main()
