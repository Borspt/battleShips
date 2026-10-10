"""Local Haki series supervisor. Never restarts a completed child."""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .haki_session import HAKI_STRATEGY


def now():
    return datetime.now(timezone.utc).isoformat()


def hidden_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    # Windows can briefly deny replacement while the HTTP reader has the file
    # open. Preserve the previous complete snapshot and retry the atomic swap.
    for attempt in range(10):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(.02 * (attempt + 1))


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def haki_processes():
    """Return only identifiers, never command lines (which may hold credentials)."""
    if os.name != 'nt':
        # The panel is intentionally Windows-only until cross-platform discovery exists.
        raise RuntimeError('Process discovery requires Windows')
    script = "$ErrorActionPreference='Stop'; @(Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*battleships.strategies.haki:HakiObservationBot*' } | Select-Object ProcessId,ParentProcessId) | ConvertTo-Json -Compress"
    result = subprocess.run(['powershell', '-NoProfile', '-Command', script],
        capture_output=True, text=True, timeout=12, check=True, **hidden_options())
    data = json.loads(result.stdout.strip() or '[]')
    return data if isinstance(data, list) else [data]


def journal_summary(directory):
    results, seen = [], set()
    summary = {'results': results, 'losses': 0, 'wins': 0, 'draws': 0,
               'active': False, 'opponent': None, 'stopReason': None,
               'timeouts': 0, 'rejections': 0, 'bonuses': 0, 'resyncEvents': 0,
               'modifier': None, 'lastActionMs': None}
    for path in sorted(Path(directory).glob('session-*.jsonl')):
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue  # A final line can be in flight.
            data = row.get('data') or {}
            event = row.get('event')
            if event == 'received':
                kind = data.get('type')
                if kind in {'MATCH_STARTED', 'MATCH_SNAPSHOT'}:
                    opponent = data.get('opponent') or {}
                    summary.update(active=True, modifier=(data.get('rules') or {}).get('modifier'),
                        shotLimit=(data.get('rules') or {}).get('shotLimitPerPlayer'),
                        opponent={'bot_id': opponent.get('botId'), 'name': opponent.get('botName')})
                summary['timeouts'] += kind == 'TURN_TIMED_OUT'
                summary['rejections'] += kind in {'SHOT_REJECTED', 'BONUS_REJECTED', 'COMMAND_REJECTED'}
                summary['bonuses'] += kind == 'BONUS_RESOLVED'
            if event == 'haki_action_timing':
                summary['lastActionMs'] = data.get('request_to_send_ms')
            if event == 'haki_rating_checkpoint':
                summary.update({k: data[k] for k in ('ratingBefore', 'ratingCurrent', 'ratingGain',
                    'ratingMatchCount', 'completed', 'warmupMatches', 'ratingDecision')})
                summary['ratingCheckedAt'] = row.get('at')
            if event == 'haki_turn_deferred':
                summary['resyncEvents'] += 1
            if event in {'match_context', 'opponent_context'}:
                opponent = dict(summary.get('opponent') or {})
                opponent.update({k: data[k] for k in ('bot_id', 'name', 'rating', 'kind') if data.get(k) is not None})
                summary.update(active=True, opponent=opponent)
            if event == 'match_summary' and data.get('match_key') not in seen:
                seen.add(data.get('match_key'))
                outcome = data.get('outcome')
                results.append({'at': row.get('at'), 'outcome': outcome,
                                'opponent': {**(summary.get('opponent') or {}), **(data.get('opponent') or {})},
                                'modifier': data.get('modifier'),
                                'shots': len(data.get('shots') or []), 'endReason': data.get('end_reason')})
                summary['active'] = False
                counter = {'LOSS': 'losses', 'WIN': 'wins', 'DRAW': 'draws'}.get(outcome)
                if counter:
                    summary[counter] += 1
            if event in {'stop_on_loss', 'haki_session_stopped'}:
                summary['stopReason'] = data.get('reason') or ('LOSS_LIMIT' if summary['losses'] >= 2 else 'STOP_ON_LOSS')
    return summary


