"""Haki-only series guard based on confirmed platform statistics."""
import asyncio
import json
import math
import time
from urllib.request import Request, urlopen

HAKI_ID = '6603c7b8-f81d-4309-b4cf-28e3f9a6c670'


def rating_snapshot(base_url):
    request = Request(f'{base_url}/api/v1/bots/{HAKI_ID}/statistics?kind=ALL',
                      headers={'Accept': 'application/json', 'Cache-Control': 'no-cache'})
    with urlopen(request, timeout=1.5) as response:
        payload = json.load(response)
    if payload.get('error'):
        raise ValueError('Statistics unavailable')
    data = payload['value']
    rating, count = data['rating'], data['matchCount']
    if type(rating) not in (int, float) or not math.isfinite(rating):
        raise ValueError('Invalid rating')
    if type(count) is not int or count < 0:
        raise ValueError('Invalid match count')
    return {'rating': float(rating), 'matchCount': count}


class HakiRatingGate:
    def __init__(self, base_url, journal, warmup=3, lookup=rating_snapshot):
        if not 1 <= warmup <= 20:
            raise ValueError('Rating warmup must be between 1 and 20')
        self.base_url, self.journal, self.warmup, self.lookup = base_url, journal, warmup, lookup
        self.baseline = None
        self.completed = 0
        self.settle_seconds = 3.0

    async def start(self):
        self.baseline = await asyncio.to_thread(self.lookup, self.base_url)
        self.record(self.baseline, 'WARMUP')

    def record(self, snapshot, decision):
        self.journal.write('haki_rating_checkpoint', {
            'ratingBefore': self.baseline['rating'], 'ratingCurrent': snapshot['rating'],
            'ratingGain': snapshot['rating'] - self.baseline['rating'],
            'ratingMatchCount': snapshot['matchCount'], 'completed': self.completed,
            'warmupMatches': self.warmup, 'ratingDecision': decision})

    async def after_match(self):
        self.completed += 1
        # A response with the previous match count is not a settled rating.
        deadline = time.monotonic() + self.settle_seconds
        while True:
            try:
                snapshot = await asyncio.to_thread(self.lookup, self.base_url)
                if snapshot['matchCount'] >= self.baseline['matchCount'] + self.completed:
                    decision = ('WARMUP' if self.completed < self.warmup else
                                'CONTINUE' if snapshot['rating'] > self.baseline['rating'] else
                                'RATING_GAIN_EXHAUSTED')
                    self.record(snapshot, decision)
                    return decision if decision == 'RATING_GAIN_EXHAUSTED' else None
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if time.monotonic() >= deadline:
                self.journal.write('haki_rating_unavailable', {'completed': self.completed})
                return 'RATING_UNAVAILABLE'
            await asyncio.sleep(.15)
