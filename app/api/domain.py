"""Player and catalogue HTTP adapters."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories, services
from app.cache import (
    parse_leaderboard_member,
    project_final_score,
    projected_page,
    projected_player_rank,
)
from app.database import get_session as get_database_session
from app.rng import OutcomeProvider
from app.rng import RewardBand as FairnessRewardBand
from app.schemas import (
    ClaimRequest,
    FairnessCommitRequest,
    FairnessCommitResponse,
    FairnessEvaluateRequest,
    FairnessEvaluateResponse,
    FairnessProofResponse,
    FairnessVerificationResponse,
    FinalScoreCreate,
    FinalScoreResponse,
    GameConfigResponse,
    GameListResponse,
    GameResponse,
    GameSummaryItem,
    GameSummaryResponse,
    LeaderboardEntry,
    LeaderboardResponse,
    OutcomeAuditResponse,
    OutcomeResponse,
    PlayerCreate,
    PlayerRankResponse,
    PlayerResponse,
    PlayRequest,
    RewardListResponse,
    RewardResponse,
    SessionCreate,
    SessionResponse,
    SettlementRecipientResponse,
    SettlementResponse,
)
from app.schemas import (
    RewardBand as RewardBandResponse,
)

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_database_session)]


@router.post("/players", response_model=PlayerResponse, status_code=status.HTTP_201_CREATED)
async def create_player(body: PlayerCreate, session: Session) -> PlayerResponse:
    return PlayerResponse.model_validate(await services.create_player(session, body.display_name))


@router.get("/players/{player_id}", response_model=PlayerResponse)
async def get_player(
    player_id: UUID,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> PlayerResponse:
    return PlayerResponse.model_validate(
        await services.retrieve_player(session, player_id, owner_id)
    )


@router.get("/games", response_model=GameListResponse)
async def list_games(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> GameListResponse:
    games = await services.catalogue(session, limit, offset)
    return GameListResponse(
        items=[GameResponse.model_validate(game) for game in games], limit=limit, offset=offset
    )


@router.get("/games/{game_key}/config", response_model=GameConfigResponse)
async def get_active_config(game_key: str, session: Session) -> GameConfigResponse:
    return GameConfigResponse.model_validate(await services.active_config(session, game_key))


@router.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: SessionCreate,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> SessionResponse:
    game_session = await services.create_session(
        session,
        request_id=body.request_id,
        player_id=body.player_id,
        owner_id=owner_id,
        game_key=body.game_key,
    )
    return SessionResponse.model_validate(game_session)


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: UUID,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> SessionResponse:
    return SessionResponse.model_validate(
        await services.retrieve_session(session, session_id, owner_id)
    )


@router.delete("/sessions/{session_id}", response_model=SessionResponse)
async def cancel_session(
    session_id: UUID,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> SessionResponse:
    return SessionResponse.model_validate(
        await services.cancel_session(session, session_id, owner_id)
    )


@router.post("/sessions/{session_id}/play", response_model=OutcomeResponse)
async def play_session(
    session_id: UUID,
    body: PlayRequest,
    session: Session,
    request: Request,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> OutcomeResponse:
    provider: OutcomeProvider = request.app.state.outcome_provider
    outcome = await services.play_session(
        session,
        session_id=session_id,
        owner_id=owner_id,
        choice=body.choice,
        actions=body.actions,
        provider=provider,
    )
    return OutcomeResponse.model_validate(outcome)


@router.post("/sessions/{session_id}/claim", response_model=RewardResponse)
async def claim_session_reward(
    session_id: UUID,
    body: ClaimRequest,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> RewardResponse:
    reward = await services.claim_session_reward(session, session_id=session_id, owner_id=owner_id)
    del body
    return RewardResponse.model_validate(reward)


@router.get("/players/{player_id}/rewards", response_model=RewardListResponse)
async def list_player_rewards(
    player_id: UUID,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RewardListResponse:
    rewards = await services.player_rewards(
        session, player_id=player_id, owner_id=owner_id, limit=limit, offset=offset
    )
    return RewardListResponse(
        items=[RewardResponse.model_validate(reward) for reward in rewards],
        limit=limit,
        offset=offset,
    )


@router.get("/audit/outcomes/{outcome_id}", response_model=OutcomeAuditResponse)
async def get_outcome_audit(
    outcome_id: UUID,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> OutcomeAuditResponse:
    outcome, evidence = await services.retrieve_outcome_audit(session, outcome_id, owner_id)
    return OutcomeAuditResponse(outcome=OutcomeResponse.model_validate(outcome), evidence=evidence)


@router.get("/analytics/game-summary", response_model=GameSummaryResponse)
async def get_game_summary(
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
    start_at: Annotated[datetime | None, Query()] = None,
    end_at: Annotated[datetime | None, Query()] = None,
    game_key: Annotated[
        str | None, Query(min_length=1, max_length=40, pattern=r"^[a-z0-9_]+$")
    ] = None,
) -> GameSummaryResponse:
    rows = await services.analytics_game_summary(
        session, owner_id=owner_id, start_at=start_at, end_at=end_at, game_key=game_key
    )
    return GameSummaryResponse(
        items=[
            GameSummaryItem(
                game_key=row.game_key,
                plays=row.plays,
                rewards_issued=row.rewards_issued,
                average_reward_value=row.average_reward_value,
            )
            for row in rows
        ],
        start_at=start_at,
        end_at=end_at,
        game_key=game_key,
    )


def _period_start(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise services.InvalidPlayError
    value = value.astimezone(UTC)
    if value.weekday() != 0 or any((value.hour, value.minute, value.second, value.microsecond)):
        raise services.InvalidPlayError
    return value


def _entry(score: object, rank: int) -> LeaderboardEntry:
    from app.models import FinalScore

    if not isinstance(score, FinalScore):
        raise TypeError("score must be a FinalScore")
    return LeaderboardEntry(
        rank=rank,
        score_id=score.id,
        player_id=score.player_id,
        session_id=score.session_id,
        final_score=score.final_score,
        completed_at=score.completed_at,
    )


@router.post(
    "/leaderboards/{game_key}/settle",
    response_model=SettlementResponse,
)
async def settle_leaderboard(
    game_key: str,
    session: Session,
    request: Request,
    period_start: Annotated[datetime, Query()],
    admin_token: Annotated[str | None, Header(alias="X-Settlement-Token")] = None,
) -> SettlementResponse:
    configured = request.app.state.settings.settlement_admin_token
    authorized = (
        configured is not None
        and admin_token is not None
        and secrets.compare_digest(configured.get_secret_value(), admin_token)
    )
    run, recipients = await services.settle_leaderboard(
        session,
        game_key=game_key,
        period_start=_period_start(period_start),
        authorized=authorized,
    )
    if run.completed_at is None:
        raise services.InvalidTransitionError
    return SettlementResponse(
        id=run.id,
        game_key=game_key,
        period_start=run.period_start,
        period_end=run.period_end,
        tier_config_id=run.tier_config_id,
        status=run.status,
        completed_at=run.completed_at,
        recipients=[
            SettlementRecipientResponse.model_validate(recipient) for recipient in recipients
        ],
    )


@router.post("/scores", response_model=FinalScoreResponse, status_code=status.HTTP_201_CREATED)
async def submit_score(
    body: FinalScoreCreate,
    session: Session,
    request: Request,
    response: Response,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> FinalScoreResponse:
    score, created = await services.submit_final_score(
        session, session_id=body.session_id, owner_id=owner_id
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    else:
        await project_final_score(getattr(request.app.state, "redis", None), score, "skill_check")
    return FinalScoreResponse.model_validate(score)


@router.get("/leaderboards/{game_key}", response_model=LeaderboardResponse)
async def get_leaderboard(
    game_key: str,
    session: Session,
    request: Request,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
    period_start: Annotated[datetime, Query()],
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> LeaderboardResponse:
    start = _period_start(period_start)
    game, database_scores = await services.canonical_leaderboard(
        session,
        owner_id=owner_id,
        game_key=game_key,
        period_start=start,
        limit=limit + 1,
        offset=cursor,
    )
    total = await repositories.count_canonical_scores(session, game_id=game.id, period_start=start)
    projected = await projected_page(
        getattr(request.app.state, "redis", None),
        game_key=game_key,
        period_start=start.strftime("%Y%m%dT%H%M%SZ"),
        offset=cursor,
        limit=limit + 1,
        expected_count=total,
    )
    if projected is None:
        items = [
            _entry(score, cursor + index + 1) for index, score in enumerate(database_scores[:limit])
        ]
        has_more = len(database_scores) > limit
        source = "postgresql"
    else:
        items = []
        for member, score_value, rank in projected[:limit]:
            completed_us, session_id, score_id, player_id = parse_leaderboard_member(member)
            items.append(
                LeaderboardEntry(
                    rank=rank,
                    score_id=UUID(score_id),
                    player_id=UUID(player_id),
                    session_id=UUID(session_id),
                    final_score=score_value,
                    completed_at=datetime.fromtimestamp(completed_us / 1_000_000, UTC),
                )
            )
        has_more = len(projected) > limit
        source = "redis"
    return LeaderboardResponse(
        game_key=game_key,
        period_start=start,
        period_end=start + timedelta(days=7),
        source=source,
        items=items,
        next_cursor=str(cursor + limit) if has_more else None,
    )


@router.get("/players/{player_id}/rank", response_model=PlayerRankResponse)
async def get_player_rank(
    player_id: UUID,
    session: Session,
    request: Request,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
    game_key: Annotated[str, Query(min_length=1, max_length=40)],
    period_start: Annotated[datetime, Query()],
) -> PlayerRankResponse:
    start = _period_start(period_start)
    score, rank = await services.canonical_player_rank(
        session,
        player_id=player_id,
        owner_id=owner_id,
        game_key=game_key,
        period_start=start,
    )
    total = await repositories.count_canonical_scores(
        session, game_id=score.game_id, period_start=start
    )
    projected = await projected_player_rank(
        getattr(request.app.state, "redis", None),
        game_key=game_key,
        period_start=start.strftime("%Y%m%dT%H%M%SZ"),
        player_id=str(player_id),
        expected_count=total,
    )
    if projected is None:
        entry = _entry(score, rank)
        source = "postgresql"
    else:
        member, score_value, projected_rank_value = projected
        completed_us, session_id, score_id, projected_player = parse_leaderboard_member(member)
        entry = LeaderboardEntry(
            rank=projected_rank_value,
            score_id=UUID(score_id),
            player_id=UUID(projected_player),
            session_id=UUID(session_id),
            final_score=score_value,
            completed_at=datetime.fromtimestamp(completed_us / 1_000_000, UTC),
        )
        source = "redis"
    return PlayerRankResponse(
        game_key=game_key,
        period_start=start,
        period_end=start + timedelta(days=7),
        source=source,
        entry=entry,
    )


@router.post(
    "/fairness/commit", response_model=FairnessCommitResponse, status_code=status.HTTP_201_CREATED
)
async def commit_fairness(
    body: FairnessCommitRequest,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> FairnessCommitResponse:
    proof = await services.commit_fairness(session, session_id=body.session_id, owner_id=owner_id)
    return FairnessCommitResponse(
        proof_id=proof.id,
        status=proof.status.value,
        protocol_version=proof.protocol_version,
        algorithm=proof.algorithm,
        server_seed_commitment=proof.server_seed_commitment,
        session_id=proof.session_id,
        game_key=proof.game_key,
        config_version_id=proof.config_version_id,
    )


@router.post("/fairness/evaluate", response_model=FairnessEvaluateResponse)
async def evaluate_fairness(
    body: FairnessEvaluateRequest,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> FairnessEvaluateResponse:
    outcome, reward, proof = await services.evaluate_fairness(
        session, proof_id=body.proof_id, owner_id=owner_id, client_seed=body.client_seed
    )
    return FairnessEvaluateResponse(
        proof_id=proof.id,
        status=proof.status.value,
        outcome=OutcomeResponse.model_validate(outcome),
        reward=RewardResponse.model_validate(reward),
    )


def _fairness_proof_response(
    proof: services.RevealedFairnessProof, reward_bands: tuple[FairnessRewardBand, ...]
) -> FairnessProofResponse:
    """Translate service-validated finalized evidence for the HTTP boundary."""

    return FairnessProofResponse(
        proof_id=proof.proof_id,
        outcome_id=proof.outcome_id,
        status=proof.status.value,
        protocol_version=proof.protocol_version,
        algorithm=proof.algorithm,
        server_seed_commitment=proof.server_seed_commitment,
        server_seed_revealed=proof.server_seed_revealed,
        client_seed=proof.client_seed,
        nonce=proof.nonce,
        game_key=proof.game_key,
        session_id=proof.session_id,
        config_version_id=proof.config_version_id,
        mapping_version=proof.mapping_version,
        mapping_digest=proof.mapping_digest,
        reward_bands=[
            RewardBandResponse(key=band.key, weight=band.weight, value=band.value)
            for band in reward_bands
        ],
        raw_random_value=proof.raw_random_value,
        normalized_value=proof.normalized_value,
        derivation_attempt=proof.derivation_attempt,
        reward_key=proof.reward_key,
        reward_value=proof.reward_value,
        revealed_at=proof.revealed_at,
    )


@router.get("/fairness/outcomes/{outcome_id}/proof", response_model=FairnessProofResponse)
async def get_fairness_proof(
    outcome_id: UUID,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> FairnessProofResponse:
    proof, reward_bands = await services.retrieve_fairness_proof(
        session, outcome_id=outcome_id, owner_id=owner_id
    )
    return _fairness_proof_response(proof, reward_bands)


@router.get("/fairness/outcomes/{outcome_id}/verify", response_model=FairnessVerificationResponse)
async def verify_fairness_proof(
    outcome_id: UUID,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> FairnessVerificationResponse:
    _, _, code, verified = await services.verify_fairness_proof(
        session, outcome_id=outcome_id, owner_id=owner_id
    )
    return FairnessVerificationResponse(outcome_id=outcome_id, verified=verified, code=code)
