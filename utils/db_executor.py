"""Helpers to run blocking DB work on a bounded threadpool.

Use `await run_db(func, *args, **kwargs)` for synchronous SQLAlchemy work
to avoid saturating the default Python threadpool and opening too many DB
connections concurrently.
"""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import functools
import logging
import time

# Tunable: number of threads dedicated to DB work. Keep small to avoid
# opening too many DB connections concurrently.
DB_THREAD_WORKERS = 6
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=DB_THREAD_WORKERS, thread_name_prefix="db-worker")


async def run_db(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    pfunc = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(_DB_EXECUTOR, pfunc)


async def run_db_profiled(label, func, *args, **kwargs):
    """Run DB work and log executor queue vs. worker/SQL timings when slow."""
    submitted = time.perf_counter()
    loop = asyncio.get_running_loop()

    def worker():
        started = time.perf_counter()
        from database import profile_database_work
        with profile_database_work() as profile:
            result = func(*args, **kwargs)
        return result, started - submitted, profile

    result, queue_seconds, profile = await loop.run_in_executor(_DB_EXECUTOR, worker)
    total = time.perf_counter() - submitted
    if total >= 0.2:
        logging.warning(
            "DB timing %s: total=%.3fs queue=%.3fs worker=%.3fs checkout=%.3fs "
            "sql=%.3fs (%d statements, pre_ping=%.3fs) flush=%.3fs commit=%.3fs",
            label, total, queue_seconds, total - queue_seconds,
            profile.checkout_seconds, profile.sql_seconds, profile.statement_count, profile.pre_ping_seconds,
            profile.flush_seconds, profile.commit_seconds,
        )
        for statement, elapsed in profile.statements:
            logging.warning("DB statement %s: %.3fs %s", label, elapsed, statement)
    return result


def shutdown():
    try:
        _DB_EXECUTOR.shutdown(wait=False)
    except Exception:
        pass
