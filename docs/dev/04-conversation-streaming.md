# 04 — 会话记忆与流式输出

## 前置依赖

阶段 [03-langgraph-runtime.md](03-langgraph-runtime.md) 的验收清单全部通过。特别确认：

- `POST /api/v1/runs/` 能用真实模型跑通并返回回复
- Run 详情页能看到执行轨迹
- 数据库里没有卡在 `running` 的 Run

## 本阶段目标

这是**第一个可演示的里程碑**。做完之后平台能像一个真正的 Agent 产品那样用：

- `Conversation` 和 `Message` 两张表，多轮对话持久化
- 上下文裁剪与自动摘要，避免长对话超出 context window
- 把 `ainvoke` 换成 `astream_events`，补齐节点级事件
- SSE 流式接口，回复逐字推给前端
- Playground 页面，完整的对话调试界面

## 本阶段不做什么

不做工具调用（阶段 05）、不做异步队列（阶段 06）。流式执行仍然是"请求打进来就在这个进程里跑完"。

---

## 先读这一条：SSE 不能用 EventSource

浏览器原生的 `EventSource` API **不支持自定义请求头**，而本项目的鉴权是 `Authorization: Bearer <token>`。

所以前端**必须用 `fetch` + `ReadableStream` 手动解析 SSE**，不能用 `EventSource`，也不能用依赖它的第三方库。任务 6 给出完整的解析实现。

同理，`@hey-api/openapi-ts` 生成的客户端对 SSE 的支持（`core/serverSentEvents.gen.ts`）在带鉴权头的场景下不一定够用。**流式接口不通过生成的 Service 调用，直接写 fetch**。非流式接口继续用生成的客户端。

这意味着流式接口的 URL 和事件格式是前后端之间的手工契约，改动时两边都要改。把契约写在任务 4 里并保持同步。

---

## 任务 1：Conversation 与 Message 模型

新建 `backend/app/models/conversation.py`：

```python
import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel

from app.models.base import get_datetime_utc

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.user import User


class MessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class ConversationBase(SQLModel):
    title: str | None = Field(default=None, max_length=255)


class ConversationCreate(ConversationBase):
    agent_id: uuid.UUID


class ConversationUpdate(SQLModel):
    title: str | None = Field(default=None, max_length=255)


class Conversation(ConversationBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE", index=True
    )
    agent_id: uuid.UUID = Field(
        foreign_key="agent.id", nullable=False, ondelete="CASCADE", index=True
    )
    # LangGraph checkpointer 的线程标识。阶段 06 会真正用到，现在先生成好存着。
    thread_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()), max_length=128, unique=True, index=True
    )
    # 早期消息被裁剪后的摘要，见任务 2
    summary: str | None = Field(default=None, max_length=8000)
    summarized_up_to_seq: int | None = None

    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    updated_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )

    messages: list["ConvMessage"] = Relationship(
        back_populates="conversation", cascade_delete=True
    )


class ConversationPublic(ConversationBase):
    id: uuid.UUID
    owner_id: uuid.UUID
    agent_id: uuid.UUID
    thread_id: str
    summary: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    message_count: int | None = None


class ConversationsPublic(SQLModel):
    data: list[ConversationPublic]
    count: int


class ConvMessage(SQLModel, table=True):
    """
    对话中的一条消息。

    类名不叫 Message，因为 app/models/base.py 里已经有一个 Message
    （模板的通用响应模型 {"message": "..."}）。表名仍然是 message。
    """

    __tablename__ = "message"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    conversation_id: uuid.UUID = Field(
        foreign_key="conversation.id", nullable=False, ondelete="CASCADE", index=True
    )
    seq: int = Field(nullable=False)
    role: MessageRole = Field(nullable=False)
    content: str = Field(default="")
    # 阶段 05 用：模型发起的工具调用
    tool_calls: list[dict[str, Any]] = Field(default_factory=list, sa_type=JSONB)
    tool_call_id: str | None = Field(default=None, max_length=128)
    token_count: int | None = None
    run_id: uuid.UUID | None = Field(default=None, index=True)

    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )

    conversation: Conversation | None = Relationship(back_populates="messages")


class ConvMessagePublic(SQLModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    seq: int
    role: MessageRole
    content: str
    tool_calls: list[dict[str, Any]]
    tool_call_id: str | None = None
    token_count: int | None = None
    run_id: uuid.UUID | None = None
    created_at: datetime | None = None


class ConvMessagesPublic(SQLModel):
    data: list[ConvMessagePublic]
    count: int
```

