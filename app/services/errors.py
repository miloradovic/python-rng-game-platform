"""Stable domain failures translated only at the transport boundary."""


class DomainError(Exception):
    code = "domain_error"


class NotFoundError(DomainError):
    code = "not_found"


class ForbiddenError(DomainError):
    code = "forbidden"


class InactiveGameError(DomainError):
    code = "game_inactive"


class InactivePlayerError(DomainError):
    code = "player_inactive"


class CooldownError(DomainError):
    code = "cooldown_active"


class ActiveSessionError(DomainError):
    code = "active_session_exists"


class InvalidTransitionError(DomainError):
    code = "invalid_transition"


class InvalidPlayError(DomainError):
    code = "invalid_play"


class DailySpinFairnessRequiredError(DomainError):
    code = "daily_spin_fairness_required"


class IdempotencyConflictError(DomainError):
    code = "idempotency_conflict"


class SessionExpiredError(DomainError):
    code = "session_expired"


class RewardUnavailableError(DomainError):
    code = "reward_unavailable"


class InvalidAnalyticsRangeError(DomainError):
    code = "invalid_analytics_range"


class LeaderboardGameIneligibleError(DomainError):
    code = "leaderboard_game_ineligible"


class LeaderboardPeriodClosedError(DomainError):
    code = "leaderboard_period_closed"


class LeaderboardEntryNotFoundError(DomainError):
    code = "leaderboard_entry_not_found"


class LeaderboardPeriodOpenError(DomainError):
    code = "leaderboard_period_open"


class SettlementForbiddenError(DomainError):
    code = "settlement_forbidden"
