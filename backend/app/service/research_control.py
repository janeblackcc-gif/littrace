"""Cancellation is separate from HTTP routes to avoid circular-import no-op fallbacks."""
import asyncio
from core.redis_client import get_redis_client

CANCEL_KEY_PREFIX = "research:cancel:"

def is_research_cancelled(session_id):
    return get_redis_client().get(CANCEL_KEY_PREFIX + session_id) is not None

def clear_cancel_flag(session_id):
    get_redis_client().delete(CANCEL_KEY_PREFIX + session_id)

def request_cancel(session_id):
    get_redis_client().set(CANCEL_KEY_PREFIX + session_id, "1", ex=3600)

async def db_call(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task  # Finish the short transaction before returning/releasing its connection.
        raise
