"""Atomic file writes that tolerate Windows reader-locks.

On POSIX, os.replace() onto a destination that another process holds open
succeeds (rename is inode-based). On Windows it fails with
PermissionError [WinError 5] / FileExistsError because the open destination
is locked. The dashboard polls workspace JSON files continuously while eidos
writes them, so collisions are frequent and were crashing the tick loop.

replace_with_retry() retries the replace for up to ~1s; readers hold each
file open only for microseconds, so a free window appears almost immediately.
Callers that treat the write as best-effort (telemetry) should still wrap the
call in try/except OSError; callers for whom the write is critical (plan, wal,
knowledge) let the final PermissionError propagate after the retry budget.
"""

import os
import time


def replace_with_retry(src, dst, *, retries: int = 40, delay: float = 0.025):
    """os.replace(src, dst), retrying on transient Windows lock errors.

    Raises the last error if the destination stays locked past the budget.
    """
    src = os.fspath(src)
    dst = os.fspath(dst)
    last = None
    for _ in range(retries):
        try:
            os.replace(src, dst)
            return
        except FileNotFoundError:
            # Source vanished — nothing we can retry into.
            raise
        except PermissionError as e:  # WinError 5: dst held open by a reader
            last = e
            time.sleep(delay)
        except OSError as e:  # e.g. WinError 183 on odd filesystems
            last = e
            time.sleep(delay)
    if last is not None:
        raise last
