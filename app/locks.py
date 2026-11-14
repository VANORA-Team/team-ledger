"""Advisory locks shared by the web server and the bot processes."""
import os
import threading
import time
from contextlib import contextmanager

from . import config

_held = threading.local()


@contextmanager
def file_lock(name, *, exclusive=True, timeout=30):
    config.DATA.mkdir(parents=True, exist_ok=True)
    path = config.DATA / name
    held = getattr(_held, 'locks', None)
    if held is None:
        held = _held.locks = {}
    key = str(path.resolve())
    if key in held:
        if exclusive and not held[key]:
            raise RuntimeError('Cannot upgrade a shared data lock')
        yield
        return
    with path.open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
        else:
            import fcntl
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == 'nt':
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError('账本正在备份或恢复，请稍后重试') from None
                time.sleep(0.05)
        held[key] = exclusive
        try:
            yield
        finally:
            del held[key]
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def data_guard(*, exclusive=False):
    return file_lock('.ledger.lock', exclusive=exclusive)
