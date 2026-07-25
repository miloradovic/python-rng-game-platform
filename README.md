# Python RNG Game Platform

A server-authoritative Python/FastAPI demo platform for free-to-play
games. It is a portfolio project focused on correctness: deterministic game rules,
provably fair RNG, idempotent rewards, and durable audit evidence.

It has no payments, cash value, wagering, KYC, or claim of regulatory compliance.

## What it demonstrates

- Server-owned game outcomes, scores, rewards, timestamps, and session lifecycle.
- Immutable game configuration versions and append-only audit/analytics evidence.
- Provably fair `daily_spin` commitment/reveal using HMAC-SHA256.
- Atomic session expiration that terminally expires committed fairness proofs,
  appends their hash-chained terminal event, and removes unrevealed seed custody.
- PostgreSQL as the durable authority; Redis only as a rebuildable leaderboard projection.
- Idempotent reward claims, score submissions, and closed-period settlement.

The seeded games are `daily_spin`, `prediction_card`, and `skill_check`. Only the
server-derived `skill_check` result is eligible for the leaderboard.

## Quick start

Only Docker and Docker Compose are required on the host.

```console
docker compose up --build -d
docker compose run --rm app python -m tools.seed
docker compose ps
```

The API is at `http://127.0.0.1:8000`; OpenAPI is at `/docs`. Copy `.env.example`
to an ignored `.env` only when local values need changing—never commit secrets.

## Common commands

```console
docker compose run --rm app ruff format --check .
docker compose run --rm app ruff check .
docker compose run --rm app mypy app tests tools
docker compose run --rm app pytest
docker compose run --rm app alembic check
docker compose run --rm app python -m tools.simulate daily_spin --runs 100000
```

## API at a glance

- `POST /api/v1/players`, sessions, play, claim, rewards, and audit reads.
- `POST /api/v1/fairness/commit` and `/fairness/evaluate`; retrieve or verify a proof by outcome.
- `POST /api/v1/scores`; read leaderboard and authenticated player rank.
- `POST /api/v1/leaderboards/skill_check/settle` for an authorized, closed ISO week.

All player-owned operations use the demo `X-Player-ID` boundary. Settlement additionally
requires `X-Settlement-Token`. These are local demo controls, not production authentication.
