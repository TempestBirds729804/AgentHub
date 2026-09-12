from sqlmodel import Session

from app.models import Run, RunEvent, RunEventType
from tests.utils.agent import create_random_agent, create_random_agent_version


def create_random_run(db: Session) -> Run:
    agent = create_random_agent(db)
    version = create_random_agent_version(db, agent)
    run = Run(
        owner_id=agent.owner_id, agent_version_id=version.id, input={"message": "Hello"}
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def create_random_run_event(db: Session, run: Run) -> RunEvent:
    event = RunEvent(run_id=run.id, seq=0, event_type=RunEventType.RUN_STARTED)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event
