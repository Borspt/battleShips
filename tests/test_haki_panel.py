import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from battleships.haki_panel import Manager, make_server
from battleships.haki_supervisor import bot_command, journal_summary, supervise, write_json, read_json


class HakiPanelTests(unittest.TestCase):
    def test_windows_reader_lock_does_not_abort_snapshot_update(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'state.json'
            write_json(target, {'status': 'starting'})
            real_replace = Path.replace
            attempts = []
            def locked_once(source, destination):
                attempts.append(1)
                if len(attempts) == 1:
                    self.assertEqual(read_json(target)['status'], 'starting')
                    raise PermissionError('reader holds destination')
                return real_replace(source, destination)
            with patch.object(Path, 'replace', locked_once):
                write_json(target, {'status': 'running'})
            self.assertEqual(read_json(target)['status'], 'running')
            self.assertEqual(len(attempts), 2)

    def test_http_requires_host_origin_and_nonce(self):
        manager = Mock()
        manager.status.return_value = {'series': {}}
        manager.action.return_value = {'message': 'ok'}
        server = make_server(manager, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, path, headers=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            status, body = response.status, response.read().decode()
            connection.close()
            return status, body
        try:
            status, body = request('GET', '/')
            self.assertEqual(status, 200)
            nonce = re.search("let nonce='([^']+)'", body).group(1)
            self.assertEqual(request('POST', '/api/start')[0], 403)
            self.assertEqual(request('GET', '/', {'Host': 'evil.example'})[0], 403)
            headers = {'Origin': f'http://127.0.0.1:{server.server_port}', 'X-Haki-Nonce': nonce}
            self.assertEqual(request('POST', '/api/start', headers)[0], 200)
            manager.action.assert_called_once_with('start')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_external_process_blocks_start_and_cannot_be_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = Manager(directory)
            with patch('battleships.haki_panel.haki_processes', return_value=[{'ProcessId': 44}]):
                with self.assertRaises(RuntimeError):
                    manager.action('start')
            with self.assertRaises(RuntimeError):
                manager.action('emergency')

    def test_stop_only_writes_owned_request(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = Manager(directory)
            series = manager.runtime / 'abc123'
            write_json(manager.runtime / 'current.json', {'seriesId': 'abc123'})
            write_json(series / 'state.json', {'status': 'running', 'pid': 123})
            manager.action('stop')
            self.assertTrue((series / 'stop.request').exists())
            manager.action('emergency')
            self.assertTrue((series / 'emergency.request').exists())

    def test_start_persists_version_without_exposing_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, '.env.muzrob').write_text('BATTLESHIPS_TOKEN_MUZROB=do-not-read', encoding='utf-8')
            manager = Manager(directory)
            with patch('battleships.haki_panel.haki_processes', return_value=[]), \
                 patch.object(manager, 'refresh_rating'), \
                 patch('battleships.haki_panel.subprocess.run', side_effect=[Mock(stdout='abc123\n'), Mock(stdout='')]), \
                 patch('battleships.haki_panel.subprocess.Popen') as launch:
                manager.action('start')
                launch.assert_called_once()
                self.assertNotIn('do-not-read', str(launch.call_args))
            recreated = Manager(directory)
            _, state = recreated.current()
            self.assertEqual(state['commit'], 'abc123')
            self.assertEqual(state['stopPolicy'], 'RATING_GAIN')
            self.assertEqual(state['warmupMatches'], 3)
            with patch('battleships.haki_panel.haki_processes', return_value=[]):
                with self.assertRaises(RuntimeError):
                    recreated.action('start')

    def test_cancelled_series_never_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            series = Path(directory) / 'series'
            series.mkdir()
            (series / 'stop.request').touch()
            with patch('battleships.haki_supervisor.subprocess.Popen') as launch:
                supervise(directory, series)
                launch.assert_not_called()
            self.assertEqual(read_json(series / 'state.json')['stopReason'], 'CANCELLED_BEFORE_START')

    def test_supervisor_child_exits_without_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            series = Path(directory) / 'series'
            series.mkdir()
            child = Mock(pid=123, returncode=0)
            child.poll.return_value = 0
            with patch('battleships.haki_supervisor.haki_processes', return_value=[]), \
                 patch('battleships.haki_supervisor.subprocess.Popen', return_value=child) as launch:
                supervise(directory, series)
                self.assertEqual(launch.call_count, 1)
                self.assertEqual(launch.call_args.kwargs['env']['HAKI_STOP_POLICY'], 'RATING_GAIN')
                self.assertEqual(launch.call_args.kwargs['env']['HAKI_RATING_WARMUP'], '3')
            self.assertEqual(read_json(series / 'state.json')['status'], 'stopped')

    def test_series_counts_total_losses_and_skips_partial_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [{'event': 'match_summary', 'data': {'match_key': str(i), 'outcome': x}}
                    for i, x in enumerate(['LOSS', 'WIN', 'LOSS'])]
            rows.append(rows[0])
            Path(directory, 'session-test.jsonl').write_text('\n'.join(map(json.dumps, rows))+'\n{', encoding='utf-8')
            summary = journal_summary(directory)
            self.assertEqual((summary['losses'], summary['wins']), (2, 1))
            self.assertEqual(len(summary['results']), 3)

    def test_command_is_haki_only(self):
        command = bot_command(Path('/repo'), Path('/logs'))
        self.assertIn('BATTLESHIPS_TOKEN_MUZROB', command)
        self.assertIn('battleships.strategies.haki:HakiObservationBot', command)
        self.assertIn('RANKING', command)

    def test_journal_shows_actual_mode_opponent_and_transport_health(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [
                {'event': 'received', 'data': {'type': 'MATCH_STARTED',
                 'rules': {'modifier': 'SCHRODINGER_SHIP'}, 'opponent': {'botId': 'x', 'botName': 'Enemy'}}},
                {'event': 'match_context', 'data': {'bot_id': 'x'}},
                {'event': 'received', 'data': {'type': 'TURN_TIMED_OUT'}},
                {'event': 'received', 'data': {'type': 'SHOT_REJECTED'}},
                {'event': 'haki_action_timing', 'data': {'request_to_send_ms': 17}},
                {'event': 'match_summary', 'data': {'match_key': 'm', 'outcome': 'WIN', 'opponent': {'bot_id': 'x'}}}]
            Path(directory, 'session-test.jsonl').write_text('\n'.join(map(json.dumps, rows)), encoding='utf8')
            result = journal_summary(directory)
            self.assertEqual(result['modifier'], 'SCHRODINGER_SHIP')
            self.assertEqual(result['results'][0]['opponent']['name'], 'Enemy')
            self.assertEqual((result['timeouts'], result['rejections'], result['lastActionMs']), (1, 1, 17))
            self.assertFalse(result['active'])


if __name__ == '__main__':
    unittest.main()