### 为什么类名是 `ConvMessage`

`app/models/base.py` 里已经有一个 `Message`，是模板的通用响应模型（DELETE 接口返回的 `{"message": "..."}`）。两个同名类会在 `models/__init__.py` 里冲突。

选择重命名对话消息类而不是通用响应类，因为后者被十几个现有接口用着。`__tablename__` 仍然显式指定为 `"message"`，数据库里看到的表名是干净的。

**`ConvMessagePublic` 会导致前端生成的类型名叫 `ConvMessagePublic`**，有点丑但可接受。不要为了好看去改通用 `Message` 类。

`content` 字段不设 `max_length`，模型回复可能很长。Postgres 的 `text` 类型没有长度限制且性能没有区别。

### 给 `run.conversation_id` 补外键

阶段 03 建 Run 表时，`conversation_id` 是裸 UUID 列没有外键（Conversation 表那时还不存在）。现在补上。

修改 `app/models/run.py`：

```python
    conversation_id: uuid.UUID | None = Field(
        default=None, foreign_key="conversation.id", ondelete="SET NULL", index=True
    )
```

用 `SET NULL` 而不是 `CASCADE`：删除会话不应该删掉运行记录，运行记录是审计数据。

### 更新 `models/__init__.py`

导出全部新符号，加 rebuild。注意导入顺序：`conversation.py` 引用 `Agent`，所以要在 `agent.py` 之后导入。用 `TYPE_CHECKING` + 字符串引用后顺序其实不敏感，但 rebuild 必须在全部导入之后。

---

## 任务 2：迁移

```bash
cd backend
uv run alembic revision --autogenerate -m "Add conversation and message models"
```

人工检查：

- `conversation` 和 `message` 两张表都建了
- `conversation.thread_id` 有唯一索引
- **`run.conversation_id` 上新增了外键约束**，`ondelete` 是 `SET NULL`。autogenerate 可能检测不到这个变化（因为列本身没变），如果没生成就手工加：

  ```python
  op.create_foreign_key(
      "run_conversation_id_fkey",
      "run",
      "conversation",
      ["conversation_id"],
      ["id"],
      ondelete="SET NULL",
  )
  ```

  `downgrade()` 里对应 `op.drop_constraint(...)`。

- `message.role` 是 varchar 不是 enum

更新 `backend/tests/conftest.py` 的 teardown 元组：

```python
for model in (RunEvent, Run, ConvMessage, Conversation, AgentVersion, Agent, User):
    session.execute(delete(model))
```

**`Run` 必须在 `Conversation` 之前删**，否则 `SET NULL` 外键会阻止删除（实际上 SET NULL 不会阻止，但保持子表在前的规则更安全）。

---

## 任务 3：上下文裁剪与摘要

新建 `backend/app/agent/memory.py`。

### 3.1 消息与 LangChain 类型的互转

数据库里存的是 `ConvMessage`，LangGraph 用的是 `BaseMessage`。需要双向转换：

```python
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.models.conversation import ConvMessage, MessageRole


def to_langchain_message(msg: ConvMessage) -> BaseMessage:
    if msg.role == MessageRole.USER:
        return HumanMessage(content=msg.content)
    if msg.role == MessageRole.ASSISTANT:
        return AIMessage(content=msg.content, tool_calls=msg.tool_calls or [])
    if msg.role == MessageRole.TOOL:
        return ToolMessage(content=msg.content, tool_call_id=msg.tool_call_id or "")
    return SystemMessage(content=msg.content)


def from_langchain_message(
    msg: BaseMessage, *, conversation_id: uuid.UUID, seq: int, run_id: uuid.UUID | None
) -> ConvMessage:
    if isinstance(msg, HumanMessage):
        role = MessageRole.USER
    elif isinstance(msg, AIMessage):
        role = MessageRole.ASSISTANT
    elif isinstance(msg, ToolMessage):
        role = MessageRole.TOOL
    else:
        role = MessageRole.SYSTEM

    usage = getattr(msg, "usage_metadata", None) or {}
    return ConvMessage(
        conversation_id=conversation_id,
        seq=seq,
        role=role,
        content=_as_text(msg.content),
        tool_calls=getattr(msg, "tool_calls", []) or [],
        tool_call_id=getattr(msg, "tool_call_id", None),
        token_count=usage.get("output_tokens"),
        run_id=run_id,
    )


def _as_text(content: str | list[str | dict[str, Any]]) -> str:
    """
    BaseMessage.content 可能是字符串，也可能是多模态的内容块列表。
    统一成字符串存库，非文本块用占位符表示。
    """
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        else:
            parts.append("[non-text content]")
    return "".join(parts)
```

