import uuid

from sqlmodel import Session

from app.agent.tools.function import get_function
from app.crud.tool import create_tool
from app.models import Tool, ToolCreate, ToolType
from tests.utils.user import create_random_user
from tests.utils.utils import random_lower_string


def create_random_tool(db: Session, owner_id: uuid.UUID | None = None) -> Tool:
    fn = get_function("calculator")
    assert fn is not None
    return create_tool(
        session=db,
        owner_id=owner_id or create_random_user(db).id,
        tool_in=ToolCreate(
            name=random_lower_string(),
            description="Calculate arithmetic",
            tool_type=ToolType.FUNCTION,
            config={"function_name": "calculator"},
            parameters_schema=fn.args_model.model_json_schema(),
        ),
    )
