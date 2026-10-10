"""A per-user cross-checkout lock for the personal Haki process only."""
from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def haki_process_lock(path=None):
    # All checkouts belonging to this OS user resolve the same lock. The handle
    # is released by the OS on exit; a leftover file does not imply a live bot.
    path = Path(path) if path else Path.home() / '.battleships' / 'haki-process.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    locked = False
    try:
        if not path.stat().st_size:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (OSError, BlockingIOError):
            raise RuntimeError('Another Haki process already owns the lock') from None
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
