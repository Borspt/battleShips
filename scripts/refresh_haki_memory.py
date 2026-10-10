"""Build Haki-only dossiers from finished replays, without changing shared profiles.

Offline: python scripts/refresh_haki_memory.py
Download first: python scripts/refresh_haki_memory.py --sync --env-file <credentials>
The sync mode uses the existing participant login, never a bot token.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from battleships.placement import validate_placement
from battleships.rules import Rules
from battleships.scouting import Platform, sync


def records_json(records):
    return '[\n' + ',\n'.join(json.dumps(r,ensure_ascii=False) for r in records) + '\n]\n'


def ships_from_board(board):
    cells = {(c['row'], c['column']) for c in board
             if c['status'] in ('корабль', 'потоплен', 'попадание')}
    ships = []
    while cells:
        todo, ship = [cells.pop()], []
        while todo:
            r, c = todo.pop()
            ship.append((r, c))
            for n in ((r-1,c), (r+1,c), (r,c-1), (r,c+1)):
                if n in cells:
                    cells.remove(n)
                    todo.append(n)
        ships.append(sorted(ship))
    if validate_placement(Rules(), ships):
        raise ValueError('Replay does not expose a complete legal fleet')
    return sorted(ships)


def import_ui(data, profiles):
    if 'Шаг ' not in data['text']:
        return None
    steps = re.search(r'Шаг (\d+) из (\d+)', data['text'])
    if not steps or steps[1] != steps[2]:
        return None
    label = profiles['bot:' + data['botId']]['botName']
    regions = re.findall(r'^  - region "([^"\n]+)":\n    - generic "([^"\n]+)"',
                         data['snapshot'], re.M)
    for (team, full_label), board in zip(regions, data['boards']):
        if full_label.split(' · ')[-1] != label:
            continue
        attributable = sum(t == team for t, _ in regions) == 1
        events = (re.findall(r'- generic "' + re.escape(team) + r'"\n        - generic: (.+)',
                            data['snapshot']) if attributable else [])
        shots = [re.search(r'выстрел ([A-J])(10|[1-9]) — (.+)', e) for e in events]
        shots = [((int(m[2])-1, ord(m[1])-65), m[3]) for m in shots if m]
        return {**{k:data[k] for k in ('botId','matchId','at','modifier','outcome')},
                'botName':label, 'kind':'RANKING', 'complete':True,
                'eventAttribution':attributable,
                'ships':ships_from_board(board), 'shots':shots,
                'bonuses':[e.strip('"') for e in events if 'расстояние,' in e
                           or 'мина' in e or 'сонар' in e or 'щит' in e],
                'source':'https://hackathon.no-offence.ru/matches/' + data['matchId']}


def import_api(data):
    if data.get('status') != 'FINISHED' or data.get('kind') == 'TRAINING':
        return []
    sides = data.get('sides') or []
    if len(sides) != 2 or any(s.get('kind') != 'REMOTE_BOT' for s in sides):
        return []  # Both are remote bots: no inference from K (which can be 10 in ranked).
    events = data.get('events') or []
    rules = next((e.get('rules') for e in events if e.get('type')=='MATCH_STARTED'), None)
    if not rules or rules.get('rows') != 10 or rules.get('columns') != 10:
        return []
    result = []
    for side in sides:
        seat = side['seat']
        placed = next((e['ships'] for e in events if e.get('type')=='PLACEMENT_ACCEPTED'
                       and e.get('seat')==seat), None)
        if not placed:
            continue
        ships = [[(c['row'],c['column']) for c in s['cells']] for s in placed]
        if validate_placement(Rules(), ships):
            continue
        result.append({'matchId':data['matchId'],'botId':side['botId'],
                       'botName':side.get('botName'), 'at':data['startedAt'],
                       'modifier':rules.get('modifier'), 'kind':'RANKING','complete':True,
                       'ships':ships, 'outcome':'DRAW' if data.get('winner') is None else
                       'WIN' if data['winner']==seat else 'LOSS',
                       'shots':[e for e in events if e.get('type')=='SHOT' and e.get('seat')==seat],
                       'bonuses':[e for e in events if 'BONUS' in e.get('type','') and e.get('seat')==seat],
                       'source':'https://hackathon.no-offence.ru/matches/' + data['matchId']})
    return result


def main():
    from battleships.__main__ import load_env
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sync', action='store_true')
    parser.add_argument('--env-file', default='.env')
    args = parser.parse_args()
    store = ROOT / 'runtime/muzrob/scouting'
    if args.sync:
        load_env(args.env_file)
        login, password = os.getenv('BATTLESHIPS_LOGIN'), os.getenv('BATTLESHIPS_PASSWORD')
        if not login or not password:
            raise SystemExit('Participant login/password required for replay sync; bot token is not used.')
        platform = Platform()
        platform.login(login, password)
        try:
            sync(platform, store)
        finally:
            platform.logout()
    output = ROOT / 'scouting/haki/matches.json'
    records = json.loads(output.read_text(encoding='utf-8')) if output.exists() else []
    by_key = {(r['botId'],r['matchId']):r for r in records}
    profiles = json.loads((ROOT/'scouting/profiles.json').read_text(encoding='utf-8'))
    for path in (ROOT/'runtime/muzrob/scout-ui').glob('*.json'):
        r = import_ui(json.loads(path.read_text(encoding='utf-8')), profiles)
        if r:
            by_key[r['botId'],r['matchId']] = r
    for folder in (store/'matches', ROOT/'runtime/scouting/matches'):
        for path in folder.glob('*.json'):
            for r in import_api(json.loads(path.read_text(encoding='utf-8'))):
                by_key[r['botId'],r['matchId']] = r
    records = sorted(by_key.values(), key=lambda r:(r['at'],r['botId']))
    output.parent.mkdir(parents=True,exist_ok=True)
    temporary = output.with_suffix('.tmp')
    temporary.write_text(records_json(records),encoding='utf-8')
    temporary.replace(output)
    # Our own encounters are useful even when a loss exposes only part of the
    # enemy fleet. Save them separately; never feed partial fleets to learning.
    encounters = {}
    for path in (ROOT/'logs/muzrob').glob('session-*.jsonl'):
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            d = entry.get('data') or {}
            if entry.get('event') != 'match_summary':
                continue
            opponent = d.get('opponent') or {}
            if not opponent.get('bot_id'):
                continue
            shots, incoming = d.get('shots', []), d.get('opponent_shots', [])
            encounters[d['match_key']] = {
                'matchKey':d['match_key'], 'at':entry['at'],
                'opponentId':opponent['bot_id'], 'opponentRating':opponent.get('rating'),
                'modifier':d.get('modifier'), 'outcome':d.get('outcome'),
                'endReason':d.get('end_reason'), 'shots':len(shots),
                'hits':sum(s['result'] in ('HIT','SUNK') for s in shots),
                'sunk':sum(s['result']=='SUNK' for s in shots),
                'opponentShots':len(incoming),
                'opponentOpening':incoming[:15]}
    history = output.with_name('encounters.json')
    previous = json.loads(history.read_text(encoding='utf-8')) if history.exists() else []
    encounters = {**{r['matchKey']:r for r in previous}, **encounters}
    temporary = history.with_suffix('.tmp')
    temporary.write_text(records_json(sorted(encounters.values(),key=lambda r:r['at'])),encoding='utf-8')
    temporary.replace(history)
    print(json.dumps({'records':len(records),'bots':dict(Counter(r['botName'] for r in records))},ensure_ascii=True))


if __name__ == '__main__':
    main()
