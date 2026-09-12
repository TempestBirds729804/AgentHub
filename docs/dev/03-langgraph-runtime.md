# 03 — LangGraph 运行时与 Run 记录

## 前置依赖

阶段 [02-agent-crud.md](02-agent-crud.md) 的验收清单全部通过。特别确认：

- Agent / AgentVersion 表可用，能发布版本
- Item 已彻底移除
- `app/agent/snapshot.py` 的 `AgentSnapshot` 已定义

## 本阶段目标

让 Agent 真正跑起来，并且每次执行都留下可追溯的记录：

- 模型适配器（唯一实现：OpenAI 兼容接口）
- 最小 LangGraph 状态图（一个模型调用节点）
- `Run` 和 `RunEvent` 两张表
- 一个同步（阻塞式）的执行接口，跑完才返回
- 前端 Run 列表和执行轨迹详情页

## 本阶段不做什么

不做流式输出（阶段 04）、不做工具调用（阶段 05）、不做异步队列和 checkpoint（阶段 06）。这一阶段的状态图只有一个节点，就是调模型。

刻意从最小图开始，是为了先把 Run 生命周期、事件记录、Token 统计这套"骨架"验证对，再往图里加节点。

---

## 任务 1：模型适配器

### 1.1 抽象接口

新建 `backend/app/agent/models/base.py`：

```python
from abc import ABC, abstractmethod

from langchain_core.language_models import BaseChatModel


class ModelProvider(ABC):
    """模型供应商适配器。第一版只实现 OpenAI 兼容一种。"""

    @abstractmethod
    def get_chat_model(
        self,
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        streaming: bool = False,
    ) -> BaseChatModel:
        """返回一个 LangChain BaseChatModel 实例。"""

    @abstractmethod
    def supports(self, model: str) -> bool:
        """该供应商是否能处理这个模型名。"""
```

接口返回 LangChain 的 `BaseChatModel` 而不是自定义类型，这样 LangGraph 节点里可以直接用，不需要额外适配层。

### 1.2 OpenAI 兼容实现

新建 `backend/app/agent/models/openai_compat.py`：

```python
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.agent.models.base import ModelProvider
from app.core.config import settings


class OpenAICompatProvider(ModelProvider):
    """
    通过 OpenAI 兼容接口访问模型。base_url 可配置，因此同时支持
    OpenAI、DeepSeek、通义千问、vLLM、Ollama 等一切兼容服务。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key or settings.LLM_API_KEY
        self.base_url = base_url or settings.LLM_BASE_URL
        if not self.api_key:
            raise ModelNotConfiguredError(
                "LLM_API_KEY is not configured. Set it in .env before running agents."
            )

    def get_chat_model(
        self,
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        streaming: bool = False,
    ) -> BaseChatModel:
        return ChatOpenAI(
            model=model,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=temperature if temperature is not None else 0.7,
            max_tokens=max_tokens,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            max_retries=settings.LLM_MAX_RETRIES,
            streaming=streaming,
            stream_usage=True,
        )

    def supports(self, model: str) -> bool:
        return True
```

要点：

- `stream_usage=True` 必须开。否则流式模式下拿不到 token 用量，阶段 04 的费用统计会全是 0
- API key 缺失时在**构造函数**里就抛错，而不是等到调模型才失败。这样错误信息明确
- `supports()` 返回 `True`，因为兼容接口不限制模型名。将来加第二个 provider 时才需要真正的判断逻辑

### 1.3 自定义异常

新建 `backend/app/agent/exceptions.py`：

```python
class AgentError(Exception):
    """Agent 运行相关错误的基类。"""


class ModelNotConfiguredError(AgentError):
    """模型未正确配置（缺 key、缺 base_url 等）。"""


class ModelCallError(AgentError):
    """调模型失败。"""


class AgentTimeoutError(AgentError):
    """执行超时。"""


class MaxIterationsExceededError(AgentError):
    """超过最大迭代轮数。"""
```

后续阶段会往这里加 `ToolExecutionError`（05）、`ApprovalRequiredError`（07）。

### 1.4 provider 工厂

新建 `backend/app/agent/models/__init__.py`：

```python
from functools import lru_cache

from app.agent.models.base import ModelProvider
from app.agent.models.openai_compat import OpenAICompatProvider


@lru_cache(maxsize=1)
def get_provider() -> ModelProvider:
    """
    返回当前配置的模型供应商。

    第一版只有 OpenAI 兼容一种实现。将来支持多 provider 时，
    这里改成读 settings 里的 provider 名做分发。
    """
    return OpenAICompatProvider()
```

