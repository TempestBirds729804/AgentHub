# 02 — Agent 管理与版本

## 前置依赖

阶段 [01-foundation.md](01-foundation.md) 的验收清单全部通过。特别确认：

- `app/models/` 和 `app/crud/` 已经是包结构
- `uv run alembic check` 输出 `No new upgrade operations detected.`

## 本阶段目标

建立平台的第一个业务实体：

- `Agent`（可编辑的当前配置）和 `AgentVersion`（发布快照）两张表
- 完整的后端 CRUD 与发布接口
- 前端 Agent 列表页、详情编辑页、版本历史
- 移除 Item 的全部痕迹

## 本阶段不做什么

不接模型，不跑 LangGraph，不写执行逻辑。这一阶段结束时 Agent 只是一份配置数据。

---

## 两个必须先定的命名决策

写代码前先理解这两条，否则会踩坑并且各处命名不一致。

### 1. 不要用 `model_` 前缀的字段名

Pydantic v2 保留了 `model_` 命名空间。字段叫 `model_name` 或 `model_settings` 会触发告警，某些情况下还会与 Pydantic 自身的方法冲突。

**统一用 `llm_` 前缀**：`llm_model`、`llm_settings`、`llm_temperature`。这个约定在后续所有阶段都适用（Run、EvalRun 的同类字段也一样）。

### 2. 多词类名要显式写蛇形表名

SQLModel 默认表名是类名的全小写，`AgentVersion` 会变成 `agentversion`，可读性差且和 Postgres 惯例不符。**凡是多词类名都显式指定**：

```python
class AgentVersion(SQLModel, table=True):
    __tablename__ = "agent_version"
```

单词类名（`Agent`、`Run`、`Tool`）不用写，默认值就对。

---

## 任务 1：模型定义

新建 `backend/app/models/agent.py`：

```python
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

from app.models.base import get_datetime_utc

if TYPE_CHECKING:
    from app.models.user import User


# --- Agent ---


class AgentBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    system_prompt: str = Field(default="", max_length=20000)
    llm_model: str = Field(default="", max_length=128)
    llm_settings: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    max_iterations: int = Field(default=10, ge=1, le=50)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    is_active: bool = True


class AgentCreate(AgentBase):
    pass


class AgentUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    system_prompt: str | None = Field(default=None, max_length=20000)
    llm_model: str | None = Field(default=None, max_length=128)
    llm_settings: dict[str, Any] | None = None
    max_iterations: int | None = Field(default=None, ge=1, le=50)
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    is_active: bool | None = None


class Agent(AgentBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE", index=True
    )
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    updated_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )

    owner: "User | None" = Relationship(back_populates="agents")
    versions: list["AgentVersion"] = Relationship(
        back_populates="agent", cascade_delete=True
    )


class AgentPublic(AgentBase):
    id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime | None = None
    updated_at: datetime | None = None
    latest_version_number: int | None = None


class AgentsPublic(SQLModel):
    data: list[AgentPublic]
    count: int


# --- AgentVersion ---


class AgentVersionBase(SQLModel):
    changelog: str | None = Field(default=None, max_length=2000)


class AgentVersionCreate(AgentVersionBase):
    """发布请求体。version_number 和 snapshot 由服务端生成，客户端不能指定。"""


class AgentVersion(AgentVersionBase, table=True):
    __tablename__ = "agent_version"
    __table_args__ = (UniqueConstraint("agent_id", "version_number"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(
        foreign_key="agent.id", nullable=False, ondelete="CASCADE", index=True
    )
    version_number: int = Field(nullable=False)
    snapshot: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    is_draft: bool = False
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )

    agent: Agent | None = Relationship(back_populates="versions")


class AgentVersionPublic(AgentVersionBase):
    id: uuid.UUID
    agent_id: uuid.UUID
    version_number: int
    snapshot: dict[str, Any]
    is_draft: bool
    created_at: datetime | None = None


class AgentVersionsPublic(SQLModel):
    data: list[AgentVersionPublic]
    count: int
```

