#!/usr/bin/env bash
# Запускает AchievementBot на macOS/Linux. Первый запуск сам готовит окружение и спрашивает токен.
# Токен хранится только локально в .env.achiever (файл в .gitignore) и не попадает в командную строку.
# Параметры: --mode TRAINING|RANKING (по умолчанию RANKING), --stop-after N, --new-token
set -euo pipefail
cd "$(dirname "$0")/.."

MODE=RANKING; STOP_AFTER=0; NEW_TOKEN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --stop-after) STOP_AFTER="$2"; shift 2 ;;
    --new-token) NEW_TOKEN=1; shift ;;
    *) echo "Неизвестный параметр: $1" >&2; exit 2 ;;
  esac
done

if [ ! -x .venv/bin/python ]; then
  PY=""
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PY="$candidate"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "Нужен Python 3.10+ (лучше 3.12). Установите с python.org или: brew install python@3.12 — и запустите снова." >&2
    exit 1
  fi
  echo "Создаю окружение Python (.venv) на $($PY --version)..."
  "$PY" -m venv .venv
fi
if ! .venv/bin/python -c 'import websockets' 2>/dev/null; then
  echo "Ставлю зависимости..."
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt \
    || .venv/bin/python -m pip install --disable-pip-version-check -q websockets
fi

if [ "$NEW_TOKEN" = 1 ] || [ ! -f .env.achiever ]; then
  printf 'Вставьте токен бота из Кабинета команды (ввод скрыт) и нажмите Enter: '
  IFS= read -rs TOKEN; echo
  TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
  [ -n "$TOKEN" ] || { echo "Пустой токен." >&2; exit 1; }
  umask 077
  printf 'BATTLESHIPS_TOKEN_ACHIEVER=%s\n' "$TOKEN" > .env.achiever
  unset TOKEN
fi

echo "Запуск AchievementBot, режим $MODE. Остановить: Ctrl+C."
RUN=(.venv/bin/python -u -m battleships --env-file .env.achiever --token-env BATTLESHIPS_TOKEN_ACHIEVER
     --strategy battleships.strategies.achiever:AchievementBot --allow-bonuses
     --mode "$MODE" --log-dir logs/achiever --stop-after "$STOP_AFTER")
# caffeinate не даёт Маку уснуть, пока бот работает.
if command -v caffeinate >/dev/null 2>&1; then exec caffeinate -i "${RUN[@]}"; else exec "${RUN[@]}"; fi