`lru_cache` 避免每次请求都重建 provider。注意这会让测试里改 `settings.LLM_API_KEY` 不生效，测试里要用 `get_provider.cache_clear()` 或直接构造 `OpenAICompatProvider`。

### 1.5 价格表与费用计算

新建 `backend/app/agent/pricing.py`：

```python
from decimal import Decimal

# 单位：美元 / 1000 tokens。只需要维护实际会用到的模型。
MODEL_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o": (Decimal("0.0025"), Decimal("0.01")),
    "gpt-4o-mini": (Decimal("0.00015"), Decimal("0.0006")),
    "deepseek-chat": (Decimal("0.00027"), Decimal("0.0011")),
}


def estimate_cost_usd(
    *, model: str, prompt_tokens: int, completion_tokens: int
) -> Decimal | None:
    """
    估算一次调用的费用。未知模型返回 None（而不是 0），
    这样 UI 上能区分"免费/本地模型"和"价格未知"。
    """
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return None
    prompt_price, completion_price = pricing
    return (
        prompt_price * Decimal(prompt_tokens) / 1000
        + completion_price * Decimal(completion_tokens) / 1000
    ).quantize(Decimal("0.000001"))
```

用 `Decimal` 而不是 `float`，费用累加用浮点会有误差。未知模型返回 `None` 而不是 `0`，这个区分在阶段 09 的成本对比里有意义。

---

## 任务 2：状态定义

新建 `backend/app/agent/state.py`：

```python
import uuid
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """
    LangGraph 状态。所有节点读写的就是这个字典。

    messages 用 add_messages reducer，节点返回的消息会追加而不是覆盖。
    其它字段是普通覆盖语义。
    """

    messages: Annotated[list[BaseMessage], add_messages]

    # 运行上下文（图构建时注入，节点只读）
    run_id: uuid.UUID
    agent_version_id: uuid.UUID

    # 配置快照（来自 AgentVersion.snapshot）
    system_prompt: str
    llm_model: str
    llm_settings: dict[str, Any]
    max_iterations: int

    # 执行过程中累积的统计
    iteration: int
    prompt_tokens: int
    completion_tokens: int
```

`messages` 的 `add_messages` reducer 是 LangGraph 的核心机制：节点返回 `{"messages": [ai_message]}` 时它会追加到列表末尾，并且能正确处理消息 ID 去重。不要自己写 `state["messages"] + [msg]`。

后续阶段会往 `AgentState` 加字段：`tool_calls`（05）、`retrieved_chunks`（08）、`pending_approval`（07）。加字段时要给所有已有的图构建点补上初始值。

---

## 任务 3：最小状态图

### 3.1 节点实现

新建 `backend/app/agent/nodes.py`：

```python
from langchain_core.messages import AIMessage, SystemMessage

from app.agent.exceptions import ModelCallError
from app.agent.models import get_provider
from app.agent.state import AgentState


async def call_model(state: AgentState) -> dict[str, object]:
    """调用 LLM，把回复追加到 messages。"""
    provider = get_provider()
    settings_dict = state["llm_settings"]
    chat = provider.get_chat_model(
        model=state["llm_model"],
        temperature=settings_dict.get("temperature"),
        max_tokens=settings_dict.get("max_tokens"),
    )

    messages: list[BaseMessage] = []
    if state["system_prompt"]:
        messages.append(SystemMessage(content=state["system_prompt"]))
    messages.extend(state["messages"])

    try:
        response = await chat.ainvoke(messages)
    except Exception as exc:
        raise ModelCallError(str(exc)) from exc

    usage = response.usage_metadata or {}
    return {
        "messages": [response],
        "iteration": state["iteration"] + 1,
        "prompt_tokens": state["prompt_tokens"] + usage.get("input_tokens", 0),
        "completion_tokens": state["completion_tokens"] + usage.get("output_tokens", 0),
    }
```

要点：

- **system prompt 每次调用时临时拼在最前面，不存进 `state["messages"]`**。否则多轮对话时会被反复追加，也会让 checkpoint 里存冗余数据
- 节点是 `async def`。整条执行链路都是异步的，不要混同步
- token 用量从 `response.usage_metadata` 取，这是 LangChain 统一后的字段，不同 provider 都能用
- 节点只返回**改变的字段**，LangGraph 会自动合并

### 3.2 图构建

新建 `backend/app/agent/graph.py`：

