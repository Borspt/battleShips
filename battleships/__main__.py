import argparse
import asyncio
import logging
import os
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlparse

from .client import run_bot


def load_env(path: str):
    """Minimal KEY=VALUE loader; environment variables take precedence. No interpolation."""
    file = Path(path)
    if file.is_file():
        for line in file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def main():
    parser = argparse.ArgumentParser(description="Нейро Мугивары: клиент турнирного бота")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--token-env", default="BATTLESHIPS_TOKEN")
    parser.add_argument("--url", help="WebSocket URL; defaults to BATTLESHIPS_URL or tournament")
    parser.add_argument("--mode", choices=["TRAINING", "RANKING"], default="TRAINING")
    parser.add_argument("--opponent", choices=["EASY", "MEDIUM", "HARD"], default="EASY")
    parser.add_argument("--strategy", default="battleships.strategy:BaselineStrategy")
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument("--opponent-ratings-file", help="Optional fresh Elo observations for built-in bots (JSON)")
    parser.add_argument("--budget-ms", type=int, default=150)
    parser.add_argument("--reserve-ms", type=int, default=250)
    parser.add_argument("--allow-bonuses", action="store_true", help="Allow strategy to spend rating on bonuses")
    parser.add_argument("--stop-after", type=int, default=0, help="Stop after N matches; 0 = continuous")
    parser.add_argument("--stop-on-loss", action="store_true", help="Disconnect after the first LOSS; do not reconnect")
    args = parser.parse_args()
    load_env(args.env_file)
    token = os.environ.get(args.token_env, "").strip()
    if not token or token == "replace-with-your-bot-token":
        parser.error(f"Set {args.token_env} in environment or {args.env_file}; don't put it in the command line")
    if args.budget_ms <= 0 or args.reserve_ms < 100 or args.stop_after < 0:
        parser.error("budget-ms must be positive, reserve-ms >= 100, stop-after >= 0")
    url = args.url or os.environ.get("BATTLESHIPS_URL", "wss://hackathon.no-offence.ru/api/v1/bots/connection")
    target = urlparse(url)
    if target.scheme != "wss" and not (target.scheme == "ws" and target.hostname in {"127.0.0.1", "localhost", "::1"}):
        parser.error("Use wss://, or ws:// only for a local test server")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        from .haki_session import HAKI_STRATEGY
        from .haki_lock import haki_process_lock
        with haki_process_lock() if args.strategy == HAKI_STRATEGY else nullcontext():
            asyncio.run(run_bot(url=url, token=token, strategy=args.strategy, mode=args.mode,
                            opponent=args.opponent, log_dir=args.log_dir, budget_ms=args.budget_ms,
                            reserve_ms=args.reserve_ms, allow_bonuses=args.allow_bonuses,
                            stop_after=args.stop_after, opponent_ratings_file=args.opponent_ratings_file,
                            stop_on_loss=args.stop_on_loss))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Network/library exceptions may include request headers. Never print those.
        logging.error("Client stopped (%s). Check configuration and local journal.", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