### 关于 `snapshot` 为什么是 JSONB 而不是逐字段复制

后续阶段会持续给 Agent 加配置字段（工具绑定、知识库绑定、guardrail 规则）。如果 AgentVersion 逐字段复制，每加一个字段都要改两张表两次迁移。用 JSONB 存整份配置，AgentVersion 表结构永远不用动。

代价是快照内容没有数据库级校验，所以**必须定义一个 Pydantic 模型来约束快照结构**，在写入和读取时都过一遍。新建 `backend/app/agent/snapshot.py`：

```python
from typing import Any

from pydantic import BaseModel, Field


class AgentSnapshot(BaseModel):
    """AgentVersion.snapshot 的结构约定。新增配置项时在这里加字段并给默认值。"""

    name: str
    description: str | None = None
    system_prompt: str = ""
    llm_model: str = ""
    llm_settings: dict[str, Any] = Field(default_factory=dict)
    max_iterations: int = 10
    timeout_seconds: int = 300
    # 后续阶段追加：tool_ids（05）、knowledge_base_ids（08）、guardrail（07）
```

**加字段时必须给默认值**，否则读取历史版本的旧快照会校验失败。

### 在 User 上加反向关系

`backend/app/models/user.py` 的 `User` 类里追加：

```python
if TYPE_CHECKING:
    from app.models.agent import Agent

class User(UserBase, table=True):
    # ... 已有字段
    agents: list["Agent"] = Relationship(back_populates="owner", cascade_delete=True)
```

### 更新 `models/__init__.py`

导出全部新符号并追加 rebuild：

```python
from app.models.agent import (
    Agent,
    AgentBase,
    AgentCreate,
    AgentPublic,
    AgentsPublic,
    AgentUpdate,
    AgentVersion,
    AgentVersionCreate,
    AgentVersionPublic,
    AgentVersionsPublic,
)

# ... __all__ 里加上以上全部

Agent.model_rebuild()
AgentVersion.model_rebuild()
User.model_rebuild()
```

漏掉 `__init__.py` 的导出，Alembic autogenerate 就不会看到新表。

---

## 任务 2：生成迁移

```bash
cd backend
uv run alembic revision --autogenerate -m "Add agent and agent version models"
```

**人工检查生成的文件**，确认：

- 建了 `agent` 和 `agent_version` 两张表
- `agent.owner_id` 有 `ondelete="CASCADE"` 的外键
- `agent_version` 上有 `(agent_id, version_number)` 的 UniqueConstraint
- JSONB 列的类型是 `postgresql.JSONB()` 而不是 `sa.JSON()`。如果生成成了 `sa.JSON()`，手工改掉并补上 `from sqlalchemy.dialects import postgresql` 的 import
- `downgrade()` 里的 drop 顺序是 `agent_version` 先、`agent` 后

然后应用并验证往返：

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
uv run alembic check   # 应输出 No new upgrade operations detected.
```

---

## 任务 3：CRUD 层

新建 `backend/app/crud/agent.py`。参照 `app/crud/user.py` 的风格：模块级函数、keyword-only 参数、`add` / `commit` / `refresh`。

```python
import uuid

from sqlmodel import Session, func, select

from app.agent.snapshot import AgentSnapshot
from app.models import Agent, AgentCreate, AgentUpdate, AgentVersion


def create_agent(
    *, session: Session, agent_in: AgentCreate, owner_id: uuid.UUID
) -> Agent:
    db_obj = Agent.model_validate(agent_in, update={"owner_id": owner_id})
    session.add(db_obj)
    session.commit()
    session.refresh(db_obj)
    return db_obj


def update_agent(*, session: Session, db_agent: Agent, agent_in: AgentUpdate) -> Agent:
    agent_data = agent_in.model_dump(exclude_unset=True)
    db_agent.sqlmodel_update(agent_data, update={"updated_at": get_datetime_utc()})
    session.add(db_agent)
    session.commit()
    session.refresh(db_agent)
    return db_agent


def get_agent(*, session: Session, agent_id: uuid.UUID) -> Agent | None:
    return session.get(Agent, agent_id)