`_as_text` 不能省略。`BaseMessage.content` 的类型是 `str | list[...]`，直接当字符串用在 mypy strict 下过不了，运行时遇到多模态回复也会出错。

### 3.2 裁剪策略

```python
from langchain_core.messages import trim_messages

MAX_CONTEXT_MESSAGES = 20
SUMMARIZE_THRESHOLD = 30


def build_context_messages(
    *,
    history: list[ConvMessage],
    summary: str | None,
    system_prompt: str,
) -> list[BaseMessage]:
    """
    组装送给模型的消息列表。

    结构：[system_prompt + summary] + 最近 N 条消息
    """
    messages: list[BaseMessage] = []

    prompt_parts = [system_prompt] if system_prompt else []
    if summary:
        prompt_parts.append(
            f"Summary of earlier conversation:\n{summary}"
        )
    if prompt_parts:
        messages.append(SystemMessage(content="\n\n".join(prompt_parts)))

    recent = history[-MAX_CONTEXT_MESSAGES:]
    messages.extend(to_langchain_message(m) for m in recent)
    return messages
```

**先按消息条数裁剪，不按 token 数**。按 token 裁剪需要 tokenizer，会引入 `tiktoken` 依赖且对非 OpenAI 模型不准。条数裁剪简单可靠，对这个项目的规模够用。

如果后面确实需要按 token 裁剪，用 LangChain 的 `trim_messages(strategy="last", token_counter=chat_model)`，它会调模型的 `get_num_tokens_from_messages`。在本文档的"偏差记录"里写明改动原因。

### 3.3 摘要生成

```python
async def maybe_summarize(
    *,
    session: AsyncSession,
    conversation: Conversation,
    history: list[ConvMessage],
) -> None:
    """
    消息数超过阈值时，把早期消息压缩成摘要存到 conversation.summary。

    在每次 Run 结束后调用。摘要生成失败不影响主流程，只记日志。
    """
    if len(history) < SUMMARIZE_THRESHOLD:
        return

    cutoff = len(history) - MAX_CONTEXT_MESSAGES
    to_summarize = history[:cutoff]
    if conversation.summarized_up_to_seq is not None:
        to_summarize = [
            m for m in to_summarize if m.seq > conversation.summarized_up_to_seq
        ]
    if not to_summarize:
        return

    transcript = "\n".join(f"{m.role.value}: {m.content}" for m in to_summarize)
    prompt = (
        "Summarize the following conversation excerpt in at most 200 words. "
        "Keep facts, decisions and user preferences. Drop pleasantries.\n\n"
        f"{transcript}"
    )
    if conversation.summary:
        prompt = (
            f"Existing summary:\n{conversation.summary}\n\n"
            f"Extend it with the following new excerpt:\n\n{transcript}"
        )

    try:
        chat = get_provider().get_chat_model(model=settings.LLM_MODEL, temperature=0)
        response = await chat.ainvoke([HumanMessage(content=prompt)])
    except Exception:
        logger.warning("Failed to summarize conversation %s", conversation.id, exc_info=True)
        return

    conversation.summary = _as_text(response.content)[:8000]
    conversation.summarized_up_to_seq = to_summarize[-1].seq
    session.add(conversation)
    await session.commit()
```

要点：

- 摘要用 `settings.LLM_MODEL`（全局默认模型）而不是 Agent 配置的模型。摘要是平台行为不是 Agent 行为，而且用便宜的小模型就够
- `temperature=0`，摘要要稳定
- **失败只记日志不抛异常**。摘要是优化不是必需功能，它挂了对话还得能继续
- `summarized_up_to_seq` 记录已摘要到哪条，避免重复摘要同一批消息
- 用 `logging` 不要用 `print`（ruff T201 会报错）

---

## 任务 4：流式执行

### 4.1 改造 `execute_run` 为流式版本

在 `backend/app/services/run_service.py` 里新增一个流式版本，**保留阶段 03 的 `execute_run` 不动**（阶段 06 的 worker 还会用它）。

```python
async def stream_run(
    *,
    session: AsyncSession,
    run: Run,
    snapshot: AgentSnapshot,
    context_messages: list[BaseMessage],
) -> AsyncGenerator[tuple[RunEventType, dict[str, Any]]]:
    """
    执行 Run 并以生成器形式逐个产出事件。

    与 execute_run 的区别：用 astream_events 采集节点级事件和 token 分片，
    边产出边落库。调用方负责把事件序列化成 SSE。
    """
```

