import uuid

from sqlmodel import Session

from app.models import Tool, ToolCreate, ToolUpdate, get_datetime_utc


def create_tool(*, session: Session, tool_in: ToolCreate, owner_id: uuid.UUID) -> Tool:
    tool = Tool.model_validate(tool_in, update={"owner_id": owner_id})
    session.add(tool)
    session.commit()
    session.refresh(tool)
    return tool


def update_tool(*, session: Session, db_tool: Tool, tool_in: ToolUpdate) -> Tool:
    db_tool.sqlmodel_update(
        tool_in.model_dump(mode="json", exclude_unset=True),
        update={"updated_at": get_datetime_utc()},
    )
    session.add(db_tool)
    session.commit()
    session.refresh(db_tool)
    return db_tool


def delete_tool(*, session: Session, db_tool: Tool) -> None:
    session.delete(db_tool)
    session.commit()
