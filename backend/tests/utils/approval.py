from sqlmodel import Session

from app.models import ApprovalRequest, Run, RunStatus
from tests.utils.run import create_random_run
from tests.utils.utils import random_lower_string


def create_random_approval(db: Session, run: Run | None = None) -> ApprovalRequest:
    run = run or create_random_run(db)
    run.sqlmodel_update(
        {
            "status": RunStatus.WAITING_APPROVAL,
            "thread_id": f"run-{run.id}-0",
            "checkpoint_id": run.checkpoint_id or random_lower_string(),
        }
    )
    db.add(run)
    request = ApprovalRequest(
        run_id=run.id,
        owner_id=run.owner_id,
        tool_name="calculator",
        tool_call_id=random_lower_string(),
        tool_args={"expression": "2+2"},
        thread_id=run.thread_id,
        checkpoint_id=run.checkpoint_id,
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    return request