```python
from langgraph.graph import END, START, StateGraph

from app.agent.nodes import call_model
from app.agent.state import AgentState


def build_agent_graph() -> StateGraph:
    """
    构建 Agent 状态图。

    当前是最小形态：START -> call_model -> END。
    阶段 05 加 tools 节点和条件边，阶段 07 加 approval 中断点，
    阶段 08 加 retrieve 节点。
    """
    graph = StateGraph(AgentState)
    graph.add_node("call_model", call_model)
    graph.add_edge(START, "call_model")
    graph.add_edge("call_model", END)
    return graph


def compile_agent_graph():  # 返回类型由 langgraph 决定，阶段 06 加 checkpointer 参数
    return build_agent_graph().compile()
```

`build_agent_graph()` 返回未编译的图，`compile_agent_graph()` 负责编译。分开是为了阶段 06 能在编译时注入 checkpointer 而不改图结构。

---

## 任务 4：Run 与 RunEvent 模型

新建 `backend/app/models/run.py`：

```python
import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Numeric
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel

from app.models.base import get_datetime_utc

if TYPE_CHECKING:
    from app.models.agent import AgentVersion
    from app.models.user import User


class RunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunTrigger(str, enum.Enum):
    PLAYGROUND = "playground"
    API = "api"
    EVAL = "eval"


class RunEventType(str, enum.Enum):
    RUN_STARTED = "run_started"
    NODE_STARTED = "node_started"
    NODE_FINISHED = "node_finished"
    MODEL_CHUNK = "model_chunk"
    TOOL_CALLED = "tool_called"
    TOOL_RESULT = "tool_result"
    CONTEXT_RETRIEVED = "context_retrieved"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"


class RunCreate(SQLModel):
    agent_version_id: uuid.UUID
    input: dict[str, Any] = Field(default_factory=dict)
    trigger: RunTrigger = RunTrigger.PLAYGROUND
    conversation_id: uuid.UUID | None = None   # 阶段 04 才用得上


class Run(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE", index=True
    )
    agent_version_id: uuid.UUID = Field(
        foreign_key="agent_version.id", nullable=False, ondelete="CASCADE", index=True
    )
    conversation_id: uuid.UUID | None = Field(default=None, index=True)

    status: RunStatus = Field(default=RunStatus.QUEUED, index=True)
    trigger: RunTrigger = Field(default=RunTrigger.PLAYGROUND)

    input: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    output: dict[str, Any] | None = Field(default=None, sa_type=JSONB)
    error: str | None = Field(default=None, max_length=4000)

    started_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)  # type: ignore
    )
    finished_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)  # type: ignore
    )
    duration_ms: int | None = None

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal | None = Field(
        default=None, sa_type=Numeric(12, 6)  # type: ignore
    )

    # 阶段 06 使用
    thread_id: str | None = Field(default=None, max_length=128, index=True)
    checkpoint_id: str | None = Field(default=None, max_length=128)

    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )

    agent_version: "AgentVersion | None" = Relationship()
    events: list["RunEvent"] = Relationship(back_populates="run", cascade_delete=True)


class RunPublic(SQLModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    agent_version_id: uuid.UUID
    conversation_id: uuid.UUID | None = None
    status: RunStatus
    trigger: RunTrigger
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal | None = None
    created_at: datetime | None = None


class RunsPublic(SQLModel):
    data: list[RunPublic]
    count: int


class RunEvent(SQLModel, table=True):
    __tablename__ = "run_event"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    run_id: uuid.UUID = Field(
        foreign_key="run.id", nullable=False, ondelete="CASCADE", index=True
    )
    seq: int = Field(nullable=False)
    event_type: RunEventType = Field(nullable=False)
    node_name: str | None = Field(default=None, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )

    run: Run | None = Relationship(back_populates="events")


class RunEventPublic(SQLModel):
    id: uuid.UUID
    run_id: uuid.UUID
    seq: int
    event_type: RunEventType
    node_name: str | None = None
    payload: dict[str, Any]
    created_at: datetime | None = None


class RunEventsPublic(SQLModel):
    data: list[RunEventPublic]
    count: int
```

要点：

- **枚举用 `str, enum.Enum` 的组合**。这样 SQLModel 会把它建成 varchar 而不是 Postgres 原生 enum 类型。原生 enum 类型加值需要 `ALTER TYPE` 迁移，很麻烦。全部取值在这一阶段就定全（见 [00-overview.md](00-overview.md) 第 3.1 节），后续阶段只是开始用到更多值
- `cost_usd` 用 `Numeric(12, 6)`，不要用 `Float`
- `conversation_id` 现在**没有外键约束**，因为 Conversation 表在阶段 04 才建。阶段 04 要加一个迁移补上外键
- `status` 和 `owner_id` 建索引，Run 列表会频繁按这两个字段过滤
- `error` 限长 4000，异常堆栈可能很长，写入前要截断

别忘了在 `app/models/__init__.py` 里导出全部符号并加 `Run.model_rebuild()` / `RunEvent.model_rebuild()`。

