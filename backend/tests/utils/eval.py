from sqlmodel import Session

from app.models import EvalCase, EvalRun
from tests.utils.agent import create_random_agent, create_random_agent_version


def create_eval_batch(db: Session, count: int = 3) -> EvalRun:
    agent = create_random_agent(db)
    version = create_random_agent_version(db, agent)
    cases = [
        EvalCase(
            agent_id=agent.id,
            name=f"case-{i}",
            input={"message": f"input-{i}"},
            expected_output="fake reply",
        )
        for i in range(count)
    ]
    db.add_all(cases)
    db.commit()
    for case in cases:
        db.refresh(case)
    batch = EvalRun(
        owner_id=agent.owner_id,
        agent_version_id=version.id,
        case_count=count,
        case_snapshots=[case.model_dump(mode="json") for case in cases],
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch
