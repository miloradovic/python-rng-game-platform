"""Analytics time-boundary contract coverage."""

from datetime import UTC, datetime

import pytest

from app.services import InvalidAnalyticsRangeError, validate_analytics_range

pytestmark = pytest.mark.unit


def test_analytics_range_accepts_open_and_ordered_aware_boundaries() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 2, tzinfo=UTC)

    validate_analytics_range(None, None)
    validate_analytics_range(start, None)
    validate_analytics_range(None, end)
    validate_analytics_range(start, end)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 1, 1), None),
        (None, datetime(2026, 1, 2)),
        (datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
        (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
    ],
)
def test_analytics_range_rejects_naive_empty_or_reversed_boundaries(
    start: datetime | None, end: datetime | None
) -> None:
    with pytest.raises(InvalidAnalyticsRangeError):
        validate_analytics_range(start, end)
