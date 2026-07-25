"""Composition router for public domain capabilities."""

from fastapi import APIRouter

from app.api.analytics import router as analytics_router
from app.api.fairness import router as fairness_router
from app.api.gameplay import router as gameplay_router
from app.api.leaderboards import router as leaderboards_router
from app.api.players import router as players_router
from app.api.rewards import router as rewards_router
from app.api.sessions import router as sessions_router

router = APIRouter()
router.include_router(players_router)
router.include_router(sessions_router)
router.include_router(gameplay_router)
router.include_router(rewards_router)
router.include_router(analytics_router)
router.include_router(leaderboards_router)
router.include_router(fairness_router)
