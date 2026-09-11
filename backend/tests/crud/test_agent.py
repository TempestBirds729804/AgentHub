from sqlmodel import Session

from app import crud
from app.models import AgentCreate, AgentUpdate
from tests.utils.agent import create_random_agent
from tests.utils.user import create_random_user


def test_create_agent(db: Session) -> None:
    owner = create_random_user(db)
    agent_in = AgentCreate(
        name="Support Agent",
        description="Answers support questions",
        system_prompt="Be concise.",
        llm_model="gpt-4o-mini",
    )
    agent = crud.create_agent(session=db, agent_in=agent_in, owner_id=owner.id)

    assert agent.name == agent_in.name
    assert agent.description == agent_in.description
    assert agent.owner_id == owner.id


def test_update_agent_partially(db: Session) -> None:
    agent = create_random_agent(db)
    original_model = agent.llm_model
    original_prompt = agent.system_prompt

    updated = crud.update_agent(
        session=db,
        db_agent=agent,
        agent_in=AgentUpdate(description="Updated description"),
    )

    assert updated.description == "Updated description"
    assert updated.llm_model == original_model
    assert updated.system_prompt == original_prompt


def test_publish_agent_versions_and_snapshot(db: Session) -> None:
    agent = create_random_agent(db)
    first = crud.publish_agent_version(
        session=db, db_agent=agent, changelog="Initial version"
    )
    second = crud.publish_agent_version(
        session=db, db_agent=agent, changelog="Second version"
    )

    assert first.version_number == 1
    assert second.version_number == 2
    assert first.snapshot["name"] == agent.name
    assert first.snapshot["description"] == agent.description
    assert first.snapshot["system_prompt"] == agent.system_prompt
    assert first.snapshot["llm_model"] == agent.llm_model
    assert first.snapshot["llm_settings"] == agent.llm_settings
    assert first.snapshot["max_iterations"] == agent.max_iterations
    assert first.snapshot["timeout_seconds"] == agent.timeout_seconds


def test_published_snapshot_is_immutable(db: Session) -> None:
    agent = create_random_agent(db)
    original_prompt = agent.system_prompt
    version = crud.publish_agent_version(session=db, db_agent=agent)

    crud.update_agent(
        session=db,
        db_agent=agent,
        agent_in=AgentUpdate(system_prompt="A changed prompt."),
    )
    db.refresh(version)

    assert agent.system_prompt == "A changed prompt."
    assert version.snapshot["system_prompt"] == original_prompt