核心是用 `astream_events` 替代 `ainvoke`：

```python
    app = compile_agent_graph()
    final_state: dict[str, Any] | None = None

    async for event in app.astream_events(initial_state, version="v2"):
        kind = event["event"]

        if kind == "on_chain_start" and event["name"] in KNOWN_NODE_NAMES:
            yield (RunEventType.NODE_STARTED, {"node": event["name"]})

        elif kind == "on_chain_end" and event["name"] in KNOWN_NODE_NAMES:
            yield (RunEventType.NODE_FINISHED, {"node": event["name"]})

        elif kind == "on_chat_model_stream":
            chunk = event["data"]["chunk"]
            text = _as_text(chunk.content)
            if text:
                yield (RunEventType.MODEL_CHUNK, {"text": text})

        elif kind == "on_chain_end" and event["name"] == "LangGraph":
            final_state = event["data"]["output"]
```

要点：

- `version="v2"` 必须显式传。v1 的事件结构不同
- `astream_events` 会产出**大量**事件（每个 runnable 的 start/end/stream），必须用 `event["name"]` 过滤。定义一个 `KNOWN_NODE_NAMES = {"call_model"}` 集合，后续阶段加节点时往里加
- 最外层图的名字是 `"LangGraph"`，它的 `on_chain_end` 携带最终状态
- **模型必须以 streaming 模式创建**，否则不会产出 `on_chat_model_stream`。`nodes.py` 里的 `call_model` 需要改造：

  ```python
  chat = provider.get_chat_model(..., streaming=True)
  ```

  LangChain 的 `astream_events` 对非流式模型也能工作，但只在最后给一个完整块，体验上就不是流式了。**把 `streaming=True` 写死在 `call_model` 里**，非流式场景（阶段 06 的 worker、阶段 09 的评测）也用流式调用没有坏处，token 统计靠 `stream_usage=True` 保证。

- token 分片事件**不要逐条落库**。一次回复可能有几百个分片，写几百行 `run_event` 是浪费。`MODEL_CHUNK` 只推给前端，不进 `run_event` 表。落库的事件类型限定为 `run_started` / `node_started` / `node_finished` / `run_finished` / `run_failed`
- 生成器里的异常处理与阶段 03 一致：`_fail_run` 兜底，并且要 `yield` 一个 `RUN_FAILED` 事件让前端知道

### 4.2 SSE 端点

在 `backend/app/api/routes/conversations.py` 里加（放在 conversations 路由下，因为流式执行总是发生在某个会话里）：

```python
@router.post("/{conversation_id}/stream")
async def stream_message(
    *,
    session: AsyncSessionDep,
    current_user: CurrentUser,
    conversation_id: uuid.UUID,
    body: StreamMessageRequest,
) -> StreamingResponse:
    """
    Send a message to the conversation and stream the agent response.

    Returns a text/event-stream. Not usable via the generated client because
    EventSource cannot send an Authorization header; see docs/dev/04.
    """
```

`StreamingResponse` 从 `fastapi.responses` 引入。返回时的 headers：

```python
    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
```

`X-Accel-Buffering: no` 是必需的。没有它，Traefik 或 nginx 会缓冲响应，前端要等全部内容到齐才收到，流式效果完全失效。这是最常见的"代码没错但没效果"的原因。

### 4.3 SSE 事件格式契约

这是前后端手工契约，**两边必须保持一致**。

```
event: <event_type>\n
data: <json>\n
\n
```

事件类型与 payload：

| event | data | 说明 |
|-------|------|------|
| `run_started` | `{"run_id": "<uuid>"}` | 第一个事件，前端拿到 run_id 用于后续跳转 |
| `node_started` | `{"node": "call_model"}` | 节点开始 |
| `node_finished` | `{"node": "call_model"}` | 节点结束 |
| `model_chunk` | `{"text": "部分"}` | 回复分片，前端追加到当前气泡 |
| `run_finished` | `{"run_id": "...", "message_id": "...", "prompt_tokens": 10, "completion_tokens": 5, "cost_usd": "0.000012"}` | 成功结束 |
| `run_failed` | `{"run_id": "...", "error": "..."}` | 失败结束 |

序列化辅助函数：

```python
def _sse(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"
```

