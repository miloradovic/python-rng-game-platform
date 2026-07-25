# AGENTS.md

## Purpose and scope

This is the contributor guide for the Python RNG Game Platform. Read it before
changing application code, configuration, migrations, tests, tooling, or docs.

The project is a single-deployment, free-to-play FastAPI portfolio application.
It demonstrates server-authoritative games, durable audit evidence, idempotent
operations, concurrency-safe settlement, and provably fair RNG. It is not a
commercial platform and excludes payments, wagering, KYC, compliance claims,
microservices, cloud deployment, and a complex frontend. `README.md` is the
current product and API reference; keep it synchronized with externally visible
behavior.

## Required environment and commands

- Docker and Docker Compose are the only project runtime dependencies on the
  host. Do not run Python, uv, Alembic, pytest, Ruff, mypy, PostgreSQL, Redis,
  or project tools directly on the host.
- Use the existing Compose services only. Start the ordinary stack with
  `docker compose up --build -d`; it contains only runtime services. Start the
  isolated quality-test stack with
  `docker compose -f compose.yaml -f compose.test.yaml up --build -d --wait`.
  Run ordinary one-off commands with `docker compose run --rm app <command>`
  and quality checks with both Compose files.
- The supported runtime is Python 3.14 (`pyproject.toml` and `Dockerfile`).
  Dependencies and container images are pinned in `pyproject.toml`/`uv.lock`
  and Compose/Dockerfile; update the manifest, lockfile, image, tests, and docs
  together when changing them.
- The standard checks are:

  ```console
  docker compose -f compose.yaml -f compose.test.yaml run --rm app ruff format --check .
  docker compose -f compose.yaml -f compose.test.yaml run --rm app ruff check .
  docker compose -f compose.yaml -f compose.test.yaml run --rm app mypy app tests tools
  docker compose -f compose.yaml -f compose.test.yaml run --rm app pytest
  ```

- Apply schema changes through reviewed Alembic migrations. Use
  `docker compose run --rm app alembic upgrade head` to prepare a local stack.
- Do not commit `.env` files, credentials, database dumps, caches, or container
  state. Add non-secret configuration defaults to `.env.example` when needed.

## Implemented architecture

Keep the existing, deliberately compact structure:

```text
app/
  api/            # FastAPI transport adapters
  services.py     # use cases and transaction orchestration
  repositories.py # database queries and persistence operations
  models.py       # SQLAlchemy 2.0 typed mappings
  schemas.py      # Pydantic v2 boundary schemas
  rng.py          # cryptographic outcome and proof verification logic
  cache.py         # rebuildable Redis projections
  config.py        # validated settings
tests/
  unit/ integration/ api/ concurrency/
alembic/versions/ # reviewed, versioned PostgreSQL schema changes
tools/             # explicit seed, simulation, and projection-rebuild CLIs
```

Maintain the dependency direction `api -> services -> repositories/database`.
Handlers validate and translate HTTP; services own business rules and visible
transaction boundaries; repositories own persistence. Keep game rules and
cryptographic derivation deterministic and independently testable. Do not add
service extraction, generic framework layers, a broker, or another database.

## Non-negotiable domain invariants

- The server derives outcomes, scores, rewards, eligibility, cooldowns, claim
  state, and timestamps. Client input may express intent or a permitted choice;
  it never selects an outcome, score, reward, config version, or settlement
  period.
- Published game configuration is immutable. Each outcome retains the exact
  configuration version used, so historical results remain reproducible.
- Sessions have enforced lifecycles. Expired or completed sessions cannot be
  played, scored, claimed, or otherwise advanced.
- Claims, final-score submissions, leaderboard submissions, and settlement are
  retry-safe and idempotent. Enforce this with database constraints and
  transaction-safe logic, not an in-memory pre-check.
- PostgreSQL is authoritative for durable domain state, evidence, rewards, and
  settlements. Redis is optional and rebuildable; leaderboard ordering must be
  reproducible from PostgreSQL.
- Audit and analytics evidence is append-only from the application's
  perspective. Do not modify or remove evidence to simplify a response or test.
- Player-visible randomness uses cryptographic algorithms only. The fairness
  proof must retain the server-seed commitment, revealed seed, client seed,
  nonce, game key, configuration version, mapping/version inputs, and derived
  values necessary for independent verification. Preserve the existing v1 proof
  byte format and version it before changing semantics.
- Player-owned reads and writes require ownership checks. Treat `X-Player-ID`
  and `X-Settlement-Token` as local demo boundaries, not production-grade
  authentication; do not weaken their existing checks.

## Engineering rules

- Use modern, fully typed Python compatible with 3.14. Follow the configured
  Ruff and strict mypy rules; avoid broad suppressions.
- Use async database access consistently. Validate external data with Pydantic
  at the boundary, then enforce concurrent correctness with database
  constraints and explicit transactions.
- Use UTC-aware timestamps. Keep generated IDs, enum values, defaults, and
  state transitions explicit.
- Treat URLs, seeds, tokens, and personally identifying data as sensitive:
  do not expose them in logs, errors, or API responses unless the fairness
  reveal protocol explicitly requires it.
- A database schema change needs a migration, suitable constraints/indexes,
  downgrade review, and integration coverage. Do not alter schemas manually.
- Document public interfaces and non-obvious security, fairness, or settlement
  assumptions. Keep OpenAPI descriptions and README examples accurate.

## Testing and handoff

- Add the smallest useful test at the correct layer. Use `unit` for
  deterministic rules, `integration` for PostgreSQL/Redis/migration behavior,
  `api` for HTTP contracts, and `concurrency` for races and duplicate requests.
- Run focused checks during development and the four standard containerized
  checks before handoff when applicable. Report any check not run or blocked;
  never describe it as passing.
- For changes affecting fairness, migrations, security, idempotency, or
  concurrency, inspect the final diff and state the invariant protected.
- Preserve unrelated user changes. Prefer `apply_patch` for edits; do not use
  hard resets, broad deletion, or destructive migration changes without explicit
  approval.

## Definition of done

A change is complete when implementation, migrations (when needed), tests,
documentation, and Docker workflow agree; relevant containerized checks have
passed; and the handoff records the exact validation and any genuine limitation.