---

## 任务 5：迁移

```bash
cd backend
uv run alembic revision --autogenerate -m "Add run and run event models"
```

人工检查：

- `run` 和 `run_event` 两张表都建了
- 枚举列是 `sa.VARCHAR` 而不是 `postgresql.ENUM`。如果生成了 ENUM，说明模型里的枚举没继承 `str`，回去改
- `cost_usd` 是 `sa.Numeric(precision=12, scale=6)`
- JSONB 列是 `postgresql.JSONB()`
- 索引都建上了

然后：

```bash
uv run alembic upgrade head
uv run alembic downgrade -1 && uv run alembic upgrade head
uv run alembic check
```

同时更新 `backend/tests/conftest.py` 的 teardown 元组（子表在前）：

```python
for model in (RunEvent, Run, AgentVersion, Agent, User):
    session.execute(delete(model))
```

---

## 任务 6：Run 服务层

这是本阶段最核心的代码。新建 `backend/app/services/run_service.py`。

### 6.1 事件记录器

执行过程中要往 `run_event` 表写事件，`seq` 必须单调递增。用一个小类管理：

```python
class RunEventRecorder:
    """按序记录 Run 事件。同一个 Run 的 seq 从 0 开始递增。"""

    def __init__(self, *, session: AsyncSession, run_id: uuid.UUID) -> None:
        self._session = session
        self._run_id = run_id
        self._seq = 0

    async def record(
        self,
        event_type: RunEventType,
        *,
        node_name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event = RunEvent(
            run_id=self._run_id,
            seq=self._seq,
            event_type=event_type,
            node_name=node_name,
            payload=payload or {},
        )
        self._session.add(event)
        await self._session.flush()
        self._seq += 1
```

用 `flush()` 而不是 `commit()`：事件在执行过程中逐条累积，最后由调用方一次性 commit。这样执行失败时事件也能随 Run 状态一起落库（见 6.3 的异常处理）。

### 6.2 执行主流程

```python
async def execute_run(
    *,
    session: AsyncSession,
    run: Run,
    snapshot: AgentSnapshot,
    initial_messages: list[BaseMessage],
) -> Run:
    """
    阻塞式执行一个 Run，跑完才返回。

    调用方负责：Run 已落库、权限已校验、snapshot 已从 AgentVersion 解析。
    本函数负责：状态流转、事件记录、统计汇总、异常兜底。
    """
    recorder = RunEventRecorder(session=session, run_id=run.id)
    started = time.perf_counter()

    run.status = RunStatus.RUNNING
    run.started_at = get_datetime_utc()
    session.add(run)
    await recorder.record(RunEventType.RUN_STARTED, payload={"input": run.input})
    await session.commit()

    initial_state: AgentState = {
        "messages": initial_messages,
        "run_id": run.id,
        "agent_version_id": run.agent_version_id,
        "system_prompt": snapshot.system_prompt,
        "llm_model": snapshot.llm_model,
        "llm_settings": snapshot.llm_settings,
        "max_iterations": snapshot.max_iterations,
        "iteration": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }

    try:
        app = compile_agent_graph()
        final_state = await asyncio.wait_for(
            app.ainvoke(initial_state),
            timeout=snapshot.timeout_seconds,
        )
    except TimeoutError as exc:
        await _fail_run(
            session=session,
            run=run,
            recorder=recorder,
            error=f"Run exceeded timeout of {snapshot.timeout_seconds}s",
            started=started,
        )
        raise AgentTimeoutError(str(exc)) from exc
    except Exception as exc:
        await _fail_run(
            session=session,
            run=run,
            recorder=recorder,
            error=str(exc)[:4000],
            started=started,
        )
        raise

    last_message = final_state["messages"][-1]
    run.status = RunStatus.SUCCEEDED
    run.output = {"content": last_message.content}
    run.prompt_tokens = final_state["prompt_tokens"]
    run.completion_tokens = final_state["completion_tokens"]
    run.cost_usd = estimate_cost_usd(
        model=snapshot.llm_model,
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
    )
    run.finished_at = get_datetime_utc()
    run.duration_ms = int((time.perf_counter() - started) * 1000)
    session.add(run)
    await recorder.record(
        RunEventType.RUN_FINISHED,
        payload={
            "output": run.output,
            "prompt_tokens": run.prompt_tokens,
            "completion_tokens": run.completion_tokens,
        },
    )
    await session.commit()
    await session.refresh(run)
    return run
```

### 6.3 失败兜底

