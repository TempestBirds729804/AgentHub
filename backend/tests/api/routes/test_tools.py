import socket
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models import AgentToolBinding, User
from tests.utils.tool import create_random_tool
from tests.utils.utils import random_lower_string

URL = f"{settings.API_V1_STR}/tools/"


@pytest.mark.parametrize(
    "kind,config",
    [
        ("function", {"function_name": "calculator"}),
        ("http", {"url": "https://example.com"}),
        (
            "mcp",
            {
                "transport": "sse",
                "url": "https://example.com/sse",
                "tool_name": "search",
            },
        ),
    ],
)
def test_create_types(
    kind: str,
    config: dict[str, str],
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_dns = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: (
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
            if host == "example.com"
            else original_dns(host, *args, **kwargs)
        ),
    )
    body = {
        "name": random_lower_string(),
        "description": "test tool",
        "tool_type": kind,
        "config": config,
        "parameters_schema": {"type": "object", "properties": {}},
    }
    response = client.post(URL, headers=normal_user_token_headers, json=body)
    assert response.status_code == 200, response.text
    tool = response.json()
    if kind == "function":
        assert "expression" in tool["parameters_schema"]["properties"]
        result = client.post(
            URL + tool["id"] + "/test",
            headers=normal_user_token_headers,
            json={"arguments": {"expression": "2*21"}},
        ).json()
        assert result["ok"] and float(result["content"]) == 42
        invalid = client.post(
            URL + tool["id"] + "/test",
            headers=normal_user_token_headers,
            json={"arguments": {}},
        ).json()
        assert not invalid["ok"] and "expression" in invalid["error"]
    assert (
        client.post(URL, headers=normal_user_token_headers, json=body).status_code
        == 409
    )
    assert (
        client.patch(
            URL + tool["id"],
            headers=normal_user_token_headers,
            json={"is_active": False},
        ).json()["is_active"]
        is False
    )
    assert (
        client.delete(URL + tool["id"], headers=normal_user_token_headers).status_code
        == 200
    )


def test_invalid_and_permissions(
    client: TestClient, db: Session, normal_user_token_headers: dict[str, str]
) -> None:
    body = {
        "name": "safe-name",
        "description": "test",
        "tool_type": "function",
        "config": {"function_name": "unknown"},
    }
    response = client.post(URL, headers=normal_user_token_headers, json=body)
    assert response.status_code == 400 and "Available" in response.text
    body.update(tool_type="http", config={"url": "http://169.254.169.254/"})
    assert (
        client.post(URL, headers=normal_user_token_headers, json=body).status_code
        == 400
    )
    body.update(
        name="bad name", tool_type="function", config={"function_name": "calculator"}
    )
    assert (
        client.post(URL, headers=normal_user_token_headers, json=body).status_code
        == 422
    )
    other = create_random_tool(db)
    for method in (client.get, client.delete):
        assert (
            method(URL + str(other.id), headers=normal_user_token_headers).status_code
            == 403
        )
    assert (
        client.post(
            URL + str(other.id) + "/test",
            headers=normal_user_token_headers,
            json={"arguments": {}},
        ).status_code
        == 403
    )
    listing = client.get(URL, headers=normal_user_token_headers).json()
    assert str(other.id) not in [tool["id"] for tool in listing["data"]]
    builtin = client.get(URL + "builtin-functions", headers=normal_user_token_headers)
    assert builtin.status_code == 200 and builtin.json()["count"] >= 2


def test_binding_publication(
    client: TestClient, db: Session, normal_user_token_headers: dict[str, str]
) -> None:
    user = db.exec(select(User).where(User.email == settings.EMAIL_TEST_USER)).one()
    tool = create_random_tool(db, user.id)
    other = create_random_tool(db)
    agents = f"{settings.API_V1_STR}/agents/"
    body = {
        "name": random_lower_string(),
        "llm_model": "fake",
        "tool_ids": [str(tool.id)],
    }
    response = client.post(agents, headers=normal_user_token_headers, json=body)
    assert response.status_code == 200, response.text
    agent_id = response.json()["id"]
    for tool_id in (str(other.id), str(uuid.uuid4())):
        assert (
            client.patch(
                agents + agent_id,
                headers=normal_user_token_headers,
                json={"tool_ids": [tool_id]},
            ).status_code
            == 400
        )
    assert (
        client.patch(
            agents + agent_id,
            headers=normal_user_token_headers,
            json={"tool_ids": [str(tool.id), str(tool.id)]},
        ).status_code
        == 200
    )
    version = client.post(
        agents + agent_id + "/versions", headers=normal_user_token_headers, json={}
    ).json()
    assert version["tool_names"] == [tool.name]
    assert (
        len(
            db.exec(
                select(AgentToolBinding).where(
                    AgentToolBinding.agent_version_id == uuid.UUID(version["id"])
                )
            ).all()
        )
        == 1
    )
    assert (
        client.delete(URL + str(tool.id), headers=normal_user_token_headers).status_code
        == 409
    )
    assert (
        client.patch(
            URL + str(tool.id),
            headers=normal_user_token_headers,
            json={"name": "bad name"},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            URL + str(tool.id), headers=normal_user_token_headers, json={"name": None}
        ).status_code
        == 422
    )
    client.patch(
        agents + agent_id, headers=normal_user_token_headers, json={"tool_ids": []}
    )
    assert client.get(
        agents + agent_id + "/versions/" + version["id"],
        headers=normal_user_token_headers,
    ).json()["tool_names"] == [tool.name]
