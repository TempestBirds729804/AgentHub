import uuid
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from jsonschema import Draft202012Validator, SchemaError
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, func, select

from app.agent.tools.adapter import execute_tool
from app.agent.tools.function import get_function, list_functions
from app.agent.tools.registry import get_executor
from app.api.deps import AsyncSessionDep, CurrentUser, SessionDep
from app.crud import tool as crud
from app.models import (
    AgentToolBinding,
    BuiltinFunctionPublic,
    BuiltinFunctionsPublic,
    Message,
    Tool,
    ToolCreate,
    ToolPublic,
    ToolsPublic,
    ToolTestRequest,
    ToolTestResult,
    ToolType,
    ToolUpdate,
)

router = APIRouter(prefix="/tools", tags=["tools"])


def _get_owned_tool(
    *, session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Tool:
    tool = session.get(Tool, id)
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    if not current_user.is_superuser and tool.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return tool


def _validate_tool(tool_in: ToolCreate) -> ToolCreate:
    try:
        get_executor(tool_in.tool_type).validate_config(tool_in.config)
        if tool_in.tool_type == ToolType.FUNCTION:
            fn = get_function(tool_in.config["function_name"])
            assert fn is not None
            tool_in.parameters_schema = fn.args_model.model_json_schema()
        Draft202012Validator.check_schema(tool_in.parameters_schema)
        if tool_in.parameters_schema.get("type") != "object":
            raise ValueError("parameters_schema must describe an object")
    except (ValueError, SchemaError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:1000])
    return tool_in


@router.get("/builtin-functions", response_model=BuiltinFunctionsPublic)
def read_builtin_functions(current_user: CurrentUser) -> Any:
    """List available built-in functions."""
    del current_user
    data = [
        BuiltinFunctionPublic(
            name=fn.name,
            description=fn.description,
            parameters_schema=fn.args_model.model_json_schema(),
        )
        for fn in list_functions()
    ]
    return BuiltinFunctionsPublic(data=data, count=len(data))


@router.get("/", response_model=ToolsPublic)
def read_tools(
    session: SessionDep,
    current_user: CurrentUser,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    tool_type: ToolType | None = None,
    is_active: bool | None = None,
) -> Any:
    """Retrieve tools filtered by type and active status."""
    statement = select(Tool)
    count_statement = select(func.count()).select_from(Tool)
    filters = []
    if not current_user.is_superuser:
        filters.append(Tool.owner_id == current_user.id)
    if tool_type is not None:
        filters.append(Tool.tool_type == tool_type)
    if is_active is not None:
        filters.append(Tool.is_active == is_active)
    data = session.exec(
        statement.where(*filters)
        .order_by(col(Tool.created_at).desc(), col(Tool.id))
        .offset(skip)
        .limit(limit)
    ).all()
    return ToolsPublic(
        data=[ToolPublic.model_validate(tool) for tool in data],
        count=session.exec(count_statement.where(*filters)).one(),
    )


@router.post("/", response_model=ToolPublic)
def create_tool(
    *, session: SessionDep, current_user: CurrentUser, tool_in: ToolCreate
) -> Any:
    """Create a validated tool definition."""
    tool_in = _validate_tool(tool_in)
    try:
        return crud.create_tool(
            session=session, tool_in=tool_in, owner_id=current_user.id
        )
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="A tool with this name already exists"
        )


@router.get("/{id}", response_model=ToolPublic)
def read_tool(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """Get a tool by ID."""
    return _get_owned_tool(session=session, current_user=current_user, id=id)


@router.patch("/{id}", response_model=ToolPublic)
def update_tool(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    tool_in: ToolUpdate,
) -> Any:
    """Update and revalidate a tool definition."""
    tool = _get_owned_tool(session=session, current_user=current_user, id=id)
    changes = tool_in.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise HTTPException(status_code=422, detail="Tool fields cannot be null")
    validated = _validate_tool(
        ToolCreate.model_validate(
            {**ToolCreate.model_validate(tool).model_dump(), **changes}
        )
    )
    changes["parameters_schema"] = validated.parameters_schema
    try:
        return crud.update_tool(
            session=session, db_tool=tool, tool_in=ToolUpdate.model_validate(changes)
        )
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="A tool with this name already exists"
        )


@router.delete("/{id}", response_model=Message)
def delete_tool(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """Delete a tool unless a published version references it."""
    tool = _get_owned_tool(session=session, current_user=current_user, id=id)
    # Share the row lock with publication validation before checking bindings.
    session.refresh(tool, with_for_update=True)
    bound = session.exec(
        select(func.count())
        .select_from(AgentToolBinding)
        .where(AgentToolBinding.tool_id == id)
    ).one()
    if bound:
        raise HTTPException(
            status_code=409,
            detail="Tool is bound to published agent versions. Deactivate it instead.",
        )
    crud.delete_tool(session=session, db_tool=tool)
    return Message(message="Tool deleted successfully")


@router.post("/{id}/test", response_model=ToolTestResult)
async def test_tool(
    *,
    session: AsyncSessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    body: ToolTestRequest,
) -> Any:
    """Test a tool with validated arguments."""
    tool = await session.get(Tool, id)
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    if not current_user.is_superuser and tool.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return ToolTestResult(**asdict(await execute_tool(tool, arguments=body.arguments)))