```python
async def _fail_run(
    *,
    session: AsyncSession,
    run: Run,
    recorder: RunEventRecorder,
    error: str,
    started: float,
) -> None:
    """把 Run 标记为失败并落库。绝不能让异常导致 Run 永远停在 running。"""
    await session.rollback()
    run.status = RunStatus.FAILED
    run.error = error[:4000]
    run.finished_at = get_datetime_utc()
    run.duration_ms = int((time.perf_counter() - started) * 1000)
    session.add(run)
    await recorder.record(RunEventType.RUN_FAILED, payload={"error": run.error})
    await session.commit()
```

**注意 `await session.rollback()` 是必须的第一步**：如果是数据库异常导致的失败，session 处于脏状态，不 rollback 后面的写入全会失败，Run 就会永远卡在 `running`。这是最容易漏的一点。

`_fail_run` 里的 `recorder` 的 `seq` 可能因为 rollback 而与已落库的事件冲突。规避办法：`_fail_run` 里重新查一次当前最大 seq，或者简单地用一个足够大的值。实现时选前者：

```python
    result = await session.execute(
        select(func.coalesce(func.max(RunEvent.seq), -1)).where(RunEvent.run_id == run.id)
    )
    recorder._seq = result.scalar_one() + 1
```

如果觉得访问私有属性难看，给 `RunEventRecorder` 加一个 `async def resync_seq()` 方法。

### 6.4 节点级事件

`call_model` 节点自己不写事件（节点里拿不到 session，也不该关心持久化）。节点级事件通过 LangGraph 的 `astream` 事件流采集 —— 但那属于阶段 04 的流式改造。

**本阶段先只记 `run_started` / `run_finished` / `run_failed` 三种事件**，在 6.2 的主流程里写。阶段 04 会把 `ainvoke` 换成 `astream_events` 并补齐 `node_started` / `node_finished` / `model_chunk`。

在 `execute_run` 的注释里写明这一点，免得后面的人以为漏了。

---

## 任务 7：路由

新建 `backend/app/api/routes/runs.py`，`tags=["runs"]`。

### 接口清单

| 方法 | 路径 | response_model | session 类型 | 说明 |
|------|------|----------------|-------------|------|
| POST | `/runs/` | `RunPublic` | **async** | 创建并阻塞执行，跑完返回 |
| GET | `/runs/` | `RunsPublic` | sync | 列表，支持按 `agent_version_id`、`status` 过滤 |
| GET | `/runs/{id}` | `RunPublic` | sync | 详情 |
| GET | `/runs/{id}/events` | `RunEventsPublic` | sync | 执行轨迹，按 `seq` 升序 |

**只有 POST 用 `AsyncSessionDep`，三个读接口用 `SessionDep`**。这是 [AGENTS.md](../../AGENTS.md) 第 3 节的同步异步分工，严格遵守。

### POST 的实现要点

```python
@router.post("/", response_model=RunPublic)
async def create_run(
    *,
    session: AsyncSessionDep,
    current_user: CurrentUser,
    run_in: RunCreate,
) -> Any:
    """
    Create and execute an agent run synchronously.

    Blocks until the run finishes. For long-running agents use the async
    endpoint added in phase 06.
    """
```

步骤：

1. 查 `AgentVersion`（用 async 写法：`await session.execute(select(...))`），不存在返回 404
2. 顺着 `agent_version.agent_id` 查 `Agent` 校验 `owner_id`，非本人且非 superuser 返回 403
3. 用 `AgentSnapshot.model_validate(agent_version.snapshot)` 解析快照，校验失败返回 400 并说明是快照格式问题
4. 从 `run_in.input` 里取用户输入。约定 `input` 的结构是 `{"message": "..."}`，缺 `message` 返回 422
5. 建 `Run` 记录（`status=QUEUED`），`add` + `commit` + `refresh`
6. 调 `execute_run(...)`
7. `AgentError` 系列异常的处理：**不要向上抛 500**。`execute_run` 已经把 Run 标记成 failed 并落库了，这里捕获后直接返回那条 Run（HTTP 200 + `status=failed`）。这样前端能拿到错误详情展示在 Run 详情页，比一个 500 有用得多

   ```python
   try:
       run = await execute_run(...)
   except AgentError:
       await session.refresh(run)
   return run
   ```

8. `ModelNotConfiguredError` 例外：这是配置问题不是运行失败，返回 503 并提示配置 `LLM_API_KEY`

### 权限辅助函数

和 `agents.py` 一样，把"查版本 + 校验归属"抽成本地函数。因为要用 async session，签名和 `agents.py` 的不同，单独写一个。

### 注册路由

`app/api/main.py` 里 `api_router.include_router(runs.router)`。

---

## 任务 8：测试

