"""Process-local, privacy-safe leaderboard invalidation fan-out."""

import asyncio
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class LeaderboardChange:
    """Public invalidation coordinates; deliberately contains no score or player data."""

    game_key: str
    period_start: datetime


class LeaderboardSubscription:
    """One bounded subscriber queue owned by a :class:`LeaderboardEventHub`."""

    def __init__(
        self,
        hub: LeaderboardEventHub,
        game_key: str,
        period_start: datetime,
        queue_size: int,
    ) -> None:
        self._hub = hub
        self.game_key = game_key
        self.period_start = period_start
        self._queue: asyncio.Queue[LeaderboardChange | None] = asyncio.Queue(queue_size)
        self._closed = False

    async def receive(self) -> LeaderboardChange | None:
        """Wait for an invalidation or the hub shutdown sentinel."""

        return await self._queue.get()

    async def close(self) -> None:
        """Remove this subscriber promptly and idempotently."""

        if not self._closed:
            self._closed = True
            await self._hub.unsubscribe(self)

    def _offer(self, event: LeaderboardChange | None) -> None:
        """Keep the newest signal when a slow subscriber fills its bounded queue."""

        if self._closed:
            return
        if self._queue.full():
            self._queue.get_nowait()
        self._queue.put_nowait(event)


class LeaderboardEventHub:
    """Small single-process broadcaster for disposable invalidation signals."""

    def __init__(self, *, queue_size: int = 8) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._queue_size = queue_size
        self._subscriptions: set[LeaderboardSubscription] = set()
        self._lock = asyncio.Lock()
        self._shutdown = False

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    async def subscribe(self, *, game_key: str, period_start: datetime) -> LeaderboardSubscription:
        """Register a filtered bounded subscription."""

        subscription = LeaderboardSubscription(self, game_key, period_start, self._queue_size)
        async with self._lock:
            if self._shutdown:
                subscription._offer(None)
            else:
                self._subscriptions.add(subscription)
        return subscription

    async def unsubscribe(self, subscription: LeaderboardSubscription) -> None:
        """Forget a disconnected subscriber."""

        async with self._lock:
            self._subscriptions.discard(subscription)

    async def publish(self, event: LeaderboardChange) -> None:
        """Offer an invalidation without waiting for slow consumers."""

        async with self._lock:
            subscriptions = tuple(self._subscriptions)
        for subscription in subscriptions:
            if (
                subscription.game_key == event.game_key
                and subscription.period_start == event.period_start
            ):
                subscription._offer(event)

    async def shutdown(self) -> None:
        """Close every stream and reject future live subscriptions."""

        async with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            subscriptions = tuple(self._subscriptions)
            self._subscriptions.clear()
        for subscription in subscriptions:
            subscription._offer(None)
