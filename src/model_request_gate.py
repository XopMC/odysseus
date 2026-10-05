"""One loaded-instance request gate shared by parents, children and Team calls."""
import asyncio
import os
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

from src.model_context import _local_base, get_serving_capacity, is_local_endpoint

_pools = WeakKeyDictionary()


class _Pool:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.active = 0
        self.limit = 1
        self.owners = {}


@asynccontextmanager
async def model_request_slot(endpoint_url, model):
    if (os.getenv('ODYSSEUS_LOCAL_MODEL_GATE', 'true').lower() in {'0', 'false', 'no', 'off'}
            or not is_local_endpoint(endpoint_url)):
        yield
        return
    capacity = await asyncio.to_thread(get_serving_capacity, endpoint_url, model)
    pools = _pools.setdefault(asyncio.get_running_loop(), {})
    key = (_local_base(endpoint_url), capacity.instance_id)
    pool = pools.setdefault(key, _Pool())
    task = asyncio.current_task()
    async with pool.condition:
        # Refresh changes the same pool's limit, never replaces a semaphore
        # while old requests still own its permits.
        pool.limit = max(1, capacity.parallel)
        if task in pool.owners:
            pool.owners[task] += 1
        else:
            await pool.condition.wait_for(lambda: pool.active < pool.limit)
            pool.active += 1
            pool.owners[task] = 1
    try:
        yield
    finally:
        async with pool.condition:
            pool.owners[task] -= 1
            if not pool.owners[task]:
                del pool.owners[task]
                pool.active -= 1
            pool.condition.notify_all()
