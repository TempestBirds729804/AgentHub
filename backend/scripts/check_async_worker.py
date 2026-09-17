"""Manual phase 06 acceptance against local Compose (uses real model and worker).

Run from backend: uv run python scripts/check_async_worker.py
Only the Run/Agent/Tools created here are mutated. Requires no other active Runs.
Stops/restarts the worker for crash recovery and Redis for queue-outage acceptance.
Evidence remains in frontend/test-results/phase06-manual; test records are retained.
"""

import json
import logging
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "frontend/test-results/phase06-manual"
logger = logging.getLogger(__name__)


def compose(*args: str) -> str:
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout + result.stderr


def main() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with httpx.Client(
        base_url="http://127.0.0.1:8000/api/v1", timeout=20, trust_env=False
    ) as api:
        login = api.post(
            "/login/access-token",
            data={
                "username": settings.FIRST_SUPERUSER,
                "password": settings.FIRST_SUPERUSER_PASSWORD,
            },
        )
        login.raise_for_status()
        api.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        def call(method: str, path: str, **kwargs: Any) -> Any:
            response = api.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        for status in ("queued", "running"):
            assert call("GET", f"/runs/?status={status}")["count"] == 0, (
                "Existing active Runs: do not interrupt shared services"
            )
        suffix = uuid.uuid4().hex[:8]
        tool_ids = []
        for name, field in (("search_dev_docs", "query"), ("read_dev_doc", "doc_id")):
            tool = call(
                "POST",
                "/tools/",
                json={
                    "name": f"{name}_{suffix}",
                    "description": name,
                    "tool_type": "mcp",
                    "config": {
                        "transport": "streamable_http",
                        "url": "http://mcp-docs:3001/mcp",
                        "tool_name": name,
                    },
                    "parameters_schema": {
                        "type": "object",
                        "properties": {field: {"type": "string"}},
                        "required": [field],
                    },
                },
            )
            tool_ids.append(tool["id"])
        agent = call(
            "POST",
            "/agents/",
            json={
                "name": f"Phase06 crash recovery {suffix}",
                "llm_model": settings.LLM_MODEL,
                "tool_ids": tool_ids,
                "timeout_seconds": 120,
                "system_prompt": f"First call search_dev_docs_{suffix} with query Checkpoint. After receiving the result, call read_dev_doc_{suffix} with doc_id 06-async-worker. Always make these two calls sequentially, never in parallel. Then explain recovery in Chinese and cite the document ID.",
            },
        )
        version = call("POST", f"/agents/{agent['id']}/versions", json={})

        def submit(
            message: str = "Search Checkpoint, then read phase06 and explain.",
        ) -> Any:
            return call(
                "POST",
                "/runs/async",
                json={
                    "agent_version_id": version["id"],
                    "input": {"message": message},
                    "trigger": "api",
                },
            )

        def wait_for(run_id: str, predicate: Any, seconds: int = 90) -> tuple[Any, Any]:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                run = call("GET", f"/runs/{run_id}")
                events = call("GET", f"/runs/{run_id}/events")["data"]
                if predicate(run, events):
                    return run, events
                time.sleep(0.1)
            raise TimeoutError(f"Run {run_id} did not reach the expected state")

        run = submit()
        run_id = run["id"]
        logger.info("Crash recovery Run %s", run_id)
        before, events = wait_for(
            run_id,
            lambda r, es: (
                r["status"] == "running"
                and r["checkpoint_id"]
                and any(e["event_type"] == "tool_result" for e in es)
            ),
        )
        try:
            compose("kill", "worker")
            # A hard kill leaves status RUNNING. Cancel the orphan before explicit
            # retry; /retry deliberately only accepts FAILED or CANCELLED.
            orphan = call("POST", f"/runs/{run_id}/cancel")
            retried = call("POST", f"/runs/{run_id}/retry")
            # The worker may commit another checkpoint between polling and kill.
            assert retried["checkpoint_id"] == orphan["checkpoint_id"]
        finally:
            compose("up", "-d", "--no-deps", "worker")
        resumed, final_events = wait_for(
            run_id, lambda r, _: r["status"] in {"succeeded", "failed"}
        )
        assert resumed["status"] == "succeeded", resumed["error"]
        assert any(e["payload"].get("resumed") for e in final_events)
        assert (
            sum(
                e["event_type"] == "tool_result" and e["payload"].get("ok")
                for e in final_events
            )
            >= 2
        )
        logs = compose("logs", "--no-color", "--tail", "100", "worker")
        assert f"Resuming run {run_id} from checkpoint" in logs
        (EVIDENCE / "worker-recovery.log").write_text(logs, encoding="utf-8")
        (EVIDENCE / "crash-recovery.json").write_text(
            json.dumps(
                {
                    "before": before,
                    "before_events": events,
                    "run": resumed,
                    "events": final_events,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info(
            "Recovered %s with %d tool results",
            run_id,
            sum(e["event_type"] == "tool_result" for e in final_events),
        )

        cancelled = submit()
        wait_for(cancelled["id"], lambda r, _: r["status"] == "running")
        assert call("POST", f"/runs/{cancelled['id']}/cancel")["status"] == "cancelled"
        # Wait for worker cleanup rather than relying on the early API state.
        time.sleep(10)
        assert call("GET", f"/runs/{cancelled['id']}")["status"] == "cancelled"
        logger.info("Running cancellation retained for %s", cancelled["id"])

        try:
            compose("stop", "worker")
            queued = [submit("Concurrency limit probe") for _ in range(3)]
            fourth = api.post(
                "/runs/async",
                json={
                    "agent_version_id": version["id"],
                    "input": {"message": "fourth"},
                },
            )
            assert fourth.status_code == 429, fourth.text
            for item in queued:
                assert (
                    call("POST", f"/runs/{item['id']}/cancel")["status"] == "cancelled"
                )
            logger.info("Concurrent limit: fourth submission returned 429")
            compose("stop", "redis")
            unavailable = api.post(
                "/runs/async",
                json={
                    "agent_version_id": version["id"],
                    "input": {"message": "Redis outage probe"},
                },
            )
            assert unavailable.status_code == 503, unavailable.text
            latest = call("GET", f"/runs/?agent_version_id={version['id']}&limit=1")[
                "data"
            ][0]
            assert latest["status"] == "failed" and "enqueue" in latest["error"]
            (EVIDENCE / "queue-outage.json").write_text(
                json.dumps(latest, indent=2), encoding="utf-8"
            )
            logger.info("Stopped Redis: 503 and durable failed Run %s", latest["id"])
        finally:
            compose("up", "-d", "redis", "worker")
        (EVIDENCE / "resources.json").write_text(
            json.dumps(
                {
                    "agent_id": agent["id"],
                    "tool_ids": tool_ids,
                    "recovered_run_id": run_id,
                    "cancelled_run_id": cancelled["id"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    main()
