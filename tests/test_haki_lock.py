import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

from battleships.haki_lock import haki_process_lock


class HakiLockTest(unittest.TestCase):
    def test_cross_process_exclusion_and_release(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'haki.lock'
            code = ('from battleships.haki_lock import haki_process_lock; '
                    'import sys; '
                    'with_lock = haki_process_lock(sys.argv[1]); '
                    'with_lock.__enter__(); with_lock.__exit__(None,None,None)')
            with haki_process_lock(path):
                result = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'Another Haki process', result.stderr)
            result = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
