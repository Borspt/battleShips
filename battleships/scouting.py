"""Разведка: статистика по каждому боту платформы и его последний рейтинг.

Платформа отдаёт участнику реплей любого завершённого матча — расстановки обеих сторон и
все выстрелы. Модуль скачивает матчи, которых ещё нет на диске, и собирает из них профили ботов.

    python -m battleships.scouting            # докачать новые матчи, пересобрать профили, показать таблицу
    python -m battleships.scouting --offline  # только пересобрать из уже скачанного
    python -m battleships.scouting --bot Kraken
    python -m battleships.scouting --plans    # какие планы выбирал наш бот и чем кончились партии
    python -m battleships.scouting --watch 300   # автообновление раз в 5 минут, пока не остановят

Вход — учётная запись участника из BATTLESHIPS_LOGIN и BATTLESHIPS_PASSWORD (файл .env).
Скачанные реплеи лежат в runtime/scouting/ и в репозиторий не попадают: это десятки мегабайт.
Готовые профили — в scouting/profiles.json, этот файл в репозитории: после запуска разведки
его надо закоммитить, чтобы профили получила вся команда.

Для выбора стратегии под соперника:

    profiles = load_profiles()
    profile = profile_for(profiles, state.opponent_info)   # None, если бота ещё не видели
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import statistics
import os
import ssl
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .model import OpponentInfo

BASE_URL = "https://hackathon.no-offence.ru/api/v1"
STORE = Path("runtime/scouting")  # реплеи и лидерборд, локально
PROFILES = Path("scouting/profiles.json")  # профили, в репозитории
DOWNLOADS = 4  # одновременных запросов к платформе
OPENING = 15  # сколько первых выстрелов партии считаем «дебютом» стрелка
EARLY = 20  # столько выстрелов у игрока в «Голодных играх»: куда бот бьёт первыми, там флот не ставим
RECENT = 25  # сколько последних партий против ботов команд идёт в раздел recent профиля
RECENT_VS_US = 10  # и сколько последних партий против нашей команды — в recentVsUs


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
        for attempt in range(4):
            try:
                with self.opener.open(request, timeout=20) as response:
                    return json.load(response).get("value")
            except urllib.error.HTTPError as error:
                raise RuntimeError(f"{method} {path}: HTTP {error.code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:  # сеть моргнула — пробуем ещё
                if attempt == 3:
                    raise RuntimeError(f"{method} {path}: сеть недоступна ({type(error).__name__})") from None
                time.sleep(1 + attempt)

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
        def download(match_id: str) -> None:
            detail = platform.get(f"/matches/{match_id}")
            (matches_dir / f"{match_id}.json").write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")

        batch = missing[: max_new - new]
        with ThreadPoolExecutor(DOWNLOADS) as pool:
            list(pool.map(download, batch))
        new += len(batch)
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
    history: dict[str, list] = {}  # по каждому боту — его партии против ботов команд, для разделов recent
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
            if opponent["kind"] == "REMOTE_BOT":
                long_ships = [ship["cells"] for ship in placed or [] if len(ship["cells"]) > 1]
                singles = [ship["cells"][0] for ship in placed or [] if len(ship["cells"]) == 1]
                history.setdefault(_key(side), []).append({
                    "at": match["startedAt"], "vsUs": bool(own_team) and opponent.get("name") == own_team,
                    "placed": bool(placed),
                    "long": sum(len(cells) for cells in long_ships),
                    "longBorder": sum(on_border(cell) for cells in long_ships for cell in cells),
                    "singles": len(singles), "singlesBorder": sum(on_border(cell) for cell in singles),
                    "opening": len(own[:OPENING]), "openingBorder": sum(on_border(e["cell"]) for e in own[:OPENING]),
                    "shotCells": {f"{e['cell']['row']},{e['cell']['column']}" for e in own},
                    "earlyCells": {f"{e['cell']['row']},{e['cell']['column']}" for e in own[:EARLY]},
                    "fleetCells": {f"{c['row']},{c['column']}" for ship in placed or [] for c in ship["cells"]},
                })

    leaderboard = store / "leaderboard.json"
    if leaderboard.exists():
        snapshot = json.loads(leaderboard.read_text(encoding="utf-8"))
        for bot in snapshot["bots"]:
            profile = profiles.setdefault(f"bot:{bot['botId']}", _new_profile(
                {"kind": "REMOTE_BOT", "botId": bot["botId"], "name": bot["teamName"], "botName": bot["botName"]}))
            profile.update(team=bot["teamName"], botName=bot["botName"], rating=bot["rating"],
                           ratingAt=snapshot["fetchedAt"], rankingMatches=bot["rankingMatchCount"])

    share = lambda part, whole: round(part / whole, 3) if whole else None

    def fleet_lift(games: list, last: int = 8) -> float | None:
        """Насколько карта прошлых расстановок бота предсказывает его следующую: 1 — никак, меньше 1 — он уходит
        с прежних мест. Для каждой из последних партий карта строится только по партиям до неё."""
        placed = [game for game in games if game["placed"]]
        scores = []
        for number in range(max(10, len(placed) - last), len(placed)):
            train = placed[max(0, number - RECENT):number]
            seen = Counter(cell for game in train for cell in game["fleetCells"])
            mean = sum(seen.values()) / 100
            cells = placed[number]["fleetCells"]
            if mean and cells:
                scores.append(sum(seen.get(cell, 0) for cell in cells) / len(cells) / mean)
        return round(sum(scores) / len(scores), 3) if len(scores) >= 5 else None

    def window(games: list) -> dict:
        """Привычки бота по последним партиям: соперники меняют игру в течение дня и под нас лично."""
        cells = Counter(cell for game in games for cell in game["shotCells"])
        early = Counter(cell for game in games for cell in game["earlyCells"])
        fleet = Counter(cell for game in games for cell in game["fleetCells"])
        placed = sum(game["placed"] for game in games)
        occupied = [fleet.get(f"{row},{column}", 0) / placed for row in range(10) for column in range(10)] if placed else []
        return {
            "matches": len(games), "placements": sum(game["placed"] for game in games),
            "since": games[0]["at"] if games else None,
            "singles": sum(game["singles"] for game in games),
            "longShipsOnBorder": share(sum(g["longBorder"] for g in games), sum(g["long"] for g in games)),
            "singlesOnBorderShare": share(sum(g["singlesBorder"] for g in games), sum(g["singles"] for g in games)),
            "openingOnBorder": share(sum(g["openingBorder"] for g in games), sum(g["opening"] for g in games)),
            # Доля партий, в которых бот хоть раз выстрелил в клетку: по ней ставятся мины.
            "shotCells": {cell: round(count / len(games), 2) for cell, count in cells.most_common()},
            # Доля партий, в которых клетка попала в первые EARLY выстрелов бота: по ней прячем флот в «Голодных играх».
            "earlyCells": {cell: round(count / len(games), 2) for cell, count in early.most_common()},
            # Доля расстановок, в которых клетка занята кораблём, и разброс этой доли по полю: у случайной
            # расстановки на 25 партиях разброс около 0,08, у бота с любимыми местами — от 0,10.
            "fleetCells": {cell: round(count / placed, 2) for cell, count in fleet.most_common()} if placed else {},
            "fleetSpread": round(statistics.pstdev(occupied), 3) if occupied else None,
        }

    for key, profile in profiles.items():
        games = sorted(history.get(key, []), key=lambda game: game["at"])
        profile["recent"] = window(games[-RECENT:])
        profile["recent"]["fleetLift"] = fleet_lift(games)
        profile["recentVsUs"] = window([game for game in games if game["vsUs"]][-RECENT_VS_US:])
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


def save_profiles(profiles: dict[str, dict], path: Path = PROFILES) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = dict(sorted(profiles.items()))  # стабильный порядок — читаемые диффы в git
    draft = path.with_suffix(".tmp")  # пишем рядом и подменяем: играющий бот не увидит половину файла
    draft.write_text(json.dumps(ordered, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    draft.replace(path)
    return path


def load_profiles(path: Path = PROFILES) -> dict[str, dict]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def profile_for(profiles: dict[str, dict], opponent) -> dict | None:
    """Профиль по state.opponent_info оболочки или по полю opponent из MATCH_STARTED."""
    if not opponent:
        return None
    if isinstance(opponent, dict):
        opponent = OpponentInfo.from_wire(opponent)
    return profiles.get(opponent.key) if opponent.key else None


def plan_results(store: Path = STORE, plan_log: Path = Path("logs/plans.jsonl"), team: str = "") -> list[dict]:
    """Сводит записи о выбранных планах с исходами матчей нашей команды.

    Запись делается в момент расстановки, матч на платформе начинается на несколько секунд раньше;
    пара ищется по ключу соперника и времени.
    """
    ours = []
    for path in (store / "matches").glob("*.json"):
        match = json.loads(path.read_text(encoding="utf-8"))
        for side in match["sides"]:
            if side.get("name") == team:
                opponent = next(s for s in match["sides"] if s is not side)
                outcome = "draw" if match.get("winner") is None else "win" if match["winner"] == side["seat"] else "loss"
                ours.append({"start": datetime.fromisoformat(match["startedAt"]), "opponent": _key(opponent),
                             "opponentName": opponent.get("botName") or opponent.get("builtInBot"),
                             "bot": side.get("botName"), "outcome": outcome, "endReason": match.get("endReason"), "used": False})
    results = []
    if not plan_log.exists():
        return results
    for line in plan_log.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        at = datetime.fromisoformat(record["at"])
        candidates = [m for m in ours if not m["used"] and m["opponent"] == record["opponent"]
                      and -5 <= (at - m["start"]).total_seconds() <= 30]
        match = min(candidates, key=lambda m: abs((at - m["start"]).total_seconds()), default=None)
        if match:
            match["used"] = True
        results.append(record | {"match": match})
    return results


def plans_table(results: list[dict]) -> str:
    names = {"random_placement": "случайная", "edge_placement": "по краям", "interior_placement": "внутри", None: "нет («Спидран»)"}
    groups: dict[tuple, Counter] = {}
    for record in results:
        bias = record["edgeBias"]
        label = (names.get(record["placement"], record["placement"]), f"{bias['edge']:g}/{bias['corner']:g}" if bias else "нет")
        outcome = record["match"]["outcome"] if record["match"] else "unknown"
        groups.setdefault(label, Counter())[outcome] += 1
    lines = ["расстановка        поправка | партий побед поражений ничьих  исход неизвестен | побед%"]
    for (placement, bias), c in sorted(groups.items()):
        known = c["win"] + c["loss"] + c["draw"]
        rate = f"{100 * c['win'] / known:5.0f}" if known else "    —"
        lines.append(f"{placement:18} {bias:>8} | {sum(c.values()):6d} {c['win']:5d} {c['loss']:9d} {c['draw']:6d} {c['unknown']:17d} | {rate}")
    return "\n".join(lines)


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


def watch(args) -> None:
    """Автообновление: один вход на всё время работы, новый — только если сессия истекла."""
    from .__main__ import load_env

    load_env(args.env_file)
    login, password = os.environ.get("BATTLESHIPS_LOGIN"), os.environ.get("BATTLESHIPS_PASSWORD")
    if not login or not password:
        raise SystemExit("Нужны BATTLESHIPS_LOGIN и BATTLESHIPS_PASSWORD в окружении или в .env")
    platform = Platform()
    platform.login(login, password)
    while True:
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            new = sync(platform, STORE, args.max_new, args.full)
            if new or not PROFILES.exists():
                profiles = build_profiles(STORE, args.team)
                save_profiles(profiles)
                print(f"{stamp} новых матчей {new}, ботов {len(profiles)}", flush=True)
            else:
                print(f"{stamp} новых матчей нет", flush=True)
        except RuntimeError as error:
            print(f"{stamp} сбой: {error}", flush=True)
            if "HTTP 401" in str(error):
                try:
                    platform.login(login, password)
                except RuntimeError as relogin:
                    print(f"{stamp} вход не удался: {relogin}", flush=True)
        time.sleep(args.watch)


def main() -> None:
    from .__main__ import load_env

    parser = argparse.ArgumentParser(description="Разведка ботов по реплеям платформы")
    parser.add_argument("--offline", action="store_true", help="не ходить на платформу, пересобрать из скачанного")
    parser.add_argument("--max-new", type=int, default=400, help="сколько новых матчей скачать за запуск")
    parser.add_argument("--full", action="store_true", help="пройти весь список матчей, а не только новые сверху")
    parser.add_argument("--bot", help="показать карточку бота: часть имени бота или команды")
    parser.add_argument("--plans", action="store_true", help="показать, какие планы выбирал наш бот и чем кончились партии")
    parser.add_argument("--team", default="Нейро Мугивары", help="наша команда — для счёта личных встреч")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--watch", type=int, metavar="СЕКУНД",
                        help="не выходить: докачивать матчи и пересобирать профили с этим интервалом")
    args = parser.parse_args()

    if args.watch:
        watch(args)
        return
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

    total = len(list((STORE / "matches").glob("*.json")))
    if total:
        profiles = build_profiles(STORE, args.team)
        path = save_profiles(profiles)
        print(f"Матчей на диске: {total}, ботов: {len(profiles)}, профили: {path} — закоммитьте файл, чтобы поделиться")
    else:  # реплеев нет — показываем профили из репозитория и не затираем их
        profiles = load_profiles()
        print(f"Реплеев на диске нет, показаны профили из {PROFILES}: ботов {len(profiles)}")
    if args.plans:
        results = plan_results(STORE, team=args.team)
        print(plans_table(results))
        for record in results[-15:]:
            match = record["match"] or {}
            print(f"  {record['at'][11:19]} {match.get('opponentName') or record['opponent']}: {record['placement']}, "
                  f"{match.get('outcome', 'исход неизвестен')} — {record['reason']}")
    elif args.bot:
        needle = args.bot.lower()
        for profile in profiles.values():
            if needle in f"{profile['botName']} {profile['team']}".lower():
                print(card(profile))
    else:
        print(table(profiles))


if __name__ == "__main__":
    main()
