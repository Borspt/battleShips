"""Haki-only opponent dossiers. Only complete, finished, public fleets are learned.

Keys include bot ID and modifier; training fleets are excluded. Never infer empty
cells from an incomplete losing game. No network work happens inside a timed turn.
"""
from dataclasses import replace
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import time

from .adaptation import MEDIUM_BIAS
from .probability import EdgeBias, HARD_BIAS

MEMORY = Path('scouting/haki/matches.json')


def archive_digest(records):
    # Stable across Git's CRLF/LF conversion on Windows/Linux.
    canonical = json.dumps(records, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def recent_records(records, bot_id, modifier, now):
    result = {}
    for item in records:
        if (item.get('botId') != bot_id or item.get('modifier') != modifier
                or item.get('kind') != 'RANKING' or not item.get('complete')):
            continue
        try:
            at = datetime.fromisoformat(item['at'].replace('Z', '+00:00'))
            age = (now - at).total_seconds()
            if not 0 <= age <= 7200:
                continue
            if sorted(map(len, item['ships'])) != [1, 1, 1, 1, 2, 2, 2, 3, 3, 4]:
                continue
            result[item['matchId']] = item
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(result.values(), key=lambda r: r['at'])[-20:]


def adapt_recent(base, records):
    if len(records) < 8:
        return base
    border = lambda cell: cell[0] in (0, 9) or cell[1] in (0, 9)
    shares = [sum(border(c) for s in r['ships'] if len(s) > 1 for c in s) / 16
              for r in records]
    half = len(shares) // 2
    drift = abs(sum(shares[:half]) / half - sum(shares[half:]) / (len(shares)-half))
    share = sum(shares) / len(shares)
    bias = (HARD_BIAS if share >= .9 else MEDIUM_BIAS if share >= .55 else EdgeBias())
    if drift > .3:
        bias = EdgeBias()  # Recent behaviour changed: do not commit to an edge hunt.
    singles = sum(border(s[0]) for r in records for s in r['ships'] if len(s) == 1)
    # Shrink the border estimate toward neutral; never ban an unseen location.
    p = (singles + 12 * .36) / (4 * len(records) + 12)
    relative = (p / (1-p)) / (.36 / .64)
    bias = replace(bias, single_edge=max(.75, min(2., relative)))
    wins = sum(r.get('outcome') == 'WIN' for r in records)
    reason = (f'Хаки: ID + текущий режим, {len(records)} свежих полных реплеев; '
              f'побед соперника {wins}; длинные у края {share:.0%}; '
              f'вес одиночек у края {bias.single_edge:.2f}; смена расстановки={drift > .3}')
    return replace(base, edge_bias=bias, reason=reason)


class HakiBook:
    def __init__(self, path=MEMORY):
        self.path = Path(path)
        self.records = []
        self.policies = {}
        self.digest = None
        self.checked = -float('inf')

    def adapt(self, base, opponent, modifier):
        if getattr(opponent, 'built_in_bot', None) or modifier in ('SPEEDRUN',):
            return base
        if time.monotonic() - self.checked >= 10:
            self.checked = time.monotonic()
            try:
                raw = self.path.read_bytes()
                data = json.loads(raw)
                if isinstance(data, list):
                    self.records = data
                    self.digest = archive_digest(data)
                policy = json.loads(self.path.with_name('policies.json').read_text(encoding='utf-8'))
                self.policies = policy.get('policies', {}) if policy.get('dataHash') == self.digest else {}
            except (OSError, ValueError):
                self.policies = {}
        records = recent_records(self.records, getattr(opponent, 'bot_id', None),
                                 modifier, datetime.now(timezone.utc))
        key = str(getattr(opponent, 'bot_id', None)) + ':' + str(modifier)
        policy = self.policies.get(key) or {}
        # A plausible story about a win is not evidence of a better policy.
        # Enable only after chronological holdout evaluation, never in-game tuning.
        if policy.get('approved') is True and len(records) >= 8:
            return adapt_recent(base, records)
        if records:
            return replace(base, reason=base.reason +
                           f'; Хаки: {len(records)} свежих реплеев по ID/режиму, '
                           'новая поправка не прошла проверку; сохранена базовая стрельба')
        return base
