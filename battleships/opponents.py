"""Read-only opponent metadata. Never opens a bot socket or reads opponent boards."""
import asyncio
import json
import math
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

from .model import OpponentInfo


class OpponentDirectory:
    def __init__(self, websocket_url: str, ratings_file: str | None = None):
        url = urlsplit(websocket_url)
        self.base_url = f"{'https' if url.scheme == 'wss' else 'http'}://{url.netloc}"
        self.ratings_file = Path(ratings_file) if ratings_file else None

    async def lookup(self, info: OpponentInfo) -> OpponentInfo:
        return await asyncio.to_thread(self._lookup, info)

    def _lookup(self, info: OpponentInfo) -> OpponentInfo:
        if info.bot_id:
            # Validate server-supplied identity before constructing a URL.
            bot_id = str(UUID(info.bot_id))
            url = f"{self.base_url}/api/v1/bots/{bot_id}/statistics?kind=ALL"
            request = Request(url, headers={"Accept": "application/json"})
            with urlopen(request, timeout=2) as response:
                data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("Statistics response too large")
            result = json.loads(data)
            if result.get("error"):
                raise ValueError("Statistics unavailable")
            rating = result["value"]["rating"]
            return self._rated(info, rating, "bot_statistics",
                               datetime.now(timezone.utc).isoformat())
        if info.built_in_bot and self.ratings_file:
            # Optional input from a separate collector, not a guessed initial rating.
            data = json.loads(self.ratings_file.read_text(encoding="utf-8"))
            item = data.get(info.key)
            if item:
                observed = datetime.fromisoformat(item["observed_at"])
                age = (datetime.now(timezone.utc) - observed).total_seconds()
                if 0 <= age <= 60:
                    return self._rated(info, item["rating"], "ratings_file", observed.isoformat())
        return info

    @staticmethod
    def _rated(info, rating, source, observed_at):
        if type(rating) not in (int, float) or not math.isfinite(rating):
            raise ValueError("Invalid opponent rating")
        return replace(info, rating=float(rating), rating_source=source,
                       rating_observed_at=observed_at)
