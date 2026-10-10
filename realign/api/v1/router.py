"""API v1 aggregation."""

from fastapi import APIRouter

from realign.api.v1 import (
    admin,
    cron,
    agents,
    approvals,
    auth,
    billing,
    health,
    integrations,
    org,
    security,
    webhooks,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(agents.router)
api_router.include_router(approvals.router)
api_router.include_router(integrations.router)
api_router.include_router(billing.router)
api_router.include_router(org.router)
api_router.include_router(security.router)
api_router.include_router(admin.router)
api_router.include_router(security.router)
api_router.include_router(webhooks.router)
api_router.include_router(cron.router)

__all__ = ["api_router"]
