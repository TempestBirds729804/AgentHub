from sqlmodel import SQLModel

from app.models.agent import (
    Agent,
    AgentBase,
    AgentCreate,
    AgentPublic,
    AgentsPublic,
    AgentUpdate,
    AgentVersion,
    AgentVersionCreate,
    AgentVersionPublic,
    AgentVersionsPublic,
)
from app.models.base import (
    Message,
    NewPassword,
    Token,
    TokenPayload,
    get_datetime_utc,
)
from app.models.user import (
    UpdatePassword,
    User,
    UserBase,
    UserCreate,
    UserPublic,
    UserRegister,
    UsersPublic,
    UserUpdate,
    UserUpdateMe,
)

__all__ = [
    "SQLModel",
    "Agent",
    "AgentBase",
    "AgentCreate",
    "AgentPublic",
    "AgentsPublic",
    "AgentUpdate",
    "AgentVersion",
    "AgentVersionCreate",
    "AgentVersionPublic",
    "AgentVersionsPublic",
    "Message",
    "NewPassword",
    "Token",
    "TokenPayload",
    "get_datetime_utc",
    "UpdatePassword",
    "User",
    "UserBase",
    "UserCreate",
    "UserPublic",
    "UserRegister",
    "UsersPublic",
    "UserUpdate",
    "UserUpdateMe",
]

Agent.model_rebuild()
AgentVersion.model_rebuild()
User.model_rebuild()