def get_latest_version_number(*, session: Session, agent_id: uuid.UUID) -> int:
    statement = select(func.max(AgentVersion.version_number)).where(
        AgentVersion.agent_id == agent_id
    )
    return session.exec(statement).one() or 0


def publish_agent_version(
    *, session: Session, db_agent: Agent, changelog: str | None = None
) -> AgentVersion:
    """把 Agent 当前配置冻结成一个新版本。"""
    snapshot = AgentSnapshot(
        name=db_agent.name,
        description=db_agent.description,
        system_prompt=db_agent.system_prompt,
        llm_model=db_agent.llm_model,
        llm_settings=db_agent.llm_settings,
        max_iterations=db_agent.max_iterations,
        timeout_seconds=db_agent.timeout_seconds,
    )
    next_number = get_latest_version_number(session=session, agent_id=db_agent.id) + 1
    db_version = AgentVersion(
        agent_id=db_agent.id,
        version_number=next_number,
        snapshot=snapshot.model_dump(mode="json"),
        changelog=changelog,
    )
    session.add(db_version)
    session.commit()
    session.refresh(db_version)
    return db_version


def get_agent_version(
    *, session: Session, version_id: uuid.UUID
) -> AgentVersion | None:
    return session.get(AgentVersion, version_id)
```

要点：

- `snapshot.model_dump(mode="json")` 而不是 `model_dump()`。`mode="json"` 会把 datetime、UUID 等转成 JSON 原生类型，否则写 JSONB 时会报序列化错误
- `publish_agent_version` 里的 `version_number` 计算和插入之间有竞态。单用户场景可以接受，但要在函数里加一句注释说明：并发发布依赖 `(agent_id, version_number)` 的唯一约束兜底，冲突时路由层返回 409
- 记得 `from app.models.base import get_datetime_utc`

在 `app/crud/__init__.py` 里导出全部新函数。

---

## 任务 4：路由

新建 `backend/app/api/routes/agents.py`。**完全照抄 `items.py` 的结构**，只是资源换成 Agent。

```python
router = APIRouter(prefix="/agents", tags=["agents"])
```

`tags=["agents"]` 决定前端生成的类名是 `AgentsService`，**定了就不要再改**。

### 接口清单

| 方法 | 路径 | response_model | 说明 |
|------|------|----------------|------|
| GET | `/agents/` | `AgentsPublic` | 列表，`skip`/`limit`，非 superuser 只看自己的 |
| GET | `/agents/{id}` | `AgentPublic` | 详情 |
| POST | `/agents/` | `AgentPublic` | 创建 |
| PATCH | `/agents/{id}` | `AgentPublic` | 更新 |
| DELETE | `/agents/{id}` | `Message` | 删除（级联删版本） |
| POST | `/agents/{id}/versions` | `AgentVersionPublic` | 发布新版本 |
| GET | `/agents/{id}/versions` | `AgentVersionsPublic` | 版本列表，倒序 |
| GET | `/agents/{id}/versions/{version_id}` | `AgentVersionPublic` | 版本详情（含完整快照） |

**用 PATCH 而不是 PUT**，因为 `AgentUpdate` 的所有字段都可选，是部分更新语义。Item 用的是 PUT，那是模板的历史包袱，新资源统一用 PATCH。

### 权限检查抽成辅助函数

每个接口都要做"存在 + 归属"检查，重复五遍很啰嗦。在 `agents.py` 顶部写一个本地辅助函数：

```python
def _get_owned_agent(
    *, session: SessionDep, current_user: CurrentUser, agent_id: uuid.UUID
) -> Agent:
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and agent.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return agent
```

错误文案沿用模板惯例，不要自创。

### `latest_version_number` 的填充

`AgentPublic` 有个 `latest_version_number` 字段，它不是数据库列。列表接口里避免 N+1 查询，用一次聚合查询批量拿：

```python
version_counts = dict(
    session.exec(
        select(AgentVersion.agent_id, func.max(AgentVersion.version_number))
        .where(col(AgentVersion.agent_id).in_([a.id for a in agents]))
        .group_by(col(AgentVersion.agent_id))
    ).all()
)
agents_public = [
    AgentPublic.model_validate(
        a, update={"latest_version_number": version_counts.get(a.id)}
    )
    for a in agents
]
```

### 发布接口的冲突处理

```python
@router.post("/{id}/versions", response_model=AgentVersionPublic)
def publish_version(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    version_in: AgentVersionCreate,
) -> Any:
    """
    Publish a new immutable version of the agent.
    """
    agent = _get_owned_agent(session=session, current_user=current_user, agent_id=id)
    if not agent.llm_model:
        raise HTTPException(
            status_code=400, detail="Agent must have an LLM model configured to publish"
        )
    try:
        return crud.publish_agent_version(
            session=session, db_agent=agent, changelog=version_in.changelog
        )
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="Concurrent publish detected, please retry"
        )
