"""Bounded local media commands; never log source paths, URLs or stderr."""
import errno
import logging
import subprocess
import threading
import time

logger = logging.getLogger(__name__)
_slot = threading.BoundedSemaphore(1)


class MediaCommandError(RuntimeError):
    def __init__(self, code: str, *, transient: bool = False):
        super().__init__(code)
        self.code = code
        self.transient = transient


def run_media(args: list[str], *, timeout: int = 60) -> bytes:
    """Serialize codec work within the worker and retry only local busy/timeouts.

    This must never wrap a platform upload or post_create. subprocess.run kills
    and waits for a timed-out child before another attempt can acquire the slot.
    """
    if not _slot.acquire(timeout=timeout):
        raise MediaCommandError("MEDIA_BUSY", transient=True)
    try:
        for attempt in range(2):
            try:
                return subprocess.run(args, capture_output=True, timeout=timeout, check=True).stdout
            except subprocess.TimeoutExpired:
                error = MediaCommandError("MEDIA_TIMEOUT", transient=True)
            except FileNotFoundError:
                error = MediaCommandError("MEDIA_TOOL_MISSING")
            except OSError as exc:
                busy = exc.errno in {errno.EAGAIN, errno.ENOMEM}
                error = MediaCommandError("MEDIA_BUSY" if busy else "MEDIA_IO_ERROR", transient=busy)
            except subprocess.CalledProcessError as exc:
                stderr = exc.stderr or b""
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", errors="replace")
                busy = any(word in stderr.lower() for word in (
                    "resource temporarily unavailable", "pthread_create", "cannot allocate memory",
                    "can't start new thread", "failed to create thread",
                ))
                error = MediaCommandError("MEDIA_BUSY" if busy else "MEDIA_INVALID", transient=busy)
            logger.warning("channels_media_failed tool=%s code=%s attempt=%s", args[0], error.code, attempt + 1)
            if not error.transient or attempt:
                raise error from None
            time.sleep(1)
    finally:
        _slot.release()
