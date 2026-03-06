# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Powered by caffeine and stackoverflow
import asyncio
import functools
import os
import sys
import uuid
from datetime import datetime, timezone

import curl_cffi as curl
import machineid

POSTHOG_KEY = "phc_giRHYHo4460O5UPxySajdO9L4KDRsjSmNQACA7uG9px"
POSTHOG_BATCH_URL = "https://app.posthog.com/batch/"
APP_ID = "delta-farmer"

_queue: list[dict] = []
_context: dict = {}
_session_id = str(uuid.uuid4())


@functools.cache
def _anon_id() -> str:
    return machineid.hashed_id(APP_ID)[:16]


async def _async_flush() -> None:
    if not _queue:
        return

    batch = list(_queue)
    _queue.clear()

    try:
        async with curl.AsyncSession() as s:
            pld = {"api_key": POSTHOG_KEY, "batch": batch}
            await s.post(POSTHOG_BATCH_URL, json=pld, timeout=5)
    except Exception:
        pass


async def _flush_loop(interval: int) -> None:
    try:
        while True:
            await asyncio.sleep(interval)
            await _async_flush()
    except asyncio.CancelledError:
        await _async_flush()  # final flush before shutdown
        raise


async def _heartbeat_loop(interval: int) -> None:
    try:
        while True:
            await asyncio.sleep(interval)
            track("heartbeat")
    except asyncio.CancelledError:
        raise


def init(
    exchange: str, command: str, version: str = "", flush_interval=120, heartbeat_interval=3600
) -> None:
    pld = {
        "exchange": exchange,
        "command": command,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "platform": sys.platform,
        "version": version.strip(),
        "$lib": APP_ID,
        "$session_id": _session_id,
    }
    _context.update(pld)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_flush_loop(flush_interval), name="telemetry-flush")
        loop.create_task(_heartbeat_loop(heartbeat_interval), name="telemetry-heartbeat")
    except RuntimeError:
        pass  # no event loop running


def track(event: str, props: dict | None = None) -> None:
    if os.getenv("DF_TELEMETRY") == "0":
        return

    ts = datetime.now(timezone.utc).isoformat()
    props = {**_context, **(props or {})}
    _queue.append({"event": event, "distinct_id": _anon_id(), "timestamp": ts, "properties": props})