### 8.1 不要调真实 API

所有测试必须用假模型。LangChain 提供了现成的：

```python
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
```

在 `backend/tests/conftest.py` 里加一个 fixture：

```python
@pytest.fixture
def fake_chat_model(monkeypatch: pytest.MonkeyPatch) -> GenericFakeChatModel:
    """把 provider 的 get_chat_model 换成假模型，避免测试调真实 API。"""
    fake = GenericFakeChatModel(messages=iter([AIMessage(content="fake reply")]))

    def _get_chat_model(self, **kwargs):  # noqa: ANN001, ANN003
        return fake

    monkeypatch.setattr(OpenAICompatProvider, "get_chat_model", _get_chat_model)
    get_provider.cache_clear()
    return fake
```

**必须调 `get_provider.cache_clear()`**，否则 `lru_cache` 缓存的旧 provider 实例会让 patch 失效。

另外 `OpenAICompatProvider.__init__` 在没有 `LLM_API_KEY` 时会抛错，所以 fixture 里还要 patch 掉 key：

```python
    monkeypatch.setattr(settings, "LLM_API_KEY", "test-key")
```

`GenericFakeChatModel` 不返回 `usage_metadata`，所以 token 统计会是 0。测试 token 统计时手工构造带 usage 的 `AIMessage`：

```python
AIMessage(
    content="fake reply",
    usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
)
```

### 8.2 `backend/tests/agent/test_pricing.py`

- 已知模型的费用计算正确（手算一个值对比）
- 未知模型返回 `None` 而不是 0
- 零 token 返回 `Decimal("0")`

### 8.3 `backend/tests/agent/test_graph.py`

- 图能编译
- 用假模型 `ainvoke` 一次，`messages` 末尾是 AIMessage
- `iteration` 从 0 变成 1
- 带 usage 的假回复能正确累加到 `prompt_tokens` / `completion_tokens`
- `system_prompt` 非空时被传给模型，且**没有**出现在返回的 `state["messages"]` 里（这条要专门测，是容易写错的点）

### 8.4 `backend/tests/api/routes/test_runs.py`

- 创建 Run 成功，返回 `status == "succeeded"`，`output.content` 是假回复内容
- `duration_ms` 大于 0，`started_at` 和 `finished_at` 都有值
- 模型抛异常时 Run 落成 `status == "failed"` 且 `error` 非空，**HTTP 状态码仍是 200**
- 用不存在的 `agent_version_id` 返回 404
- 用别人的 agent 的版本返回 403
- `input` 里没有 `message` 返回 422
- 事件接口返回至少 2 条事件（`run_started` 和 `run_finished`），`seq` 严格递增
- 失败的 Run 的事件里有 `run_failed`
- 列表按 `status` 过滤生效
- 列表隔离：用户 A 看不到用户 B 的 Run

### 8.5 手动验证真实模型

自动化测试全用假模型，所以必须手动验证一次真实调用。在 `.env` 里配好 `LLM_API_KEY`，起服务后：

```bash
# 先登录拿 token
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/login/access-token \
  -d "username=admin@example.com&password=changethis" | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 用某个已发布的 agent version id 跑一次
curl -X POST http://localhost:8000/api/v1/runs/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"agent_version_id":"<uuid>","input":{"message":"用一句话介绍你自己"}}'
```

确认返回里 `output.content` 是真实回复，`prompt_tokens` / `completion_tokens` / `cost_usd` 都有合理数值。

---

## 任务 9：前端

### 9.1 生成客户端

```bash
bash scripts/generate-client.sh
rg "class RunsService" frontend/src/client/sdk.gen.ts
```

### 9.2 Run 列表页

新建 `frontend/src/routes/_layout/runs.tsx`，照 `agents.tsx` 的结构。

列：status（用 `Badge`，不同状态不同 variant）、agent 名（需要 join，见下）、trigger、duration_ms、tokens（prompt + completion）、cost_usd、created_at、actions（查看详情）。

**agent 名的问题**：`RunPublic` 只有 `agent_version_id`，没有 agent 名。三个选择：

1. 后端在 `RunPublic` 上加 `agent_name` 和 `version_number` 两个非表字段，在路由里 join 填充
2. 前端额外拉一次 agents 列表在内存里匹配
3. 列表只显示版本 id 的前 8 位

**选方案 1**，因为 Run 列表是高频页面，前端匹配会很别扭。在 `RunPublic` 上加：

```python
    agent_name: str | None = None
    agent_version_number: int | None = None
```

路由里用一次 join 查询批量填充（参照阶段 02 里 `latest_version_number` 的做法，避免 N+1）。

状态 Badge 的颜色约定（后续阶段复用）：

