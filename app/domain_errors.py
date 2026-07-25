"""Domain failures shared by pure rules and service orchestration."""


class DomainError(Exception):
    code = "domain_error"


class InvalidTransitionError(DomainError):
    code = "invalid_transition"
