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
- Idempotent reward claims, score submissions, and unique-player closed-period settlement.

The seeded games are `daily_spin`, `prediction_card`, and `skill_check`. Only the
server-derived `skill_check` result is eligible for the leaderboard.

## Player frontend

The repository-owned Jinja, Alpine.js, JavaScript, CSS, and SVG frontend has no
Node build step or CDN dependency. Create or select a local demo identity from the
header, then use the lobby to play each real server flow:

- Daily Spin publishes a commitment, retains one browser-generated client seed for
  exact recovery, animates only after the authoritative result, retrieves the full
  reveal evidence, asks the server to verify it, and claims the recorded reward.
- Prediction Card submits only a labelled red or black choice and reveals the card
  returned by the server. Retrying the same session and choice returns that recorded
  outcome, while changing the choice is rejected. Incorrect predictions explicitly
  show a zero-point reward.
- Skill Check previews the server-generated unique-digit sequence, accepts keyboard
  or touch input, and submits those actions for the authoritative correct-prefix score.
  It then submits that returned score idempotently and claims the matching reward.
- The weekly Skill Check scoreboard presents the top three, paginated canonical
  score entries, and the current player's best rank. Generated `Player-…` labels
  keep player-entered display names and player UUIDs out of leaderboard entries.

Browser timers and animations are presentation only. Server timestamps enforce
expiry and cooldown, and browser refresh recovery reads durable state from PostgreSQL.
The browser operation journal retains request, session, commitment, seed, outcome,
and pending-action identifiers; uncertain mutations are never automatically replaced
with a new intent. Local player IDs remain a demonstration boundary, not authentication.

## Quick start

Only Docker and Docker Compose are required on the host.

```console
docker compose up --build -d
docker compose run --rm app python -m tools.seed
docker compose ps
```

Open the player lobby at `http://127.0.0.1:8000/`. The JSON API is under `/api/v1`,
and interactive OpenAPI documentation is at `/docs`. Copy `.env.example` to an
ignored `.env` only when local values need changing—never commit secrets.

## Common commands

Ordinary development commands use `compose.yaml` and never create or wait for
the test database:

```console
docker compose run --rm app alembic check
docker compose run --rm app python -m tools.simulate daily_spin --runs 100000
```

Quality checks use the explicit test overlay. Starting this stack waits for the
separate `db_test` database and supplies its isolated URL to both project commands
and the test suite, so Alembic and quality checks cannot target development data:

```console
docker compose -f compose.yaml -f compose.test.yaml up --build -d --wait
docker compose -f compose.yaml -f compose.test.yaml run --rm app ruff format --check .
docker compose -f compose.yaml -f compose.test.yaml run --rm app ruff check .
docker compose -f compose.yaml -f compose.test.yaml run --rm app mypy app tests tools
docker compose -f compose.yaml -f compose.test.yaml run --rm app pytest
```

Pytest migrates and clears only `db_test`; development migrations remain an
explicit `docker compose run --rm app alembic upgrade head`. The suite measures
branch coverage across `app` and `tools` and fails below 80%. Stop development
with `docker compose down`. Stop the test stack with
`docker compose -f compose.yaml -f compose.test.yaml down`; add `--volumes` only
when you intentionally want to remove both development and test container data.

## API at a glance

- `POST /api/v1/players`, sessions, play, claim, rewards, and audit reads.
- `GET /api/v1/players/{player_id}/game-state?game_key=...` for owner-checked,
  server-timestamped session, outcome, reward, fairness, score, and cooldown recovery.
- `POST /api/v1/fairness/commit` and `/fairness/evaluate`; retrieve or verify a proof by outcome.
- `POST /api/v1/scores`; read leaderboard and authenticated player rank.
- `POST /api/v1/leaderboards/skill_check/settle` for an authorized, closed ISO week.

The player-facing leaderboard is score-entry-based, so one generated public label
may appear more than once. Its UTC period, pagination, and personal best rank use
the same PostgreSQL-authoritative order whether a validated Redis projection serves
the read or the request falls back to PostgreSQL. Settlement intentionally ranks
unique players instead: each player contributes
only their best eligible score for the closed period. Settlement ranks are contiguous,
and both best-score selection and ties between players use the canonical order:
higher score, then earlier completion time, then lower session ID. Every durable
recipient retains the exact source score that established their rank.

All player-owned operations use the demo `X-Player-ID` boundary. Settlement additionally
requires `X-Settlement-Token`. These are local demo controls, not production authentication.