```typescript
const statusVariant: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  queued: "outline",
  running: "secondary",
  waiting_approval: "outline",
  succeeded: "default",
  failed: "destructive",
  cancelled: "secondary",
}
```

### 9.3 Run 详情页

新建 `frontend/src/routes/_layout/runs.$runId.tsx`。三个区块：

**概要卡片**：status、trigger、耗时、token、费用、错误信息（失败时用 `Alert` 的 destructive 变体显示）。

**输入输出**：`input` 里的 message 和 `output` 里的 content，用 `Card` 分开展示。

**执行轨迹**：`RunEvent` 列表按 `seq` 升序，每条一行，显示 `event_type`、`node_name`、相对开始时间的偏移毫秒数，点击展开 `payload` 的 JSON。这是本阶段最有展示价值的部分，把它做清楚。

用两个 query：

```typescript
queryKey: ["runs", runId]
queryKey: ["runs", runId, "events"]
```

### 9.4 侧栏注册

`AppSidebar.tsx` 的 `baseItems` 加：

```typescript
{ icon: Activity, title: "Runs", path: "/runs" },
```

### 9.5 Agent 详情页加"试运行"

在 `agents.$agentId.tsx` 的 Versions 页签里，每个版本行加一个"Run"按钮，弹 Dialog 让用户输入一句话，提交调 `createRun`，成功后跳转到 `/runs/$runId`。

这是本阶段唯一能触发执行的 UI 入口。阶段 04 的 Playground 会取代它成为主要调试入口，但这个快捷入口保留着有用。

**注意这个接口是阻塞的**，可能要等十几秒。按钮上必须用 `LoadingButton` + `mutation.isPending`，并在 Dialog 里加一句"执行中，请勿关闭"的提示。

---

## 阶段验收清单

- [x] `uv run alembic check` 输出 `No new upgrade operations detected.`
- [x] `docker compose exec db psql -U postgres -d app -c "\d run"` 里 `status` 列是 `character varying` 不是自定义 enum 类型
- [x] `docker compose exec db psql -U postgres -d app -c "\d run"` 里 `cost_usd` 是 `numeric(12,6)`
- [x] `uv run pytest tests/agent/ tests/api/routes/test_runs.py -v` 全绿
- [x] 测试运行时没有任何真实网络请求（可以临时把 `LLM_BASE_URL` 改成无效地址，测试应该照样全绿）
- [x] `cd backend && bash scripts/lint.sh` 全绿
- [x] `cd backend && bash scripts/test.sh` 全绿
- [x] 手动用真实 key 跑通一次 `POST /api/v1/runs/`，返回真实回复且 token 和费用有值
- [x] 故意把 `LLM_MODEL` 改成不存在的模型名，跑一次，确认 Run 落成 `failed`、`error` 有内容、HTTP 状态是 200，且 Run 详情页能看到错误
- [x] 故意把 Agent 的 `timeout_seconds` 设为 1，跑一次，确认超时后 Run 落成 `failed` 而不是卡在 `running`
- [x] `bun run lint` 通过
- [x] 手动验证：Runs 列表有数据且状态 Badge 颜色正确；Run 详情页能看到完整执行轨迹，事件按 seq 排序；从 Agent 详情页的版本行能触发试运行并跳转
- [x] 数据库里 `SELECT status, count(*) FROM run GROUP BY status;` 没有任何行卡在 `running`

---

## 常见坑

**`lru_cache` 导致测试里的 monkeypatch 失效**
`get_provider` 有缓存。patch 后必须 `get_provider.cache_clear()`。

**Run 永远卡在 `running`**
`_fail_run` 里漏了 `await session.rollback()`，或者异常没被 `execute_run` 的 `except` 兜住。验收清单里那条 SQL 查询就是为了发现这个问题。

**`usage_metadata` 是 None**
非流式调用一般有，流式调用需要 `stream_usage=True`。取值时用 `response.usage_metadata or {}` 兜住。

**`asyncio.wait_for` 超时后任务还在后台跑**
`wait_for` 会取消 task，但底层的 HTTP 请求可能已经发出。这是可接受的（模型那边会正常返回只是没人收），不用额外处理。但**不要**在 `except TimeoutError` 里再去访问 `final_state`，它不存在。

**Python 3.11+ 的 `TimeoutError`**
`asyncio.TimeoutError` 已经是内置 `TimeoutError` 的别名。直接 `except TimeoutError`，不要 `except asyncio.TimeoutError`（ruff 会报 UP041）。

**`system_prompt` 被重复追加**
如果把 system message 写进了 `state["messages"]`，多轮对话时每轮都会加一条。必须在节点里临时拼接。阶段 08 的 test_graph 有专门一条测试守这个。