`default=str` 处理 UUID 和 Decimal，不加会报序列化错误。

### 4.4 流式端点的完整流程

1. 校验 conversation 存在且归属当前用户
2. 查 conversation 关联的 agent，取它的**最新非 draft 版本**。没有已发布版本就返回 400 提示先发布
3. 用户消息立刻落库（`ConvMessage`，role=user，seq 递增）
4. 建 Run 记录（`trigger=playground`，`conversation_id` 填上）
5. 读历史消息 + summary，调 `build_context_messages` 组装上下文
6. `async for` 消费 `stream_run` 产出的事件，逐个 `yield _sse(...)`
7. 结束后把助手回复落库为 `ConvMessage`（role=assistant），更新 `conversation.updated_at`
8. 调 `maybe_summarize`
9. 会话标题为空时，用第一条用户消息的前 50 字符自动填充

### 4.5 客户端断连的处理

用户关闭页面或点"停止"时，SSE 连接断开。这时生成器会在下一次 `yield` 时抛 `asyncio.CancelledError`（或者 Starlette 内部处理掉）。

必须保证 Run 不会卡在 `running`。在生成器外层包一个 `try/finally`：

```python
    try:
        async for event_type, payload in stream_run(...):
            yield _sse(event_type.value, payload)
    except asyncio.CancelledError:
        await _cancel_run(session=session, run=run)
        raise
    finally:
        await session.close()
```

`_cancel_run` 把 Run 标记为 `RunStatus.CANCELLED` 并记一条 `run_finished` 事件。**`CancelledError` 捕获后必须重新 raise**，否则 asyncio 的取消机制会出问题。

---

## 任务 5：Conversation 路由

`backend/app/api/routes/conversations.py`，`tags=["conversations"]`。

| 方法 | 路径 | response_model | session | 说明 |
|------|------|----------------|---------|------|
| GET | `/conversations/` | `ConversationsPublic` | sync | 列表，可按 `agent_id` 过滤 |
| POST | `/conversations/` | `ConversationPublic` | sync | 创建空会话 |
| GET | `/conversations/{id}` | `ConversationPublic` | sync | 详情 |
| PATCH | `/conversations/{id}` | `ConversationPublic` | sync | 改标题 |
| DELETE | `/conversations/{id}` | `Message` | sync | 删除（级联删消息） |
| GET | `/conversations/{id}/messages` | `ConvMessagesPublic` | sync | 消息列表，按 seq 升序 |
| POST | `/conversations/{id}/stream` | （SSE） | **async** | 流式对话，见任务 4 |

`message_count` 在列表接口里用聚合查询批量填充，做法与阶段 02 的 `latest_version_number` 一致。

`StreamMessageRequest` 定义在 `app/models/conversation.py`：

```python
class StreamMessageRequest(SQLModel):
    message: str = Field(min_length=1, max_length=20000)
```

别忘了在 `app/api/main.py` 注册路由。

---

## 任务 6：前端 Playground

这是本阶段的展示重点，做扎实一点。

### 6.1 SSE 解析工具

新建 `frontend/src/lib/sse.ts`。这段代码是本阶段唯一需要手写的底层逻辑：

```typescript
export interface SSEEvent {
  event: string
  data: unknown
}

export async function* streamSSE(
  url: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<SSEEvent> {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${localStorage.getItem("access_token") ?? ""}`,
    },
    body: JSON.stringify(body),
    signal,
  })

  if (!response.ok || !response.body) {
    throw new Error(`Stream failed: ${response.status}`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // SSE 以空行分隔事件
    const chunks = buffer.split("\n\n")
    buffer = chunks.pop() ?? ""

    for (const chunk of chunks) {
      let eventName = "message"
      const dataLines: string[] = []
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) {
          eventName = line.slice(6).trim()
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trim())
        }
      }
      if (dataLines.length > 0) {
        yield { event: eventName, data: JSON.parse(dataLines.join("\n")) }
      }
    }
  }
}
```

关键点：

- **必须用 `buffer` 累积**。网络分片不保证与 SSE 事件边界对齐，一个事件可能跨两次 `read()`，也可能一次 `read()` 里有多个事件。不做缓冲会随机丢事件或 JSON 解析失败
- `buffer = chunks.pop()`：最后一段可能不完整，留到下一轮
- token 从 `localStorage` 直接读，与 `main.tsx` 里 `client.setConfig` 的 auth 逻辑保持一致
- `signal` 用于用户点"停止"时中断

base URL 用 `import.meta.env.VITE_API_URL ?? ""`，与生成客户端的配置一致。

### 6.2 Playground 页面

新建 `frontend/src/routes/_layout/playground.tsx`。

布局：左侧会话列表（窄栏），右侧对话区。

**顶部控制栏**：Agent 选择器（`Select`，选项来自 `readAgents`，只列有已发布版本的）、"New conversation" 按钮。

**对话区**：消息气泡列表。user 靠右，assistant 靠左。底部输入框 + 发送按钮。

**执行状态指示**：流式过程中在当前 assistant 气泡上方显示当前节点名（来自 `node_started` 事件），例如一个小的 "call_model" 标签 + 转圈图标。这个细节能直观体现"这是个状态图在跑"而不是简单的聊天接口。

**右侧或底部的事件面板**（可折叠）：实时显示收到的事件流，以及结束后的 token 和费用。

### 6.3 流式状态管理

不要用 TanStack Query 管流式状态，它的模型不适合。用本地 `useState`：

```typescript
const [messages, setMessages] = useState<ChatMessage[]>([])
const [streamingText, setStreamingText] = useState("")
const [currentNode, setCurrentNode] = useState<string | null>(null)
const [isStreaming, setIsStreaming] = useState(false)
const abortRef = useRef<AbortController | null>(null)