```

`IntegrityError` 从 `sqlalchemy.exc` 引入。

### 版本不可变

**没有更新和删除版本的接口**，这是故意的。版本一旦发布就只读，否则历史运行无法复现。如果用户想改，让他改 Agent 再发新版本。

### 注册路由

`backend/app/api/main.py`：

```python
from app.api.routes import agents, items, login, private, users, utils

api_router.include_router(agents.router)
```

---

## 任务 5：测试基础设施

### 5.1 更新 `conftest.py` 的 teardown

`backend/tests/conftest.py` 的 `db` fixture 目前只删 Item 和 User。**每新增一张表都要在这里加，且子表必须在父表之前**：

```python
@pytest.fixture(scope="session", autouse=True)
def db() -> Generator[Session]:
    with Session(engine) as session:
        init_db(session)
        yield session
        # 顺序很重要：子表先删，父表后删，否则外键约束报错
        for model in (AgentVersion, Agent, Item, User):
            session.execute(delete(model))
        session.commit()
```

改成循环形式，后续阶段往元组前面加就行，不用每次重写一遍。

### 5.2 测试辅助函数

新建 `backend/tests/utils/agent.py`，参照 `tests/utils/item.py`：

```python
from sqlmodel import Session

from app import crud
from app.models import Agent, AgentCreate, AgentVersion
from tests.utils.user import create_random_user
from tests.utils.utils import random_lower_string


def create_random_agent(db: Session, owner_id: uuid.UUID | None = None) -> Agent:
    if owner_id is None:
        owner_id = create_random_user(db).id
    agent_in = AgentCreate(
        name=random_lower_string(),
        description=random_lower_string(),
        system_prompt="You are a helpful assistant.",
        llm_model="gpt-4o-mini",
    )
    return crud.create_agent(session=db, agent_in=agent_in, owner_id=owner_id)


def create_random_agent_version(db: Session, agent: Agent) -> AgentVersion:
    return crud.publish_agent_version(session=db, db_agent=agent, changelog="test")