**枚举在 JSON 里序列化成 `RunStatus.SUCCEEDED`**
枚举必须继承 `str`。`class RunStatus(str, enum.Enum)` 才会序列化成 `"succeeded"`。

**`Numeric` 列在 Pydantic 里变成 float**
`RunPublic.cost_usd` 标注为 `Decimal | None`，FastAPI 会序列化成 JSON 数字字符串。前端拿到的是 string，格式化时要 `Number(cost_usd)`。

**阻塞式 POST 在 Traefik 后面超时**
默认超时通常够用，但如果 Agent 跑很久，Docker Compose 环境下可能被代理切断。这是阶段 06 要用异步执行解决的问题，本阶段只要在 UI 上提示用户即可。

---

## 偏差记录

- 实际使用的模型与 base_url：`deepseek-flash`，`https://api.deepseek.com`（2026-09-12 实测）。
- 其它偏差：
  - 前序基线复核：68 项后端测试、Agent 管理 Playwright（含登录共 2 项）、mypy / ty / ruff、Alembic check 通过。Windows 无可用 bash，使用脚本内部等价命令。数据库报告既有 collation 版本不一致，本阶段不修改排序规则。
  - 当前 LangGraph 的 StateLike 协议边界不能被 ty 正确识别为 TypedDict；graph.py 保留完整泛型及 mypy 检查，仅对该边界使用局部 ty ignore。
  - ChatOpenAI 使用当前版本的显式参数 max_completion_tokens（对应快照中的 max_tokens）。价格表沿用本文参考值，属于估算而非实时报价。
  - 为满足双向级联关系约定，需在 backend/app/models/user.py 和 agent.py 增加 Run 反向关系；测试辅助文件 backend/tests/utils/run.py 和前端 components/Runs/、Pending/PendingRuns.tsx 属于本阶段配套实现。
  - 当前 SQLModel 的 str + Enum 仍会推导为原生 enum，因此显式使用 String(32)；RunEvent 增加 (run_id, seq) 唯一约束，保证同一 Run 的事件序号不会重复。
  - frontend/tests/runs.spec.ts 将浏览器验收固化为假响应交互测试；真实数据库和 LangGraph 执行由后端测试覆盖。Windows TestClient 显式使用 SelectorEventLoop，与 psycopg 异步连接兼容。
  - Windows 本地启动已验证：`uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --loop asyncio:SelectorEventLoop`。当前 Uvicorn 默认无 reload 的 Windows loop 是 Proactor，与 psycopg 不兼容；不更改 Linux 部署配置。浏览器访问 `http://localhost:8000`；Playwright 运行时设置 `VITE_API_URL=http://127.0.0.1:8000`，避免 Node 将 localhost 解析为未监听的 IPv6 地址。
  - 新增实际使用模型 deepseek-flash 的估算价格，按官方峰时、缓存未命中价格计算（输入 $0.3/百万 Token，输出 $1.2/百万 Token）；实际账单受缓存和峰谷时段影响。来源：https://api-docs.deepseek.com/quick_start/pricing/ 。不扩展本阶段的数据模型和计费接口。
  - 真实验收：成功 Run `fb3eba53-e469-4670-8337-0a7cf13b5104`，耗时 4597 ms，输入/输出 Token 为 38/106，估算费用 $0.000139；无效模型 Run `aaa74b3f-054c-4f9e-b68e-0ae9cbb0afc6` 和 1 秒超时 Run `9edd8c05-d570-41a5-a4f5-79f667f12a2a` 均为 HTTP 200 + failed，事件 seq 为 0、1。无效模型通过发布独立版本验证，不修改全局 LLM_MODEL 或历史快照。
  - 自动验收：完整后端测试 82 项通过，随后新增 deepseek-flash 价格项及测试，价格测试 4 项通过；mypy、ty、ruff 和 Alembic check 通过；前端 build、Biome 通过，Playwright 的登录、Agent 管理、Run 成功和失败交互共 4 项通过。前端客户端由现有生成器生成，其空白行保留生成器输出，未手改。
  - 浏览器实测：真实记录列表成功/失败颜色正确，失败详情显示供应商错误且 payload 可展开；从 v1 的 Run 按钮提交后显示等待提示并跳转成功详情（`20087516-a646-4d77-8bbd-6e082c85e504`）。该次常驻服务尚未重载价格表，费用为 Unknown；重启后通过本地 HTTP 再次验证，Run `0a003f25-e552-4a46-ac9d-4074f331a0a5` 输入/输出 Token 为 41/91，估算费用 $0.000122。历史记录不回填。
