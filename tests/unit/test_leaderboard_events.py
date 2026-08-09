"""Unit coverage for the bounded process-local leaderboard event hub."""

from datetime import UTC, datetime

import pytest

from app.leaderboard_events import LeaderboardChange, LeaderboardEventHub


async def test_hub_filters_and_unsubscribes() -> None:
    period_start = datetime(2026, 8, 3, tzinfo=UTC)
    hub = LeaderboardEventHub()
    subscription = await hub.subscribe(game_key="skill_check", period_start=period_start)

    await hub.publish(LeaderboardChange("daily_spin", period_start))
    expected = LeaderboardChange("skill_check", period_start)
    await hub.publish(expected)

    assert await subscription.receive() is expected
    await subscription.close()
    await subscription.close()
    assert hub.subscriber_count == 0


async def test_hub_keeps_newest_signal_when_queue_is_full() -> None:
    period_start = datetime(2026, 8, 3, tzinfo=UTC)
    hub = LeaderboardEventHub(queue_size=1)
    subscription = await hub.subscribe(game_key="skill_check", period_start=period_start)
    first = LeaderboardChange("skill_check", period_start)
    newest = LeaderboardChange("skill_check", period_start)

    await hub.publish(first)
    await hub.publish(newest)

    assert await subscription.receive() is newest
    await subscription.close()


async def test_shutdown_closes_current_and_future_subscriptions() -> None:
    period_start = datetime(2026, 8, 3, tzinfo=UTC)
    hub = LeaderboardEventHub()
    current = await hub.subscribe(game_key="skill_check", period_start=period_start)

    await hub.shutdown()
    await hub.shutdown()
    future = await hub.subscribe(game_key="skill_check", period_start=period_start)

    assert hub.subscriber_count == 0
    assert await current.receive() is None
    assert await future.receive() is None


async def test_invalid_queue_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="queue_size must be positive"):
        LeaderboardEventHub(queue_size=0)
