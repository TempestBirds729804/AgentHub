import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session

from app import crud
from app.core.config import settings
from app.models import AgentCreate, AgentVersion, UserCreate
from tests.utils.agent import create_random_agent, create_random_agent_version
from tests.utils.user import create_random_user, user_authentication_headers
from tests.utils.utils import random_email, random_lower_string


def _create_user_headers(
    *, client: TestClient, db: Session
) -> tuple[uuid.UUID, dict[str, str]]:
    email = random_email()
    password = random_lower_string()
    user = crud.create_user(
        session=db, user_create=UserCreate(email=email, password=password)
    )
    return user.id, user_authentication_headers(
        client=client, email=email, password=password
    )


def test_create_agent_as_normal_user(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    data = {"name": "Normal Agent", "llm_model": "gpt-4o-mini"}
    response = client.post(
        f"{settings.API_V1_STR}/agents/",
        headers=normal_user_token_headers,
        json=data,
    )

    assert response.status_code == 200
    content = response.json()
    assert content["name"] == data["name"]
    assert content["llm_model"] == data["llm_model"]
    assert content["owner_id"]


def test_create_agent_as_superuser(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    response = client.post(
        f"{settings.API_V1_STR}/agents/",
        headers=superuser_token_headers,
        json={"name": "Admin Agent", "llm_model": "gpt-4o-mini"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Admin Agent"


def test_read_own_agent(client: TestClient, db: Session) -> None:
    owner_id, headers = _create_user_headers(client=client, db=db)
    agent = create_random_agent(db, owner_id=owner_id)

    response = client.get(f"{settings.API_V1_STR}/agents/{agent.id}", headers=headers)

    assert response.status_code == 200
    assert response.json()["id"] == str(agent.id)


def test_read_agent_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    response = client.get(
        f"{settings.API_V1_STR}/agents/{uuid.uuid4()}",
        headers=superuser_token_headers,
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Agent not found"


def test_read_other_users_agent_forbidden(
    client: TestClient, normal_user_token_headers: dict[str, str], db: Session
) -> None:
    agent = create_random_agent(db)

    response = client.get(
        f"{settings.API_V1_STR}/agents/{agent.id}",
        headers=normal_user_token_headers,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Not enough permissions"


def test_list_agents_pagination(client: TestClient, db: Session) -> None:
    owner_id, headers = _create_user_headers(client=client, db=db)
    for _ in range(3):
        create_random_agent(db, owner_id=owner_id)

    response = client.get(
        f"{settings.API_V1_STR}/agents/?skip=0&limit=2", headers=headers
    )

    assert response.status_code == 200
    content = response.json()
    assert len(content["data"]) == 2
    assert content["count"] == 3


def test_list_agents_isolated_by_owner(client: TestClient, db: Session) -> None:
    owner_id, headers = _create_user_headers(client=client, db=db)
    own_agent = create_random_agent(db, owner_id=owner_id)
    other_agent = create_random_agent(db)

    response = client.get(f"{settings.API_V1_STR}/agents/", headers=headers)

    assert response.status_code == 200
    ids = {agent["id"] for agent in response.json()["data"]}
    assert str(own_agent.id) in ids
    assert str(other_agent.id) not in ids


def test_superuser_lists_all_agents(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    first = create_random_agent(db)
    second = create_random_agent(db)

    response = client.get(
        f"{settings.API_V1_STR}/agents/", headers=superuser_token_headers
    )

    assert response.status_code == 200
    ids = {agent["id"] for agent in response.json()["data"]}
    assert str(first.id) in ids
    assert str(second.id) in ids


def test_update_agent_partially(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    agent = create_random_agent(db)
    original_model = agent.llm_model

    response = client.patch(
        f"{settings.API_V1_STR}/agents/{agent.id}",
        headers=superuser_token_headers,
        json={"description": "Updated through API"},
    )

    assert response.status_code == 200
    content = response.json()
    assert content["description"] == "Updated through API"
    assert content["llm_model"] == original_model


def test_delete_agent_and_versions(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    agent = create_random_agent(db)
    version = create_random_agent_version(db, agent)
    version_id = version.id

    response = client.delete(
        f"{settings.API_V1_STR}/agents/{agent.id}",
        headers=superuser_token_headers,
    )

    assert response.status_code == 200
    get_response = client.get(
        f"{settings.API_V1_STR}/agents/{agent.id}",
        headers=superuser_token_headers,
    )
    assert get_response.status_code == 404
    db.expire_all()
    assert db.get(AgentVersion, version_id) is None


def test_publish_agent_versions(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    agent = create_random_agent(db)

    first = client.post(
        f"{settings.API_V1_STR}/agents/{agent.id}/versions",
        headers=superuser_token_headers,
        json={"changelog": "Initial"},
    )
    second = client.post(
        f"{settings.API_V1_STR}/agents/{agent.id}/versions",
        headers=superuser_token_headers,
        json={"changelog": "Second"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["version_number"] == 1
    assert second.json()["version_number"] == 2


def test_publish_agent_without_model_fails(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    owner = create_random_user(db)
    agent = crud.create_agent(
        session=db,
        agent_in=AgentCreate(name="Draft Agent"),
        owner_id=owner.id,
    )

    response = client.post(
        f"{settings.API_V1_STR}/agents/{agent.id}/versions",
        headers=superuser_token_headers,
        json={},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Agent must have an LLM model configured to publish"
    )


def test_version_list_is_newest_first(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    agent = create_random_agent(db)
    create_random_agent_version(db, agent)
    crud.publish_agent_version(session=db, db_agent=agent, changelog="newest")

    response = client.get(
        f"{settings.API_V1_STR}/agents/{agent.id}/versions",
        headers=superuser_token_headers,
    )

    assert response.status_code == 200
    content = response.json()
    assert content["count"] == 2
    assert [version["version_number"] for version in content["data"]] == [2, 1]


def test_read_version_detail_and_latest_number(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    agent = create_random_agent(db)
    version = create_random_agent_version(db, agent)

    response = client.get(
        f"{settings.API_V1_STR}/agents/{agent.id}/versions/{version.id}",
        headers=superuser_token_headers,
    )
    agent_response = client.get(
        f"{settings.API_V1_STR}/agents/{agent.id}",
        headers=superuser_token_headers,
    )

    assert response.status_code == 200
    assert response.json()["snapshot"]["system_prompt"] == agent.system_prompt
    assert agent_response.status_code == 200
    assert agent_response.json()["latest_version_number"] == 1
