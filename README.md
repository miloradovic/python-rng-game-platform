# Python RNG Game Platform

A server-authoritative Python game platform focused on provably fair RNG, auditable outcomes, idempotent rewards, and reliable gameplay infrastructure.

## Stack

Python, FastAPI, PostgreSQL, Redis, SQLAlchemy, Alembic, Pydantic, Pytest, and Docker Compose.

This MVP is free-to-play demo software. It has no payments, cash value, wagering, KYC, or claim of
regulatory certification.

## Server-authoritative contract

The client can identify a player, request a game, choose a prediction, or submit skill actions. The
server owns eligibility, cooldowns, configuration selection, expiry, timestamps, random outcomes,
score validation, reward values, and claim state. Request schemas reject client-supplied outcomes,
scores, reward tiers or values, configuration versions, and claim state.

The three seeded games are:

- `daily_spin`: cryptographically derived weighted selection; the bound config owns reward bands.
- `prediction_card`: the client chooses a card color; the server derives and evaluates the result.
- `skill_check`: the server creates a challenge and calculates the score from submitted actions.

Every session binds one published configuration version. Published configuration contents are
database-protected from mutation; a changed configuration requires a new version. Outcomes and
audit evidence retain the exact historical version identifier.

Sessions move from `active` to one terminal state: `completed`, `expired`, or `cancelled`. Expired
or terminal sessions cannot be played again. Reward issuance is atomic with accepted gameplay and
unique per outcome. Claims lock the durable reward row and move `issued` to `claimed`; a retry or
concurrent duplicate returns the same claimed reward without another audit, analytics, or reward
effect.

PostgreSQL is authoritative. Redis is optional and currently affects readiness reporting only; no
gameplay, reward, audit, or analytics fact depends on it.

## Local workflow

Only Docker and Docker Compose are required on the host. Copy `.env.example` to an ignored `.env`
if different development values are needed; never commit `.env` or production secrets.

```console
docker compose up --build -d
docker compose run --rm app alembic upgrade head
docker compose run --rm app python -m tools.seed
docker compose ps
```

The API is available at `http://127.0.0.1:8000`, OpenAPI at `/docs`, liveness at `/live`, and
dependency readiness at `/ready`. Stop without deleting PostgreSQL or Redis volumes with
`docker compose down`.

Quality gates use the same development image:

```console
docker compose run --rm app ruff format --check .
docker compose run --rm app ruff check .
docker compose run --rm app mypy app tests tools
docker compose run --rm app pytest
```

## API flow

Create a player, preserving the returned identifier as the demo authorization header:

```http
POST /api/v1/players
Content-Type: application/json

{"display_name":"Demo Player"}
```

For `daily_spin`, a player can commit the server seed before evaluation, then submit only a client seed. Both endpoints require `X-Player-ID`; commitment and evaluation retries are idempotent.

```http
POST /api/v1/fairness/commit

{"session_id":"{daily_spin_session_id}"}
```

```http
POST /api/v1/fairness/evaluate

{"proof_id":"{proof_id}","client_seed":"demo-client-seed"}
```

Start, play, and claim a session:

```http
POST /api/v1/sessions
X-Player-ID: {player_id}
Content-Type: application/json

{"request_id":"{retry_safe_uuid}","player_id":"{player_id}","game_key":"daily_spin"}
```

```http
POST /api/v1/sessions/{session_id}/play
X-Player-ID: {player_id}
Content-Type: application/json

{}
```

```http
POST /api/v1/sessions/{session_id}/claim
X-Player-ID: {player_id}
Content-Type: application/json

{}
```

Related reads are:

- `GET /api/v1/games` and `GET /api/v1/games/{game_key}/config`
- `GET /api/v1/sessions/{session_id}`
- `GET /api/v1/players/{player_id}/rewards?limit=20&offset=0`
- `GET /api/v1/audit/outcomes/{outcome_id}`
- `GET /api/v1/analytics/game-summary`

All player/session/reward/audit/analytics reads require `X-Player-ID` and enforce ownership. Stable
domain failures use `{"error":{"code":"..."}}`; Pydantic validation failures use FastAPI's 422
shape.

## Audit and analytics

Session starts, accepted outcomes, reward issuance, and claims append evidence in the same database
transaction as the business action. Analytics event keys derive from durable session, outcome, and
reward identifiers, so retries do not inflate metrics. Payloads exclude challenges, seeds,
credentials, and raw client input.

`GET /api/v1/analytics/game-summary` returns the authenticated player's per-game play count,
issued-reward count, and average issued reward. Optional `game_key`, `start_at`, and `end_at`
filters use an inclusive start and exclusive end; timestamps require an offset. Results are ordered
by game key and empty filters return an empty `items` list.

## Migrations and data

Alembic is the only schema-change mechanism. Revisions `0001` through `0008` establish the
foundation, catalogue, lifecycle, gameplay, rewards, analytics, and additive fairness-proof
persistence. Revisions `0007` and `0008` add isolated seed custody, proof evidence, append-only
hash-chain events, and forward-only proof lifecycle protection; they do not backfill earlier
outcomes as provably fair. Revision `0007` refuses to downgrade while proof evidence exists,
preserving finalized audit data.

`python -m tools.seed` is explicit and repeatable. Application startup and migrations do not
silently create catalogue rows.

## Security boundary and known extensions

Connection URLs and the HMAC key are secret settings and are not logged. Client errors expose only
stable domain codes or validation details, not SQL, stack traces, or configuration. The current
`X-Player-ID` boundary is intentionally demo authorization, not production authentication.

The current HMAC-SHA256 rejection-sampling provider is a cryptographic, unbiased outcome seam. The
mandatory next module is Plan 3: commitment/reveal, public proof verification, tamper detection,
seed rotation, and distribution simulation without changing the rule-facing `OutcomeProvider`
contract. Plan 4 may later add PostgreSQL-authoritative score submissions and settlements plus a
disposable Redis leaderboard projection. Neither extension should introduce microservices, a
message broker, or a second durable source of truth.

