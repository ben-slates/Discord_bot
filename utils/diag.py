import asyncio
import logging
import sys
import threading
import traceback
from functools import wraps
import time


def identify_block_source(loop: asyncio.AbstractEventLoop, max_stack=6):
    """Try to identify the coroutine/task or main-thread frame that was running
    when the event loop was observed blocked. Returns a dict with diagnostic info.
    This function is designed to be reasonably lightweight when called infrequently
    (e.g. throttled to once per minute from the watchdog)."""
    info = {
        "type": "unknown",
        "detail": None,
    }

    try:
        # A watchdog only runs after the loop resumes.  Other task stacks are
        # therefore normally pending and cannot identify the code that stalled it.
        # Sample only the main-thread frame and label it accurately as a sample.
        frames = sys._current_frames()
        main_ident = threading.main_thread().ident
        fr = frames.get(main_ident)
        if fr:
            # sys._current_frames already returns the currently executing frame;
            # walking f_back reaches the outer bot.run frame and is misleading.
            code = fr.f_code
            info["type"] = "thread"
            info["detail"] = {
                "thread": "MainThread",
                "file": code.co_filename,
                "function": code.co_name,
                "lineno": fr.f_lineno,
            }
            return info
    except Exception:
        logging.exception("Failed to run identify_block_source")

    return info


class EventLoopBlockSampler:
    """Sample the main thread while (not after) the loop is stalled."""
    def __init__(self, threshold: float = 2.0):
        self.threshold = threshold
        self._last_pulse = time.monotonic()
        self._reported = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="event-loop-block-sampler", daemon=True)

    def start(self):
        self._thread.start()

    def pulse(self):
        with self._lock:
            self._last_pulse = time.monotonic()
            self._reported = False

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=1)

    def _run(self):
        while not self._stop.wait(0.1):
            with self._lock:
                stalled_for = time.monotonic() - self._last_pulse
                if stalled_for < self.threshold or self._reported:
                    continue
                self._reported = True
            frame = sys._current_frames().get(threading.main_thread().ident)
            if not frame:
                continue
            code = frame.f_code
            logging.warning(
                "Event-loop stall sampled while active: %.3fs at %s:%s:%s",
                stalled_for, code.co_filename, code.co_name, frame.f_lineno,
            )


def instrument_async(threshold: float = 0.25):
    """Decorator for async functions to log if execution exceeds `threshold` seconds.
    Keep the wrapper lightweight: only measures wall time and logs a single warning
    when the threshold is exceeded."""
    def _dec(func):
        @wraps(func)
        async def _wrapped(*args, **kwargs):
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            try:
                return await func(*args, **kwargs)
            finally:
                try:
                    dt = loop.time() - t0
                    if dt >= threshold:
                        # Best-effort: fetch source location
                        try:
                            code = func.__code__
                            logging.warning(f"Slow async function: {func.__qualname__} took {dt:.3f}s at {code.co_filename}:{code.co_name}:{code.co_firstlineno}")
                        except Exception:
                            logging.warning(f"Slow async function: {func.__qualname__} took {dt:.3f}s")
                except Exception:
                    pass
        return _wrapped
    return _dec