```

`owner_id` 参数可选是为了方便写"某用户的 agent 别的用户看不到"这类权限测试。

---

## 任务 6：后端测试

### `backend/tests/crud/test_agent.py`

- 创建后字段正确、`owner_id` 正确
- 部分更新只改指定字段，其它字段不变
- `publish_agent_version` 第一次生成 `version_number == 1`，第二次是 2
- 快照内容与 Agent 当前配置一致
- 发布后再改 Agent，旧版本快照不变（这是版本机制的核心保证，必须有这条测试）

### `backend/tests/api/routes/test_agents.py`

参照 `test_items.py`，覆盖：

- 创建（normal user 和 superuser 都测）
- 读取自己的 / 读取不存在的（404）/ 读取别人的（403）
- 列表分页：创建 3 个后 `limit=2` 返回 2 条且 `count == 3`
- 列表隔离：用户 A 的列表里看不到用户 B 的 agent
- superuser 的列表能看到所有人的
- PATCH 部分更新
- DELETE 后再 GET 返回 404
- DELETE agent 后它的版本也被级联删除（直接查库断言）
- 发布版本：返回 `version_number == 1`；连发两次得到 1 和 2
- 未配置 `llm_model` 的 agent 发布返回 400
- 版本列表倒序（最新在前）

运行：

```bash
cd backend
uv run pytest tests/crud/test_agent.py tests/api/routes/test_agents.py -v
```

---

## 任务 7：生成前端客户端

后端接口定型后立刻执行（在仓库根目录）：

```bash
bash scripts/generate-client.sh
```

确认生成结果：

```bash
rg "class AgentsService" frontend/src/client/sdk.gen.ts
rg "AgentPublic|AgentVersionPublic" frontend/src/client/types.gen.ts
```

方法名应该是 `readAgents`、`readAgent`、`createAgent`、`updateAgent`、`deleteAgent`、`publishVersion`、`readVersions`、`readVersion`。如果名字不符合预期，去后端改函数名（operation id 来自 `tags[0] + "-" + 函数名`），改完重新生成。

**`frontend/src/client/` 的改动要一起提交。**

---

## 任务 8：前端 Agent 列表页

### 8.1 页面

新建 `frontend/src/routes/_layout/agents.tsx`，**结构完全照抄 `items.tsx`**：

```typescript
import { useSuspenseQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Suspense } from "react"

import { AgentsService } from "@/client"
import { DataTable } from "@/components/Common/DataTable"
import AddAgent from "@/components/Agents/AddAgent"
import { columns } from "@/components/Agents/columns"
import PendingAgents from "@/components/Pending/PendingAgents"

function getAgentsQueryOptions() {
  return {
    queryFn: async () =>
      (await AgentsService.readAgents({ query: { skip: 0, limit: 100 } })).data,
    queryKey: ["agents"],
  }
}

export const Route = createFileRoute("/_layout/agents")({
  component: Agents,
  head: () => ({ meta: [{ title: "Agents - AgentHub" }] }),
})
```

空状态、`Suspense` 包裹、`DataTable` 渲染都照 `items.tsx` 的写法。

### 8.2 组件

新建 `frontend/src/components/Agents/`：

| 文件 | 参照 | 说明 |
|------|------|------|
| `AddAgent.tsx` | `Items/AddItem.tsx` | Dialog + react-hook-form + zodResolver |
| `EditAgent.tsx` | `Items/EditItem.tsx` | 简单字段的快速编辑（name / description / is_active） |
| `DeleteAgent.tsx` | `Items/DeleteItem.tsx` | 确认对话框 |
| `AgentActionsMenu.tsx` | `Items/ItemActionsMenu.tsx` | 下拉菜单，多一个"Open"跳详情页 |
| `columns.tsx` | `Items/columns.tsx` | 列：name、llm_model、latest_version_number（无版本显示 Draft badge）、is_active、actions |

以及 `frontend/src/components/Pending/PendingAgents.tsx`，照 `PendingItems.tsx`。

`AddAgent` 的 zod schema：

```typescript
const formSchema = z.object({
  name: z.string().min(1, { message: "Name is required" }),
  description: z.string().optional(),
  system_prompt: z.string().optional(),
  llm_model: z.string().min(1, { message: "Model is required" }),
})
```

`system_prompt` 用 `Textarea` 而不是 `Input`。如果 `frontend/src/components/ui/textarea.tsx` 不存在，从 shadcn 补一个（参照同目录其它组件的写法和样式约定）。

**`DeleteItem.tsx` 里的 `onSettled` 调的是无参数的 `queryClient.invalidateQueries()`，这是模板的疏漏，不要照抄。** `DeleteAgent` 里要写成 `invalidateQueries({ queryKey: ["agents"] })`。

### 8.3 侧栏注册

`frontend/src/components/Sidebar/AppSidebar.tsx`：

```typescript
import { Bot, Home, Users } from "lucide-react"

