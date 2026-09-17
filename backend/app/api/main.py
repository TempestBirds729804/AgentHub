from fastapi import APIRouter

from app.api.routes import (
    agents,
    conversations,
    login,
    private,
    runs,
    tools,
    users,
    utils,
)
from app.core.config import settings

api_router = APIRouter()
api_router.include_router(tools.router)
api_router.include_router(login.router)
api_router.include_router(users.router)
api_router.include_router(utils.router)
api_router.include_router(agents.router)
api_router.include_router(runs.router)
api_router.include_router(conversations.router)


if settings.FASTAPI_ENV == "development":
    api_router.include_router(private.router)