async function send(text: string) {
  setMessages((prev) => [...prev, { role: "user", content: text }])
  setStreamingText("")
  setIsStreaming(true)
  abortRef.current = new AbortController()

  try {
    for await (const evt of streamSSE(
      `${baseURL}/api/v1/conversations/${conversationId}/stream`,
      { message: text },
      abortRef.current.signal,
    )) {
      switch (evt.event) {
        case "model_chunk":
          setStreamingText((prev) => prev + (evt.data as { text: string }).text)
          break
        case "node_started":
          setCurrentNode((evt.data as { node: string }).node)
          break
        case "run_finished":
          // 把 streamingText 固化成一条 assistant 消息
          break
        case "run_failed":
          showErrorToast((evt.data as { error: string }).error)
          break
      }
    }
  } finally {
    setIsStreaming(false)
    setCurrentNode(null)
    queryClient.invalidateQueries({ queryKey: ["conversations", conversationId, "messages"] })
  }
}
```

`run_finished` 时把 `streamingText` 追加到 `messages` 并清空，然后 invalidate 消息列表 query 让它从服务端拉一遍（拿到真实的 message id 和 token 数）。

刷新页面后靠 `readMessages` 恢复历史，这是"刷新后历史还在"这条验收标准的实现方式。

### 6.4 侧栏注册

```typescript
{ icon: MessageSquare, title: "Playground", path: "/playground" },
```

放在 Agents 之后、Runs 之前。

---

## 任务 7：测试

### 7.1 后端单元测试

`backend/tests/agent/test_memory.py`：

- `to_langchain_message` / `from_langchain_message` 四种 role 往返一致
- `_as_text` 处理字符串、内容块列表、含非文本块三种情况
- `build_context_messages` 在历史超过 `MAX_CONTEXT_MESSAGES` 时只保留最近 N 条
- `summary` 非空时被拼进第一条 SystemMessage
- `system_prompt` 和 `summary` 都为空时不产生 SystemMessage
- `maybe_summarize` 在消息数不足阈值时直接返回不调模型（用假模型的调用计数断言）
- `maybe_summarize` 在模型抛异常时不抛出，`conversation.summary` 保持原值

### 7.2 SSE 端点测试

`backend/tests/api/routes/test_conversations.py`。

`TestClient` 支持读流式响应：

```python
with client.stream(
    "POST",
    f"{settings.API_V1_STR}/conversations/{conv_id}/stream",
    headers=normal_user_token_headers,
    json={"message": "hello"},
) as response:
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    raw = "".join(response.iter_text())