const baseItems: Item[] = [
  { icon: Home, title: "Dashboard", path: "/" },
  { icon: Bot, title: "Agents", path: "/agents" },
]
```

Items 那一项在任务 10 里删掉。

---

## 任务 9：前端 Agent 详情页

新建 `frontend/src/routes/_layout/agents.$agentId.tsx`。这是本阶段唯一的新页面形态（带路径参数 + 多区块）。

### 页面结构

用 `Tabs` 组件分两个页签：

**Configuration 页签**：完整配置表单（name、description、system_prompt、llm_model、llm_settings 的 temperature 和 max_tokens、max_iterations、timeout_seconds、is_active），保存调 `updateAgent`。这是主要的编辑入口，`EditAgent` 对话框只做快速改名。

`llm_settings` 是自由 JSONB，但 UI 上只暴露两个常用项：

```typescript
llm_settings: {
  temperature: number   // 0 - 2，滑块或数字输入
  max_tokens: number
}
```

提交时组装成对象传给后端。不要给用户一个裸 JSON 编辑框。

**Versions 页签**：版本列表表格（version_number、changelog、created_at），每行可展开查看完整 `snapshot`（用 `<pre>` 格式化 JSON 即可，不用做 diff）。页签顶部有 "Publish new version" 按钮，弹 Dialog 填 changelog 后调 `publishVersion`。

### 路由与数据

```typescript
export const Route = createFileRoute("/_layout/agents/$agentId")({
  component: AgentDetail,
  head: () => ({ meta: [{ title: "Agent - AgentHub" }] }),
})

