"""127.0.0.1-only control panel for the personal Haki bot, no external services."""
import argparse
import json
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from .haki_supervisor import hidden_options, haki_processes, now, read_json, write_json
from .haki_lock import haki_process_lock


PAGE = r'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Хаки · управление</title>
<style>*{box-sizing:border-box}body{font:16px system-ui;background:#0b1524;color:#e8f0fa;max-width:1120px;margin:30px auto;padding:24px}h1{font-size:34px;margin:8px 0}p,small{color:#a7bbcf}.eyebrow{color:#66d6b9;letter-spacing:3px;font-size:12px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:25px 0}.card,section,details{background:#152338;border:1px solid #29405b;border-radius:14px;padding:20px}.value{font-size:29px;margin-top:10px}.controls{display:flex;gap:10px;flex-wrap:wrap}button{padding:13px 18px;background:#65d4b5;color:#102923;border:0;border-radius:9px;font-size:15px;font-weight:600;cursor:pointer}button.secondary{background:#263d58;color:#e8f0fa}button.danger{background:transparent;color:#f6a8a8;border:1px solid #69434b}button:disabled{opacity:.4;cursor:default}section{margin:18px 0}dl{display:grid;grid-template-columns:180px 1fr;gap:10px;margin:0}dt{color:#9db2c9}dd{margin:0;overflow-wrap:anywhere}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px 8px;border-bottom:1px solid #29405b}th{color:#9db2c9;font-size:13px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}#message{min-height:24px;color:#e8c583}.win{color:#65d4b5}.loss{color:#f3a0a5}summary{cursor:pointer}footer{margin:20px 0;line-height:1.6}@media(max-width:680px){.cards{grid-template-columns:1fr}body{padding:15px}dl{grid-template-columns:1fr}table{font-size:13px}}
</style><div class="eyebrow">НЕЙРО МУГИВАРЫ / ПУЛЬТ БОТА</div><h1>Хаки Наблюдения</h1><p>Первые 3 матча без лимита поражений · затем играем, пока рейтинг выше стартового</p>
<div class="cards"><div class="card">Состояние<div class="value" id="state">Проверяем…</div></div><div class="card">Рейтинг<div class="value" id="rating">—</div><small id="ratingTime"></small></div><div class="card">Результат серии<div class="value" id="score">0 побед / 0 поражений</div><small id="delta">Остановка при приросте ≤ 0 после 3 матчей</small></div></div>
<div class="controls"><button id="start" onclick="act('start')" disabled>Начать новую серию</button><button id="stop" class="secondary" onclick="act('stop')" disabled>Остановить после матча</button><button id="emergency" class="danger" onclick="if(confirm('Завершить процесс Хаки немедленно? Текущий матч может быть проигран.'))act('emergency')" disabled>Аварийная остановка</button></div><p id="message" role="status"></p>
<section><dl><dt>Режим партии</dt><dd id="mode">—</dd><dt>Соперник</dt><dd id="opponent">—</dd><dt>Версия в игре</dt><dd id="version">—</dd><dt>Процесс</dt><dd id="pid">—</dd><dt>Причина остановки</dt><dd id="reason">—</dd><dt>Рейтинг на проверке</dt><dd id="ratingCheck">—</dd><dt>Контроль соединения</dt><dd id="network">—</dd></dl></section>
<section><h2>Матчи этой серии</h2><table><thead><tr><th>Результат</th><th>Соперник</th><th>Выстрелы</th><th>Время</th></tr></thead><tbody id="matches"></tbody></table><p id="empty">Матчей пока нет</p></section>
<details><summary>Технические подробности</summary><pre id="details"></pre></details><footer><small>Страница доступна только на этом компьютере. Закрытие вкладки не останавливает бота.<br>После завершения серии автоматического перезапуска нет. Панель управляет только Хаки.</small></footer>
<script>let nonce='__NONCE__';let busy=false,refreshing=false;const $=id=>document.getElementById(id),text=(id,v)=>$(id).textContent=v;const time=v=>v?new Date(v).toLocaleTimeString('ru-RU'):'—';
async function refresh(){if(refreshing)return;refreshing=true;try{let r=await fetch('/api/status');if(!r.ok)throw Error();let s=await r.json(),x=s.series;let active=['starting','running','stopping_after_match','error_running'].includes(x.status),external=s.hakiProcesses.length&&!active;let names={starting:'Запускается',running:x.active?'Играет':'Ждёт матч',stopping_after_match:'Завершает матч',stopped:'Остановлен',error:'Ошибка',error_running:'Ошибка контроля'};text('state',external?'Другой запуск':names[x.status]||'Готов к запуску');text('rating',s.rating==null?'Недоступен':s.rating.toFixed(2));text('ratingTime','Проверено: '+time(s.ratingObservedAt));text('score',(x.wins||0)+' побед / '+(x.losses||0)+' поражений');text('delta',s.ratingDelta==null?'Остановка при приросте ≤ 0 после 3 матчей':'Изменение рейтинга: '+(s.ratingDelta>=0?'+':'')+s.ratingDelta.toFixed(2));text('mode',({HUNGER_GAMES:'Голодные игры',SCHRODINGER_SHIP:'Корабль Шрёдингера',THREE_MUSKETEERS:'Три мушкетёра',DUEL:'Дуэль'}[x.modifier]||x.modifier||'Ожидание партии')+' · рейтинг'+(x.shotLimit?' · '+x.shotLimit+' выстрелов':''));text('opponent',x.opponent?.name||x.opponent?.bot_id||'—');text('version',x.commit?x.commit.slice(0,8)+(x.dirtyAtStart?' + локальные изменения':''):'—');text('pid',x.pid||'—');text('reason',({RATING_GAIN_EXHAUSTED:'Прирост рейтинга исчерпан',RATING_UNAVAILABLE:'Не удалось подтвердить свежий рейтинг',SUPERVISOR_ERROR:'Ошибка контроля; серия завершена',LOSS_LIMIT:'Достигнут лимит двух поражений',OPERATOR_STOP:'Остановлен по команде',UNRESOLVED_ACTION:'Не получен однозначный ответ на действие',AMBIGUOUS_REJECTION:'Не удалось сопоставить отказ',EMERGENCY_STOP:'Аварийная остановка',PROCESS_EXIT:'Процесс завершился'}[x.stopReason]||x.stopReason||'—'));text('ratingCheck',x.ratingCurrent==null?'Ожидание проверки':x.ratingBefore.toFixed(2)+' → '+x.ratingCurrent.toFixed(2)+' · '+time(x.ratingCheckedAt)+' · '+({WARMUP:'первые 3 матча',CONTINUE:'играем дальше',RATING_GAIN_EXHAUSTED:'остановка'}[x.ratingDecision]||x.ratingDecision));text('network','Таймауты: '+(x.timeouts||0)+' · отказы: '+(x.rejections||0)+' · ожидания старого ответа: '+(x.resyncEvents||0)+(x.lastActionMs!=null?' · последний ответ: '+x.lastActionMs+' мс':''));$('start').disabled=busy||active||!!s.hakiProcesses.length||!!s.processCheckError;$('stop').disabled=busy||!active;$('emergency').disabled=busy||!active;if(s.processCheckError)text('message',s.processCheckError);else if(external)text('message','Хаки уже запущен отдельно. Второй экземпляр заблокирован.');$('matches').replaceChildren();for(let m of [...(x.results||[])].reverse()){let tr=document.createElement('tr');for(let v of [m.outcome==='WIN'?'Победа':m.outcome==='LOSS'?'Поражение':'Ничья',m.opponent?.name||m.opponent?.bot_id||'—',m.shots,time(m.at)]){let td=document.createElement('td');td.textContent=v;tr.appendChild(td)}tr.firstChild.className=m.outcome==='WIN'?'win':'loss';$('matches').appendChild(tr)}$('empty').hidden=!!x.results?.length;text('details',JSON.stringify(s,null,2))}catch(e){text('message','Панель недоступна. Это не означает остановку бота.');$('start').disabled=true}finally{refreshing=false}}
async function act(a){if(busy)return;busy=true;try{let r=await fetch('/api/'+a,{method:'POST',headers:{'X-Haki-Nonce':nonce,'Content-Type':'application/json'},body:'{}'});let d=await r.json();text('message',d.message||d.error||'Готово')}catch(e){text('message','Не удалось подтвердить команду. Проверьте состояние.')}finally{busy=false;refresh()}}refresh();setInterval(refresh,3000)</script></html>'''


class Manager:
    def __init__(self, repo):
        self.repo = Path(repo).resolve()
        self.runtime = self.repo / 'runtime' / 'muzrob' / 'panel'
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.rating_lock = threading.Lock()
        self.rating_checked = 0.0
        self.rating = None
        self.rating_observed = None
        self.process_lock = threading.Lock()
        self.process_checked = 0.0
        self.process_cache = []
        self.process_error = None
        self.last_series_status = None

    def processes(self):
        with self.process_lock:
            if time.monotonic() - self.process_checked >= 3:
                try:
                    self.process_cache = haki_processes()
                    self.process_error = None
                except Exception:
                    self.process_cache = []
                    self.process_error = 'Не удалось проверить процессы; запуск заблокирован'
                self.process_checked = time.monotonic()
            return self.process_cache, self.process_error

    def refresh_rating(self):
        with self.rating_lock:
            if time.monotonic() - self.rating_checked < 30:
                return
            self.rating_checked = time.monotonic()
            try:
                url = 'https://hackathon.no-offence.ru/api/v1/bots/6603c7b8-f81d-4309-b4cf-28e3f9a6c670/statistics?kind=ALL'
                with urllib.request.urlopen(url, timeout=2) as response:
                    data = json.load(response)
                value = data.get('value', data)
                rating = value.get('rating')
                if type(rating) in (int, float):
                    self.rating, self.rating_observed = rating, now()
            except Exception:
                pass  # Preserve dated evidence, never invent a rating.

    def current(self):
        current = read_json(self.runtime / 'current.json')
        series_id = current.get('seriesId', '')
        if not series_id or any(c not in '0123456789abcdef' for c in series_id):
            return None, {}
        path = self.runtime / series_id
        return path, read_json(path / 'state.json')

    def status(self):
        _, state = self.current()
        signature = (state.get('seriesId'), state.get('status'))
        if signature != self.last_series_status:
            self.rating_checked = 0.0
            self.last_series_status = signature
        self.refresh_rating()
        processes, discovery_error = self.processes()
        return {'bot': 'Хаки Наблюдения', 'series': state, 'hakiProcesses': processes,
                'processCheckError': discovery_error, 'automaticRestart': False,
                'rating': self.rating, 'ratingObservedAt': self.rating_observed,
                'ratingDelta': self.rating - state['ratingBefore']
                    if self.rating is not None and state.get('ratingBefore') is not None else None}

    def action(self, action):
        with self.lock:
            series, state = self.current()
            if action == 'start':
                if haki_processes():
                    raise RuntimeError('Хаки уже работает, включая запуск из другой папки')
                # Fail closed on unresolved ownership; stale state needs operator inspection.
                if state.get('status') in {'starting', 'running', 'stopping_after_match', 'error_running'}:
                    raise RuntimeError('Предыдущий запуск ещё не завершён или требует проверки')
                if not (self.repo / '.env.muzrob').is_file():
                    raise RuntimeError('Отсутствует локальный файл .env.muzrob')
                commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.repo,
                    capture_output=True, text=True, check=True, **hidden_options()).stdout.strip()
                dirty = bool(subprocess.run(['git', 'status', '--porcelain'], cwd=self.repo,
                    capture_output=True, text=True, check=True, **hidden_options()).stdout.strip())
                series_id = uuid4().hex
                series = self.runtime / series_id
                self.refresh_rating()
                state = {'seriesId': series_id, 'status': 'starting', 'createdAt': now(),
                         'commit': commit, 'dirtyAtStart': dirty, 'workingDirectory': str(self.repo),
                         'mode': 'RANKING', 'lossLimit': None, 'losses': 0,
                         'stopPolicy': 'RATING_GAIN', 'warmupMatches': 3,
                         'ratingBefore': self.rating, 'ratingBeforeObservedAt': self.rating_observed}
                write_json(series / 'state.json', state)
                write_json(self.runtime / 'current.json', {'seriesId': series_id})
                try:
                    with (series / 'supervisor.log').open('ab') as output:
                        subprocess.Popen([sys.executable, '-m', 'battleships.haki_supervisor',
                            '--repo', str(self.repo), '--series', str(series)], cwd=self.repo,
                            stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                            **hidden_options())
                except Exception:
                    state.update(status='error', stopReason='SUPERVISOR_START_FAILED')
                    write_json(series / 'state.json', state)
                    raise
                return {'message': 'Новая серия запрошена. После 3 матчей продолжаем только при положительном приросте рейтинга.'}
            if action not in {'stop', 'emergency'}:
                raise ValueError('Неизвестная команда')
            if not series or state.get('status') not in {'starting', 'running', 'stopping_after_match', 'error_running'}:
                raise RuntimeError('Нет запущенной через эту панель серии')
            (series / ('stop.request' if action == 'stop' else 'emergency.request')).touch()
            return {'message': 'Запрошена остановка после матча' if action == 'stop' else 'Запрошена аварийная остановка собственного процесса'}


def make_server(manager, port=8765):
    nonce = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def valid_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def reply(self, status, value, html=False):
            body = value.encode('utf-8') if html else json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, {'error': 'Invalid host'})
            if self.path == '/':
                return self.reply(200, PAGE.replace('__NONCE__', nonce), True)
            if self.path == '/api/status':
                return self.reply(200, manager.status())
            self.reply(404, {'error': 'Not found'})

        def do_POST(self):
            if (not self.valid_host() or self.headers.get('Origin') != f'http://127.0.0.1:{self.server.server_port}'
                    or not secrets.compare_digest(self.headers.get('X-Haki-Nonce', ''), nonce)):
                return self.reply(403, {'error': 'Invalid origin or nonce'})
            if self.path not in {'/api/start', '/api/stop', '/api/emergency'}:
                return self.reply(404, {'error': 'Not found'})
            try:
                self.reply(200, manager.action(self.path.rsplit('/', 1)[1]))
            except RuntimeError as exc:
                self.reply(409, {'error': str(exc)})
            except Exception:
                self.reply(409, {'error': 'Действие не выполнено; проверьте процессы и локальные журналы'})
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description='Local Haki control panel')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    with haki_process_lock(Path.home() / '.battleships' / 'haki-panel.lock'):
        server = make_server(Manager(repo), args.port)
        print(f'http://127.0.0.1:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
