# Play Fast. Climb Faster.

**A server-authoritative competitive gaming platform built with Python and FastAPI.**

Three quick-play games feed one live competitive loop: play, score, watch the
leaderboard move, and come back to take the lead. The experience is designed to
feel immediate and social while the backend protects every outcome, score, and
reward.

This is a free-to-play portfolio project. It has no payments, wagering, cash
value, KYC, or claim of regulatory compliance.

## The player experience

The compact lobby puts games first. Each title has a short replay cadence, a
clear score, and a live weekly leaderboard embedded beside the action—never
hidden on a separate page.

| Game | Player loop | Competitive score |
| --- | --- | --- |
| **Hourly Spin** | Return every hour, spin the animated wheel, and reveal a provably fair prize. | Prize value |
| **Red or Black** | Make the call, reveal the card, and see instantly whether the prediction landed. | 25 points for a win |
| **Memory Rush** | Memorize the sequence, enter it under pressure, and chase a perfect run. | Up to 1,000 points |

Every game includes:

- A live top-10 leaderboard and the current player's personal best rank.
- Animated rank changes when another player moves ahead.
- A weekly reset countdown that keeps the competition fresh.
- Responsive layouts with the leaderboard beside the game on desktop and below
  it on smaller screens.
- Motion-reduced alternatives and accessible status announcements.

## Built for competition

The leaderboard is part of the game loop, not a reporting screen. New scores
trigger privacy-safe live updates, clients refetch authoritative rankings, and
brief motion draws attention to meaningful position changes. If the live signal
is interrupted, periodic refresh and reconnect recovery bring the board back in
sync.

Public `Player-…` labels protect player-entered names and identifiers. Rankings
remain reproducible from PostgreSQL, while Redis acts only as a fast, disposable
projection. Weekly settlement uses each player's best eligible result, with
deterministic tie-breaking and a durable link to the score that earned the rank.

## Product energy, engineering discipline

The frontend is deliberately lightweight—server-rendered Jinja, Alpine.js,
JavaScript, CSS, and SVG, with no Node build pipeline or CDN dependency. Behind
it sits a compact architecture shaped around the failure modes that matter in
real game systems:

- **Server authority:** the browser expresses intent; the server owns outcomes,
  scores, rewards, cooldowns, timestamps, and session state.
- **Provably fair play:** Hourly Spin uses an HMAC-SHA256 commitment/reveal flow
  with independently verifiable evidence.
- **Safe retries:** play, score submission, reward collection, and settlement are
  idempotent and enforced at the database boundary.
- **Reproducible history:** published game configurations are immutable, and
  every outcome retains the exact version that produced it.
- **Durable recovery:** PostgreSQL is the source of truth; browser refreshes and
  Redis loss do not erase completed play.
- **Auditable behavior:** append-only evidence records the lifecycle of outcomes,
  rewards, and fairness proofs.
- **Concurrency-aware settlement:** database constraints and explicit
  transactions prevent duplicate rewards and conflicting advancement.

The result is intentionally more than a polished UI demo. It shows how product
engagement, fairness, reliability, privacy, and operational simplicity can be
designed as one system.

## Architecture at a glance

```text
Browser UI  ->  FastAPI routes  ->  Services  ->  PostgreSQL
                                      |
                                      +------->  Redis leaderboard cache
```

API handlers validate and translate requests. Services own game rules and
transaction boundaries. Repositories own persistence. Redis can accelerate a
leaderboard read, but it can never become the authority for a score, reward, or
settlement.

## Run it

Docker and Docker Compose are the only host requirements.

```console
docker compose up --build -d
docker compose run --rm app python -m tools.seed
```

Open `http://127.0.0.1:8000/` to play. The API is available under `/api/v1`, with
interactive documentation at `/docs`.

## Technology

Python 3.14 · FastAPI · SQLAlchemy 2.0 · PostgreSQL · Redis · Alembic · Pydantic
v2 · Jinja · Alpine.js · pytest · Ruff · mypy · Docker Compose

The automated suite covers deterministic game rules, API contracts, database
integration, migrations, retries, and concurrency behavior with an enforced
branch-coverage threshold.