def bot_command(repo, logs):
    return [sys.executable, '-u', '-m', 'battleships', '--env-file', str(repo / '.env.muzrob'),
            '--token-env', 'BATTLESHIPS_TOKEN_MUZROB', '--strategy', HAKI_STRATEGY,
            '--mode', 'RANKING', '--log-dir', str(logs), '--allow-bonuses']


def supervise(repo, series):
    repo, series = Path(repo).resolve(), Path(series).resolve()
    state_path = series / 'state.json'
    state = read_json(state_path)
    state.update(supervisorPid=os.getpid(), status='starting', lossLimit=None,
                 stopPolicy='RATING_GAIN', warmupMatches=3, mode='RANKING')
    write_json(state_path, state)
    child = None
    try:
        if (series / 'stop.request').exists() or (series / 'emergency.request').exists():
            state.update(status='stopped', stopReason='CANCELLED_BEFORE_START', stoppedAt=now())
            return
        if haki_processes():
            raise RuntimeError('Another Haki process is running')
        env = os.environ.copy()
        env.update(HAKI_STOP_POLICY='RATING_GAIN', HAKI_RATING_WARMUP='3',
                   HAKI_STOP_FILE=str(series / 'stop.request'))
        with (series / 'client.log').open('ab') as output:
            child = subprocess.Popen(bot_command(repo, series / 'logs'), cwd=repo, env=env,
                                     stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                                     **hidden_options())
            state.update(pid=child.pid, status='running', startedAt=now())
            write_json(state_path, state)
            write_json(repo / 'runtime' / 'muzrob' / 'process.json', {
                'pid': child.pid, 'supervisorPid': os.getpid(),
                'commit': state.get('commit'), 'workingDirectory': str(repo),
                'startedAt': state['startedAt'], 'startingMode': 'RANKING',
                'lossLimit': None, 'stopPolicy': 'RATING_GAIN', 'warmupMatches': 3, 'status': 'running',
                'logDirectory': str(series / 'logs'), 'panelSeries': str(series)})
            while child.poll() is None:
                state.update(journal_summary(series / 'logs'))
                state['updatedAt'] = now()
                if (series / 'emergency.request').exists():
                    # Only the still-live child owned by this supervisor is terminated.
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                                       capture_output=True, timeout=10, **hidden_options())
                    else:
                        child.terminate()
                    state['stopReason'] = 'EMERGENCY_STOP'
                    child.wait(timeout=15)
                    break
                if (series / 'stop.request').exists():
                    state['status'] = 'stopping_after_match'
                write_json(state_path, state)
                time.sleep(.5)
            reason = state.get('stopReason')
            state.update(journal_summary(series / 'logs'))
            state.update(status='stopped', exitCode=child.returncode, stoppedAt=now())
            state['stopReason'] = reason or state.get('stopReason') or 'PROCESS_EXIT'
    except Exception as exc:
        # No exception text: OS/network errors can include sensitive arguments.
        if child is not None and child.poll() is None:
            # Preserve supervision even when parsing/writing a journal failed.
            (series / 'stop.request').touch()
            state.update(status='error_running', error=type(exc).__name__)
            write_json(state_path, state)
            while child.poll() is None:
                if (series / 'emergency.request').exists():
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                                       capture_output=True, timeout=10, **hidden_options())
                    else:
                        child.terminate()
                    child.wait(timeout=15)
                time.sleep(.5)
            # The child may have finished a match while graceful stop was pending.
            # Recover that final result even when the original failure was a write.
            state.update(journal_summary(series / 'logs'))
            state.update(status='stopped', stopReason='SUPERVISOR_ERROR',
                         exitCode=child.returncode, stoppedAt=now())
        else:
            state.update(status='error', error=type(exc).__name__, stoppedAt=now())
    finally:
        write_json(state_path, state)
        metadata_path = repo / 'runtime' / 'muzrob' / 'process.json'
        metadata = read_json(metadata_path)
        if child is not None and metadata.get('pid') == child.pid:
            metadata.update({key: state.get(key) for key in (
                'status', 'stoppedAt', 'stopReason', 'losses', 'exitCode')})
            write_json(metadata_path, metadata)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True)
    parser.add_argument('--series', required=True)
    args = parser.parse_args()
    supervise(args.repo, args.series)


if __name__ == '__main__':
    main()