function AgentDetail() {
  const { agentId } = Route.useParams()
  const { data: agent } = useSuspenseQuery({
    queryFn: async () =>
      (await AgentsService.readAgent({ path: { id: agentId } })).data,
    queryKey: ["agents", agentId],
  })
  // ...
}
```

`queryKey` 用 `["agents", agentId]`。发布版本后要同时失效 `["agents", agentId]` 和 `["agent-versions", agentId]`。

### 404 处理

Agent 不存在时后端返回 404，`useSuspenseQuery` 会抛错。`__root.tsx` 已经配了 `ErrorComponent`，会兜住，不用额外处理。

---

## 任务 10：移除 Item

Agent 全链路跑通、测试全绿之后再做这一步。一次性删干净，不要留半截。

### 删除文件

```
backend/app/models/item.py
backend/app/crud/item.py
backend/app/api/routes/items.py
backend/tests/api/routes/test_items.py
backend/tests/utils/item.py
frontend/src/routes/_layout/items.tsx
frontend/src/components/Items/            （整个目录）
frontend/src/components/Pending/PendingItems.tsx
frontend/tests/items.spec.ts
```

### 修改引用

| 文件 | 改什么 |
|------|--------|
| `backend/app/models/__init__.py` | 删掉 item 相关的 import、`__all__` 条目、`Item.model_rebuild()` |
| `backend/app/crud/__init__.py` | 删掉 `create_item` |
| `backend/app/models/user.py` | 删掉 `User.items` 关系和 `TYPE_CHECKING` 里的 Item import |
| `backend/app/api/main.py` | 删掉 `items` 的 import 和 `include_router` |
| `backend/tests/conftest.py` | teardown 的模型元组里删掉 `Item` |
| `frontend/src/components/Sidebar/AppSidebar.tsx` | 删掉 Items 导航项和 `Briefcase` 图标 import |
| `frontend/src/routes/_layout/index.tsx` | Dashboard 上若有指向 Items 的链接或统计，改成 Agents |

### 生成删表迁移

```bash
cd backend
uv run alembic revision --autogenerate -m "Drop item table"
```

检查生成的文件只 drop `item` 表，没有误删别的东西。然后：

```bash
uv run alembic upgrade head
uv run alembic check
```

### 重新生成前端客户端

```bash
bash scripts/generate-client.sh
rg "ItemsService" frontend/src/     # 应该无命中
```

### 全局确认

```bash
rg -i "\bitem\b" backend/app backend/tests frontend/src frontend/tests --glob '!frontend/src/client/**'
```

剩下的命中应该只有 shadcn 组件里的 `DropdownMenuItem`、`SelectItem`、`FormItem`、`SidebarMenuItem` 这类无关词，以及 `AppSidebar.tsx` 里的 `Item` 导航项类型名（那是侧栏自己的类型，不是业务 Item）。

---

## 任务 11：Playwright E2E

新建 `frontend/tests/agents.spec.ts`，参照删掉的 `items.spec.ts`（先看一眼再删，或者从 git 历史里翻）。

覆盖场景：

1. 登录后进入 `/agents`，页面标题正确
2. 点 "Add Agent"，填表提交，列表里出现新 agent
3. 点进详情页，改 system_prompt，保存，刷新后改动还在
4. 在 Versions 页签点发布，版本列表出现 v1
5. 删除 agent，列表里消失

表单元素用 `data-testid` 定位，参照现有 spec 的 `getByTestId("email-input")` 风格，在组件上补相应的 `data-testid`。

运行：

```bash
bun run test                                  # 需要 dev server 或 PLAYWRIGHT_BASE_URL
bunx playwright test tests/agents.spec.ts     # 单文件
```

---

## 阶段验收清单

- [ ] `uv run alembic check` 输出 `No new upgrade operations detected.`
- [ ] `uv run alembic downgrade -2 && uv run alembic upgrade head` 往返无错（覆盖建表和删 item 两个迁移）
- [ ] `docker compose exec db psql -U postgres -d app -c "\d agent_version"` 能看到表和唯一约束
- [ ] `docker compose exec db psql -U postgres -d app -c "\dt"` 里**没有** `item` 表
- [ ] `uv run pytest tests/ -v` 全绿，且测试数量比阶段 01 多（新增了 agent 测试）
- [ ] `cd backend && bash scripts/lint.sh` 全绿
- [ ] `cd backend && bash scripts/test.sh` 全绿
- [ ] `rg "ItemsService|ItemPublic" frontend/src/` 无命中
- [ ] `bash scripts/generate-client.sh` 跑完后 `git status` 里 `frontend/src/client/` 的改动已提交
- [ ] `bun run lint` 通过
- [ ] 手动验证：登录后侧栏有 Agents 没有 Items；能创建 Agent；能进详情页改配置并保存；能发布 v1 和 v2；v1 的快照在改过 Agent 之后仍是旧内容；能删除 Agent
- [ ] `bunx playwright test tests/agents.spec.ts` 通过

---

## 常见坑

**`model_settings` 字段触发 Pydantic 告警**
用 `llm_settings`。见本文档开头的命名决策。

**JSONB 字段写入报 `Object of type UUID is not JSON serializable`**
`model_dump()` 改成 `model_dump(mode="json")`。

**Alembic 把 JSONB 生成成 `sa.JSON()`**
手工改成 `postgresql.JSONB()` 并补 import。`sa.JSON()` 在 Postgres 上会建成 `json` 而不是 `jsonb`，没法建 GIN 索引也没法高效查询。

**`AgentPublic.model_validate(agent, update={...})` 报字段缺失**
`latest_version_number` 是 `AgentPublic` 独有的非表字段，必须通过 `update=` 传入或者有默认值 `None`。上面的定义给了默认值，所以单条详情接口可以直接返回 ORM 对象。

**级联删除没生效**
确认三处都写了：模型的 `Relationship(cascade_delete=True)`、`Field(ondelete="CASCADE")`、以及迁移文件里外键的 `ondelete="CASCADE"`。SQLModel 的 `cascade_delete` 只管 ORM 层，数据库层要靠 `ondelete`。

**前端详情页路由文件名**
TanStack Router 的动态参数是 `$` 前缀，文件名必须是 `agents.$agentId.tsx`（点号分隔层级）。写成 `agents/[agentId].tsx` 不会被识别。

**发布版本后前端列表的 `latest_version_number` 没更新**
`invalidateQueries` 漏了 `["agents"]`。发布操作要同时失效三个 key：`["agents"]`、`["agents", agentId]`、`["agent-versions", agentId]`。

---

## 偏差记录

- 实际接口路径与本文档不一致之处：
- 其它偏差：
