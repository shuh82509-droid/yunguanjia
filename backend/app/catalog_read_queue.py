"""Keep slow catalog reads out of concurrent HTTP worker-pool stampedes."""
import asyncio
from weakref import WeakKeyDictionary

from starlette.concurrency import run_in_threadpool


class CatalogReadQueue:
    """Serialize cache fills without holding a thread or DB session while queued.

    The callback owns its database session. A disconnected requester cannot close
    that session or release the lane before an already-running read finishes.
    Different groups (stats and facets) can still progress independently.
    """

    def __init__(self):
        self._loops = WeakKeyDictionary()
        self._running = set()

    async def run(self, group, callback):
        loop = asyncio.get_running_loop()
        locks = self._loops.setdefault(loop, {})
        lock = locks.setdefault(group, asyncio.Lock())
        state = {"started": False}

        async def execute():
            async with lock:
                state["started"] = True
                return await run_in_threadpool(callback)

        task = asyncio.create_task(execute())
        self._running.add(task)

        def settled(done):
            self._running.discard(done)
            if not done.cancelled():
                done.exception()  # Consume errors when the requester disconnected.

        task.add_done_callback(settled)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not state["started"]:
                task.cancel()
            raise


def count_ordered_asset_hits(rows, threshold=50000):
    """Same max-per-target/day and rounded-total rule as the asset GMV summary.

    Input is ordered by asset id, so only one asset's deduplication state is kept.
    Rows contain asset, account, plan, video, day, the original metrics, updated_at.
    Invalid/missing values remain absent; negative amounts follow the existing
    nonnegative GMV contract. Aggregate records are never attributed to a person.
    """
    current_asset = None
    daily = {}
    count = 0

    def hit():
        return int(round(sum(daily.values()), 2) > threshold)

    for asset_id, advertiser_id, plan_id, video_id, stat_date, metrics, _updated_at in rows:
        if current_asset is not None and asset_id != current_asset:
            count += hit()
            daily.clear()
        current_asset = asset_id
        raw = (metrics or {}).get("pay_order_amount")
        if raw is None:
            continue
        try:
            gmv = max(0.0, float(raw))
        except (TypeError, ValueError):
            continue
        key = (advertiser_id or "", plan_id or "", video_id or "", stat_date)
        previous = daily.get(key)
        if previous is None or gmv > previous:
            daily[key] = gmv
    if current_asset is not None:
        count += hit()
    return count