events = _parse_sse(raw)   # 在 tests/utils/sse.py 里写一个解析辅助
assert events[0]["event"] == "run_started"
assert events[-1]["event"] == "run_finished"
assert any(e["event"] == "model_chunk" for e in events)
```

在 `backend/tests/utils/sse.py` 里写一个 `_parse_sse` 辅助函数，逻辑与前端的解析一致。

用假模型时要让它产出多个分片，这样才能验证流式。`GenericFakeChatModel` 会把消息内容按字符流式输出，够用。

覆盖场景：

- 流式返回的事件序列符合契约（首个是 `run_started`，末尾是 `run_finished`，中间有 `model_chunk`）
- 流结束后 `message` 表里多了两条（user 和 assistant），seq 连续
- 流结束后 `run` 表里的记录 `status == succeeded` 且 `conversation_id` 正确
- 第二轮对话时上下文包含第一轮（用假模型记录收到的消息列表来断言）
- `run_event` 表里**没有** `model_chunk` 类型的记录（验证分片没落库）
- 别人的会话返回 403
- agent 没有已发布版本时返回 400
- 模型抛异常时返回的最后一个事件是 `run_failed`，且 Run 状态是 `failed`

### 7.3 Playwright

`frontend/tests/playground.spec.ts`：

1. 进 `/playground`，选一个 agent，新建会话
2. 输入消息发送，等待 assistant 气泡出现且内容非空
3. 刷新页面，历史消息仍在
4. 发第二条消息，两轮消息都在

E2E 会调真实模型，所以 CI 里跑不了（没有 API key）。**用 `test.skip` 配合环境变量控制**：

```typescript
test.skip(!process.env.LLM_API_KEY, "Requires LLM_API_KEY")
```

在 `compose.override.yml` 的 playwright 服务里透传 `LLM_API_KEY`，本地配了 key 就跑，CI 里跳过。

---

## 阶段验收清单

- [x] `uv run alembic check` 输出 `No new upgrade operations detected.`
- [x] `docker compose exec db psql -U postgres -d app -c "\d run"` 里 `conversation_id` 有指向 conversation 的外键且 `ON DELETE SET NULL`
- [x] `uv run pytest tests/ -v` 全绿
- [x] 测试不产生真实网络请求（把 `LLM_BASE_URL` 改成无效地址后测试仍全绿）
- [x] `cd backend && bash scripts/lint.sh` 全绿
- [x] `cd backend && bash scripts/test.sh` 全绿
- [x] `bun run lint` 通过
- [x] **手动验证流式效果**：Playground 里发一条消息，回复是**逐字出现**的而不是一次性整段出现。如果是整段出现，检查 `X-Accel-Buffering` 头和 `streaming=True`
- [x] 手动验证：执行中能看到 `call_model` 节点指示器
- [x] 手动验证：刷新页面后历史对话完整保留
- [x] 手动验证多轮上下文：第一轮说"我叫张三"，第二轮问"我叫什么"，回复正确
- [x] 手动验证断连：发消息后立刻关闭浏览器标签，检查数据库里那条 Run 的状态是 `cancelled` 而不是 `running`
- [x] 手动验证摘要：连发 30 条消息后查 `SELECT summary, summarized_up_to_seq FROM conversation`，summary 非空
- [x] `SELECT status, count(*) FROM run GROUP BY status;` 没有卡在 `running` 的记录
- [x] `SELECT count(*) FROM run_event WHERE event_type = 'model_chunk';` 返回 0
- [x] 在 Docker Compose 环境下（`docker compose watch`）也验证一次流式效果，确认 Traefik 没有缓冲

---

## 常见坑

**流式没效果，回复一次性整段出现**
三个可能原因，按顺序排查：`X-Accel-Buffering: no` 头没加；`call_model` 里模型没设 `streaming=True`；前端解析逻辑没有边缓冲边 yield。

**前端随机丢事件或 JSON 解析报错**
SSE 解析没做缓冲。网络分片与事件边界不对齐。必须用任务 6.1 的 buffer 写法。

**`astream_events` 产出海量无关事件**
必须按 `event["name"]` 过滤。不过滤的话一次简单对话会产出上百个事件。

**`astream_events(version="v2")` 报参数错误**
LangGraph 版本太老。检查装的版本，`v2` 事件格式在较新版本才支持。如果只支持 v1，事件字段名不同（`event["event"]` 的取值和 `data` 结构都有差异），需要相应调整并在偏差记录里写明。

**Run 卡在 `running` 且没有任何错误**
客户端断连没被处理。确认 `try/except asyncio.CancelledError` 在位且 `_cancel_run` 被调用。

**`ConvMessage` 和 `Message` 命名冲突**
见任务 1 的说明。对话消息类叫 `ConvMessage`，表名是 `message`。

**`message.seq` 冲突**
并发在同一会话发消息会撞 seq。单用户场景概率极低，但要在写入前用 `SELECT max(seq)` 查当前值而不是缓存一个计数器。如果要彻底解决，给 `(conversation_id, seq)` 加唯一约束并在冲突时重试。

**摘要把对话搞乱**
`maybe_summarize` 必须在 Run 结束**之后**调用，不能在组装上下文之前调。否则用户刚说的话就被摘要掉了。

**`Decimal` 在 SSE 的 JSON 里报序列化错误**
`json.dumps(data, default=str)`，`default=str` 不能省。

**测试里 `client.stream` 卡住不返回**
`TestClient` 对 `StreamingResponse` 的处理要求生成器必须正常结束。如果生成器里有未捕获异常，流会挂住。确认 `finally` 块里没有会抛异常的代码。

---

## 偏差记录

- 用户已确认：`ConvMessage.content` 是字符串长度规则的例外，显式使用 PostgreSQL `TEXT`，不设字符上限；输入仍限制为 20000 字符。
- 配套文件范围：为满足既有级联关系、CRUD 分层与验证要求，修改 `models/user.py`、`models/agent.py`，新增 `crud/conversation.py`、`tests/utils/conversation.py`，并按需调整 Run 路由的 conversation_id 校验及相关测试，避免补外键后无效 ID 变成数据库错误。

- `astream_events` 使用的 version：`v2`；保留阶段 03 的 `execute_run`，仅流式路径新增节点事件。
- 上下文裁剪策略（条数 / token）：保留最近 20 条消息；达到 30 条后摘要前面的消息，按 `summarized_up_to_seq` 增量推进。摘要提交前重新加锁校验进度，避免慢请求覆盖较新的摘要。
- 其它偏差：
  - `message.role` 显式使用 `String(32)`；消息正文显式使用 `Text`；增加 `(conversation_id, seq)` 唯一约束。发送消息先锁定会话，已有 queued/running Run 时返回 409，执行期间删除会话也返回 409。
  - 助手消息与 succeeded Run 在同一事务提交后才发送 `run_finished`，保证客户端收到终止事件时可以读取完整历史。断连清理使用屏蔽取消的作用域，避免 Starlette 的取消作用域打断取消状态落库；生成器提前关闭也会清理 Run。
  - 流式响应使用独立 AsyncSession，避免依赖生命周期提前关闭会话；组装上下文后将图中的 system_prompt 置空，避免重复注入系统提示词。
  - 新增 `backend/tests/agent/test_stream.py` 验证取消/提前关闭，以及 `frontend/tests/sse.spec.ts` 验证 UTF-8 字节拆分、跨边界事件、多行 data、HTTP 错误与 reader 释放。Playground 通过 URL search 保留选中的会话，历史消息分页读全；事件面板只保留非 token 事件，token 直接显示在气泡。
  - Windows 无可用 bash，使用 lint/test/generate-client 脚本内部等价命令。客户端由 OpenAPI 生成器生成；生成器输出的空白行未手工修改。
  - 2026-09-16 验证：独立数据库 `agenthub_phase04_test` 中完整后端测试 106 项通过，设置 `LLM_BASE_URL=http://127.0.0.1:1/v1` 后依然通过，coverage 89%；mypy、ty、ruff、Biome、前端生产构建通过。迁移在独立测试库 downgrade/upgrade 往返成功，实际 app 库 Alembic check 无差异，确认 Run 外键为 `ON DELETE SET NULL`。
  - 浏览器验证：登录、Agent 管理、Run 成功/失败、Playground 真实多轮流式、SSE 解析共 7 项 Playwright 通过。通过 DOM 变化记录确认回复持续增长，刷新后正文一致，第二轮正确回答“张三”，关页后轮询确认 Run 为 cancelled；查看截图确认页面布局。真实模型测试受 `LLM_API_KEY` 环境变量控制。
  - 真实摘要验证：15 轮共 30 条消息，会话 `90d5c41d-dc7d-4f60-9a29-e7054baea822` 的摘要长 190 字符，`summarized_up_to_seq=10`。验证数据保留在本地 app 数据库。数据库既有 collation 版本告警未修改。
  - Docker 验证：镜像构建成功，`docker compose watch --no-up backend` 运行期间，经 `http://localhost` 的 Traefik 发起真实 SSE 请求，Run `838b4efa-b7c6-467a-9a67-ee8f9da80785` 收到 254 个分片，首/末分片分别在 5.833/6.616 秒到达，输入/输出 Token 为 56/977，费用为 $0.001189；断开代理连接后 Run `2091a4b0-c681-4c29-a49e-226b0436f6c8` 为 cancelled。该演示会话 `582645e5-66c8-4e51-80b0-af8ff2c247a3` 保留供复核。
