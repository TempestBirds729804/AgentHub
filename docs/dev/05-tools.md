# 05 — 工具系统

## 前置依赖

阶段 [04-conversation-streaming.md](04-conversation-streaming.md) 的验收清单全部通过。特别确认：

- Playground 里流式对话可用，回复逐字出现
- `run_event` 表里没有 `model_chunk` 记录
- 多轮上下文生效

## 本阶段目标

让 Agent 能调工具，从"会聊天"变成"能干活"：

- `Tool` 和 `AgentToolBinding` 两张表
- 三种工具类型的执行器：Function（内置白名单）、HTTP、MCP
- JSON Schema 参数校验、超时控制、SSRF 防护
- 状态图从单节点变成带条件边的循环图
- 前端工具管理页、Agent 的工具绑定、Playground 里的工具调用可视化

## 本阶段不做什么

不做人工审批（阶段 07，本阶段只在 Tool 上留 `requires_approval` 字段但不实现拦截逻辑）、不做异步执行。

---

## 先读这一条：Function Tool 不执行用户提交的代码

"Python Function Tool" 有两种可能的含义，本项目**明确只做后者**：

1. 用户在 UI 上粘贴 Python 代码，平台执行它 —— **不做**
2. 平台内置一组安全的 Python 函数，用户选择启用哪些并配置参数 —— **做这个**

原因是执行任意用户代码需要真正的沙箱（容器隔离、seccomp、资源限制），这超出项目范围，做一半的沙箱比不做更危险。

所以 Function Tool 的实现是一个**内置函数注册表**：代码里用装饰器注册若干工具函数，用户创建 Tool 记录时从注册表里选一个 `function_name`。用户无法注入代码。

这个边界要在 README 和 UI 文案里说清楚，它是一个明确的设计选择而不是缺失。想扩展工具能力的用户走 HTTP Tool 或 MCP Tool，那两种天然在进程外。

---

## 任务 1：Tool 与绑定模型

新建 `backend/app/models/tool.py`：

```python
import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

from app.models.base import get_datetime_utc

if TYPE_CHECKING:
    from app.models.agent import AgentVersion
    from app.models.user import User


class ToolType(str, enum.Enum):
    FUNCTION = "function"
    HTTP = "http"
    MCP = "mcp"


class ToolBase(SQLModel):
    # 这个 name 会作为工具名传给模型，必须符合 OpenAI 的工具名规则：
    # 只允许字母、数字、下划线和连字符，最长 64。
    name: str = Field(min_length=1, max_length=64, regex=r"^[a-zA-Z0-9_-]+$")
    description: str = Field(min_length=1, max_length=1000)
    tool_type: ToolType
    parameters_schema: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    config: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    requires_approval: bool = False
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    is_active: bool = True


class ToolCreate(ToolBase):
    pass


class ToolUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    parameters_schema: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    requires_approval: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)
    is_active: bool | None = None
    # tool_type 不可改：改类型等于换了个工具，让用户新建一个


class Tool(ToolBase, table=True):
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

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


class ToolPublic(ToolBase):
    id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ToolsPublic(SQLModel):
    data: list[ToolPublic]
    count: int


class AgentToolBinding(SQLModel, table=True):
    __tablename__ = "agent_tool_binding"
    __table_args__ = (UniqueConstraint("agent_version_id", "tool_id"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_version_id: uuid.UUID = Field(
        foreign_key="agent_version.id", nullable=False, ondelete="CASCADE", index=True
    )
    tool_id: uuid.UUID = Field(
        foreign_key="tool.id", nullable=False, ondelete="CASCADE", index=True
    )
    config_override: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
```

### 关于工具绑定在哪一层

绑定表指向 `agent_version_id` 而不是 `agent_id`。这是版本机制的必然要求：v1 用了三个工具，v2 换成两个，历史 Run 必须能还原出当时的工具集。

但用户编辑的是 Agent 而不是 Version。所以 Agent 上也需要一份"当前选中的工具"。做法是**把它放进 Agent 的配置里而不是再建一张表**：

`app/models/agent.py` 的 `AgentBase` 追加：

```python
    tool_ids: list[uuid.UUID] = Field(default_factory=list, sa_type=JSONB)
```

发布版本时，`publish_agent_version` 根据 `agent.tool_ids` 创建对应的 `AgentToolBinding` 记录。这样：

- 编辑态是一个简单的 id 列表（JSONB），改起来方便
- 发布态是规范化的关联表，可以 join 查询，也能带 `config_override`

`config` 与 `config_override` 的合并规则：`{**tool.config, **binding.config_override}`，绑定级覆盖工具级。

### `parameters_schema` 的格式

标准 JSON Schema 的 object 形式，与 OpenAI function calling 的参数格式一致：

```json
{
  "type": "object",
  "properties": {
    "city": { "type": "string", "description": "City name in English" },
    "days": { "type": "integer", "description": "Forecast days", "default": 3 }
  },
  "required": ["city"]
}
```

Function Tool 的 schema 由注册表自动生成（见任务 3），用户不用填。HTTP 和 MCP Tool 需要用户提供或自动发现。

### 更新 `AgentSnapshot`

`app/agent/snapshot.py` 追加：

```python
    tool_ids: list[uuid.UUID] = Field(default_factory=list)
```

**必须给默认值**，否则读取阶段 02/03/04 期间创建的旧版本快照会校验失败。

### 更新 `models/__init__.py` 和 `conftest.py`

导出新符号 + rebuild。teardown 元组更新为：

```python
for model in (
    RunEvent, Run, ConvMessage, Conversation,
    AgentToolBinding, Tool, AgentVersion, Agent, User,
):
```

`AgentToolBinding` 必须在 `Tool` 和 `AgentVersion` 之前。

---

## 任务 2：迁移

```bash
cd backend
uv run alembic revision --autogenerate -m "Add tool and agent tool binding models"
```

人工检查：`tool` 表有 `(owner_id, name)` 唯一约束；`agent_tool_binding` 有 `(agent_version_id, tool_id)` 唯一约束；`agent.tool_ids` 列是 JSONB 且默认值合理；枚举是 varchar。

`agent.tool_ids` 是给已有表加列，**必须有 server_default 或允许 null**，否则已有行会违反 NOT NULL。检查迁移文件里是不是：

```python
op.add_column(
    "agent",
    sa.Column("tool_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
)
```

如果 autogenerate 生成的没有 `server_default`，手工加上。

---

## 任务 3：工具执行器

### 3.1 统一抽象

新建 `backend/app/agent/tools/base.py`：

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolResult:
    """工具执行结果。失败也返回 ToolResult 而不是抛异常，见下方说明。"""

    ok: bool
    content: str
    error: str | None = None
    duration_ms: int = 0


class ToolExecutor(ABC):
    """一种工具类型的执行器。"""

    @abstractmethod
    async def execute(
        self,
        *,
        config: dict[str, Any],
        arguments: dict[str, Any],
        timeout_seconds: int,
    ) -> ToolResult:
        """执行工具。绝不抛异常，失败时返回 ok=False 的 ToolResult。"""

    @abstractmethod
    def validate_config(self, config: dict[str, Any]) -> None:
        """校验配置，不合法时抛 ValueError。在创建 Tool 时调用。"""
```

**工具执行失败不抛异常，而是返回 `ok=False`**。这是关键设计：工具失败是 Agent 应该能感知并处理的事情（模型可以换个参数重试或换个工具），不是整个 Run 该崩掉的事情。失败信息会作为 `ToolMessage` 送回模型。

只有"配置错误"这种确定性问题才在 `validate_config` 里抛异常，而且是在创建工具时就抛，不到运行期。

### 3.2 Function 执行器

新建 `backend/app/agent/tools/function.py`。

**注册表机制**：

```python
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

FunctionHandler = Callable[..., Awaitable[str]]


class RegisteredFunction(BaseModel):
    name: str
    description: str
    args_model: type[BaseModel]
    handler: FunctionHandler

    model_config = {"arbitrary_types_allowed": True}


_REGISTRY: dict[str, RegisteredFunction] = {}


def register_function(
    *, name: str, description: str, args_model: type[BaseModel]
) -> Callable[[FunctionHandler], FunctionHandler]:
    def decorator(handler: FunctionHandler) -> FunctionHandler:
        _REGISTRY[name] = RegisteredFunction(
            name=name,
            description=description,
            args_model=args_model,
            handler=handler,
        )
        return handler

    return decorator


def list_functions() -> list[RegisteredFunction]:
    return list(_REGISTRY.values())


def get_function(name: str) -> RegisteredFunction | None:
    return _REGISTRY.get(name)
```

**内置函数**放在 `backend/app/agent/tools/builtins.py`。第一版实现这三个就够：

```python
class CalculatorArgs(BaseModel):
    expression: str = Field(description="Arithmetic expression, e.g. '2 * (3 + 4)'")


@register_function(
    name="calculator",
    description="Evaluate a basic arithmetic expression. Supports + - * / ( ) and numbers only.",
    args_model=CalculatorArgs,
)
async def calculator(expression: str) -> str:
    return str(_safe_eval_arithmetic(expression))


class CurrentTimeArgs(BaseModel):
    timezone: str = Field(default="UTC", description="IANA timezone name")


@register_function(
    name="current_time",
    description="Get the current date and time in a given timezone.",
    args_model=CurrentTimeArgs,
)
async def current_time(timezone: str = "UTC") -> str:
    return datetime.now(ZoneInfo(timezone)).isoformat()
```

`_safe_eval_arithmetic` **绝对不能用 `eval()`**。用 `ast` 模块解析并只允许白名单节点：

```python
import ast
import operator

_ALLOWED_OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}


def _safe_eval_arithmetic(expression: str) -> float:
    if len(expression) > 200:
        raise ValueError("Expression too long")

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
            left, right = _eval(node.left), _eval(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e6):
                raise ValueError("Exponent out of allowed range")
            return _ALLOWED_OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -_eval(node.operand)
        raise ValueError(f"Unsupported expression element: {type(node).__name__}")

    return _eval(ast.parse(expression, mode="eval").body)
```

`ast.Pow` 的范围限制不能省：`2 ** 10000000` 会让进程卡死并吃满内存，这是真实的 DoS 向量。

**执行器实现**：

```python
class FunctionToolExecutor(ToolExecutor):
    def validate_config(self, config: dict[str, Any]) -> None:
        name = config.get("function_name")
        if not name:
            raise ValueError("Function tool requires config.function_name")
        if get_function(name) is None:
            available = ", ".join(f.name for f in list_functions())
            raise ValueError(
                f"Unknown function '{name}'. Available: {available}"
            )

    async def execute(
        self, *, config: dict[str, Any], arguments: dict[str, Any], timeout_seconds: int
    ) -> ToolResult:
        started = time.perf_counter()
        fn = get_function(config["function_name"])
        if fn is None:
            return ToolResult(ok=False, content="", error="Function not registered")
        try:
            parsed = fn.args_model.model_validate(arguments)
            result = await asyncio.wait_for(
                fn.handler(**parsed.model_dump()), timeout=timeout_seconds
            )
            return ToolResult(
                ok=True,
                content=str(result),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except TimeoutError:
            return ToolResult(
                ok=False, content="", error=f"Timed out after {timeout_seconds}s",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            return ToolResult(
                ok=False, content="", error=str(exc)[:1000],
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
```

### 3.3 HTTP 执行器

新建 `backend/app/agent/tools/http.py`。

配置格式：

```json
{
  "method": "GET",
  "url": "https://api.example.com/weather/{city}",
  "headers": { "X-API-Key": "..." },
  "query_from_args": ["days"],
  "body_from_args": []
}
```

`url` 里的 `{city}` 用参数值替换（路径参数），`query_from_args` 列出的参数进 query string，`body_from_args` 列出的进 JSON body。

**SSRF 防护是必需的**，否则用户能让平台去访问内网服务和云元数据端点：

```python
import ipaddress
import socket
from urllib.parse import urlparse

BLOCKED_HOSTS = {"localhost", "metadata.google.internal"}


def _assert_url_allowed(url: str) -> None:
    """
    阻止访问内网地址。防止用户把平台当成 SSRF 跳板去打内网服务
    或云厂商的元数据端点（169.254.169.254）。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http and https URLs are allowed")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host")
    if host.lower() in BLOCKED_HOSTS:
        raise ValueError(f"Host '{host}' is not allowed")

    # 解析域名后检查所有 IP，防止 DNS 指向内网
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve host '{host}'") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise ValueError(
                f"Host '{host}' resolves to a non-public address ({ip})"
            )
```

注意几点：

- 必须解析域名后检查 IP，光检查主机名字符串挡不住指向 `127.0.0.1` 的域名
- `is_link_local` 覆盖了 `169.254.169.254`（AWS/GCP 元数据端点），这条最重要
- 这个检查有 TOCTOU 窗口（检查后 DNS 可能变化），彻底解决需要自定义 socket 连接。对本项目来说当前强度够用，在代码注释里写明这个已知限制
- **`validate_config` 和 `execute` 里都要调**。只在创建时检查，用户可以改配置绕过；只在执行时检查，错误反馈太晚

本地开发时需要测试指向 localhost 的工具，加一个开关：`settings.ALLOW_PRIVATE_TOOL_URLS: bool = False`，在 `.env` 里本地设 true。**`compose.deploy.yml` 里绝对不能开。**

执行用 `httpx.AsyncClient`：

```python
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            response = await client.request(method, url, headers=headers, params=params, json=body)
```

`follow_redirects=False` 是必需的。开了跟随重定向，SSRF 检查就被绕过了（公网 URL 302 到内网地址）。

响应内容要截断，模型的 context window 有限：

```python
MAX_RESPONSE_CHARS = 20_000
content = response.text[:MAX_RESPONSE_CHARS]
if len(response.text) > MAX_RESPONSE_CHARS:
    content += f"\n\n[truncated, total {len(response.text)} chars]"
```

非 2xx 状态码返回 `ok=False` 但把响应体放进 `error`，模型能看到具体错误信息。

### 3.4 MCP 执行器

新建 `backend/app/agent/tools/mcp.py`。

先装依赖：

```bash
cd backend
uv add "langchain-mcp-adapters"
```

如果这个包装不上或者与当前 LangChain 版本不兼容，退化方案是直接用官方 `mcp` Python SDK 手工包一层。在偏差记录里写明选了哪个。

配置格式（只支持 stdio 和 SSE 两种传输）：

```json
{
  "transport": "sse",
  "url": "http://localhost:3001/sse",
  "tool_name": "search_docs"
}
```

或者：

```json
{
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
  "tool_name": "read_file"
}
```

**stdio 传输有明确的安全风险**：它会在后端进程里 spawn 子进程，`command` 由用户提供就等于任意命令执行。处理方式：

- `validate_config` 里校验 `command` 在白名单里（`{"npx", "uvx", "python"}`），且 `args` 不含 shell 元字符
- 用 `settings.ALLOW_STDIO_MCP_TOOLS: bool = False` 控制整个 stdio 传输是否启用，默认关闭
- **第一版建议只实现 SSE 传输**，stdio 的执行器直接返回 `ok=False` 说"未启用"。SSE 足够演示 MCP 能力，而且没有命令注入面

MCP 连接不要每次调用都重建。用一个带 TTL 的连接缓存，key 是配置的 URL。连接失败时返回 `ok=False`。

### 3.5 执行器注册

新建 `backend/app/agent/tools/registry.py`：

```python
from app.agent.tools.base import ToolExecutor
from app.agent.tools.function import FunctionToolExecutor
from app.agent.tools.http import HttpToolExecutor
from app.agent.tools.mcp import McpToolExecutor
from app.models.tool import ToolType

_EXECUTORS: dict[ToolType, ToolExecutor] = {
    ToolType.FUNCTION: FunctionToolExecutor(),
    ToolType.HTTP: HttpToolExecutor(),
    ToolType.MCP: McpToolExecutor(),
}


def get_executor(tool_type: ToolType) -> ToolExecutor:
    return _EXECUTORS[tool_type]
```

`app/agent/tools/__init__.py` 里要 `import app.agent.tools.builtins`，否则装饰器不会执行，注册表是空的。这是个容易漏的点，加个注释说明。

---

## 任务 4：Tool 转 LangChain Tool

模型需要以 LangChain `BaseTool` 的形式看到工具。新建 `backend/app/agent/tools/adapter.py`：

```python
from langchain_core.tools import StructuredTool

from app.agent.tools.registry import get_executor
from app.models.tool import Tool


def to_langchain_tool(
    tool: Tool, *, config_override: dict[str, Any] | None = None
) -> StructuredTool:
    """把数据库里的 Tool 记录转成 LangChain 可用的工具。"""
    config = {**tool.config, **(config_override or {})}
    executor = get_executor(tool.tool_type)

    async def _run(**kwargs: Any) -> str:
        result = await executor.execute(
            config=config,
            arguments=kwargs,
            timeout_seconds=tool.timeout_seconds,
        )
        if result.ok:
            return result.content
        return f"Tool execution failed: {result.error}"

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.parameters_schema,
        coroutine=_run,
    )
```

要点：

- 失败时返回一段**描述错误的文本**而不是抛异常。模型看到 "Tool execution failed: ..." 会自己决定重试还是换路。抛异常会让整个图崩掉
- `args_schema` 直接传 JSON Schema 字典。LangChain 的 `StructuredTool` 支持 dict 形式的 schema；如果当前版本要求 Pydantic 模型，用 `create_model` 从 JSON Schema 动态生成，或者用 `langchain_core.utils.function_calling` 里的辅助函数
- 只传 `coroutine` 不传 `func`，因为整条链路是异步的。如果 LangChain 要求必须有同步版本，传一个抛 `NotImplementedError` 的函数

Function Tool 的 `parameters_schema` 可以从注册表的 `args_model` 自动生成：

```python
fn = get_function(tool.config["function_name"])
schema = fn.args_model.model_json_schema()
```

在创建 Function Tool 的路由里自动填充这个字段，不让用户手填。

---

## 任务 5：状态图改造

这是本阶段对运行时的核心改动：从单节点变成带循环的图。

### 5.1 状态扩展

`app/agent/state.py` 的 `AgentState` 追加：

```python
    # 本次运行可用的工具（图构建时注入）
    tools: list[Any]     # list[StructuredTool]，避免 import 循环所以标 Any
```

`tools` 不能存进 checkpoint（工具对象不可序列化），所以阶段 06 配 checkpointer 时要把它排除。更稳妥的做法是**工具不进 state，而是在图构建时闭包捕获**。采用后者：

```python
def build_agent_graph(tools: list[StructuredTool]) -> StateGraph:
    async def call_model_with_tools(state: AgentState) -> dict[str, object]:
        return await call_model(state, tools=tools)
    # ...
```

`AgentState` 里不加 `tools` 字段。这样 checkpoint 只含可序列化数据。

### 5.2 节点改造

`app/agent/nodes.py` 的 `call_model` 增加 `tools` 参数并绑定：

```python
async def call_model(
    state: AgentState, *, tools: list[StructuredTool] | None = None
) -> dict[str, object]:
    chat = provider.get_chat_model(..., streaming=True)
    if tools:
        chat = chat.bind_tools(tools)
    # 其余逻辑不变
```

新增工具执行节点。**不要自己写，用 LangGraph 现成的 `ToolNode`**：

```python
from langgraph.prebuilt import ToolNode
```

`ToolNode` 会读最后一条 AIMessage 的 `tool_calls`，并发执行对应工具，把结果作为 `ToolMessage` 追加到 messages。它处理了工具名不存在、参数不匹配等边界情况。

### 5.3 条件边

新建判定函数（放在 `app/agent/graph.py`）：

```python
def should_continue(state: AgentState) -> Literal["tools", "__end__"]:
    """
    模型发起了工具调用就去 tools 节点，否则结束。

    同时守住 max_iterations，防止模型陷入无限工具调用循环。
    """
    if state["iteration"] >= state["max_iterations"]:
        return END
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return END
```

**`max_iterations` 守卫必须有。** 模型确实会陷入"调工具、看结果、再调同一个工具"的循环，没有守卫就是一个无限循环加无限账单。

达到上限时直接 END 而不是抛异常。此时最后一条消息是带 `tool_calls` 的 AIMessage，没有对应的 ToolMessage，这在消息历史上是不完整的。处理方式：在 `run_service` 里检测到这种情况时，给 Run 的 `output` 加一个 `truncated_by_max_iterations: true` 标记，并在 UI 上提示。

### 5.4 完整图

```python
def build_agent_graph(tools: list[StructuredTool] | None = None) -> StateGraph:
    graph = StateGraph(AgentState)

    async def _call_model(state: AgentState) -> dict[str, object]:
        return await call_model(state, tools=tools)

    graph.add_node("call_model", _call_model)
    graph.add_edge(START, "call_model")

    if tools:
        graph.add_node("tools", ToolNode(tools))
        graph.add_conditional_edges("call_model", should_continue)
        graph.add_edge("tools", "call_model")
    else:
        graph.add_edge("call_model", END)

    return graph
```

没有工具时保持单节点线性图，省掉一次条件判断。

`compile_agent_graph` 相应加 `tools` 参数。

### 5.5 更新 `KNOWN_NODE_NAMES`

阶段 04 在 `run_service.py` 里定义的节点名过滤集合要加上新节点：

```python
KNOWN_NODE_NAMES = {"call_model", "tools"}
```

漏了这个，前端看不到工具节点的执行指示。

### 5.6 工具调用事件

`stream_run` 里增加两种事件的采集。`astream_events` 会产出 `on_tool_start` 和 `on_tool_end`：

```python
        elif kind == "on_tool_start":
            yield (
                RunEventType.TOOL_CALLED,
                {"tool": event["name"], "args": event["data"].get("input")},
            )

        elif kind == "on_tool_end":
            output = event["data"].get("output")
            yield (
                RunEventType.TOOL_RESULT,
                {"tool": event["name"], "result": _truncate(str(output), 2000)},
            )
```

这两种事件**要落库**（不像 `model_chunk`），它们是执行轨迹的关键部分。结果要截断，工具返回可能很大。

SSE 契约相应扩展，把这两个事件加进阶段 04 任务 4.3 的表格里（改那份文档的表格，保持契约文档只有一份）。

### 5.7 工具加载

新建 `app/services/tool_service.py`，负责从 AgentVersion 加载工具：

```python
async def load_tools_for_version(
    *, session: AsyncSession, agent_version_id: uuid.UUID
) -> list[StructuredTool]:
    """加载某个 AgentVersion 绑定的全部启用工具。"""
    result = await session.execute(
        select(Tool, AgentToolBinding.config_override)
        .join(AgentToolBinding, AgentToolBinding.tool_id == Tool.id)
        .where(
            AgentToolBinding.agent_version_id == agent_version_id,
            Tool.is_active,
        )
    )
    return [
        to_langchain_tool(tool, config_override=override)
        for tool, override in result.all()
    ]
```

在 `stream_run` 和 `execute_run` 里调它，把结果传给 `compile_agent_graph`。

---

## 任务 6：路由

### 6.1 Tools CRUD

新建 `backend/app/api/routes/tools.py`，`tags=["tools"]`。

| 方法 | 路径 | response_model | 说明 |
|------|------|----------------|------|
| GET | `/tools/` | `ToolsPublic` | 列表，可按 `tool_type` 过滤 |
| POST | `/tools/` | `ToolPublic` | 创建 |
| GET | `/tools/{id}` | `ToolPublic` | 详情 |
| PATCH | `/tools/{id}` | `ToolPublic` | 更新 |
| DELETE | `/tools/{id}` | `Message` | 删除 |
| GET | `/tools/builtin-functions` | `BuiltinFunctionsPublic` | 内置函数列表，供前端下拉选择 |
| POST | `/tools/{id}/test` | `ToolTestResult` | 用给定参数试跑一次工具 |

**创建和更新时必须调 `validate_config`**：

```python
    executor = get_executor(tool_in.tool_type)
    try:
        executor.validate_config(tool_in.config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
```

Function 类型创建时自动填 `parameters_schema`（从注册表的 `args_model` 生成），忽略用户传的值。

`GET /tools/builtin-functions` 返回：

```python
class BuiltinFunctionPublic(SQLModel):
    name: str
    description: str
    parameters_schema: dict[str, Any]
```

`POST /tools/{id}/test` 是调试利器，让用户不用建 Agent 就能验证工具配好了：

```python
class ToolTestRequest(SQLModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolTestResult(SQLModel):
    ok: bool
    content: str
    error: str | None = None
    duration_ms: int
```

这个接口用 `async def` + `AsyncSessionDep`（因为执行器是异步的）。

`DELETE` 要注意：工具被已发布版本绑定时不能直接删（外键 CASCADE 会连带删掉绑定记录，导致历史版本的工具集不完整）。检查后返回 409：

```python
    bound = await session.execute(
        select(func.count()).select_from(AgentToolBinding).where(
            AgentToolBinding.tool_id == id
        )
    )
    if bound.scalar_one() > 0:
        raise HTTPException(
            status_code=409,
            detail="Tool is bound to published agent versions. Deactivate it instead.",
        )
```

引导用户用 `is_active = false` 而不是删除。这是保护历史可复现性的必要限制。

### 6.2 Agent 的工具绑定

不需要新接口。`PATCH /agents/{id}` 已经能改 `tool_ids`（它是 `AgentUpdate` 的字段）。

但要在 `AgentUpdate` 里加 `tool_ids: list[uuid.UUID] | None = None`，并在路由里校验这些 id 都存在且归属当前用户：

```python
    if agent_in.tool_ids is not None:
        owned = session.exec(
            select(func.count()).select_from(Tool).where(
                col(Tool.id).in_(agent_in.tool_ids),
                Tool.owner_id == current_user.id,
            )
        ).one()
        if owned != len(set(agent_in.tool_ids)):
            raise HTTPException(
                status_code=400, detail="Some tool ids are invalid or not owned by you"
            )
```

不校验的话用户能绑别人的工具，配置里可能带着 API key，是越权读取。

### 6.3 发布时创建绑定

`crud/agent.py` 的 `publish_agent_version` 里，创建 AgentVersion 之后补上绑定记录：

```python
    for tool_id in db_agent.tool_ids:
        session.add(
            AgentToolBinding(agent_version_id=db_version.id, tool_id=tool_id)
        )
    session.commit()
```

`AgentVersionPublic` 上加一个 `tool_names: list[str]` 非表字段，在版本详情接口里填充，方便前端展示。

---

## 任务 7：测试

### 7.1 `backend/tests/agent/test_tools_function.py`

- 注册表非空，`calculator` 和 `current_time` 都在
- `calculator` 算对 `2 * (3 + 4)` 得 14
- **安全测试（必须有）**：
  - `__import__('os').system('ls')` 返回 `ok=False`
  - `open('/etc/passwd').read()` 返回 `ok=False`
  - `2 ** 10000000` 返回 `ok=False`（范围守卫），且测试能在 1 秒内结束
  - 超长表达式返回 `ok=False`
- 参数校验失败（缺 required 字段）返回 `ok=False` 且 error 里有字段名
- `validate_config` 对不存在的 `function_name` 抛 ValueError 且错误信息里列出了可用函数

### 7.2 `backend/tests/agent/test_tools_http.py`

SSRF 防护是重点，**这些测试不能省**：

```python
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/api/v1/users/",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",   # 云元数据端点
        "http://10.0.0.1/internal",
        "http://192.168.1.1/",
        "http://[::1]/",
        "file:///etc/passwd",
        "ftp://example.com/",
    ],
)
def test_blocks_non_public_urls(url: str) -> None:
    with pytest.raises(ValueError):
        _assert_url_allowed(url)
```

另外：

- 公网 URL 通过校验（用 `example.com`，但要 mock `getaddrinfo` 避免测试依赖 DNS）
- `ALLOW_PRIVATE_TOOL_URLS=True` 时 localhost 放行
- 用 `httpx.MockTransport` 测执行逻辑：2xx 返回 `ok=True`；4xx/5xx 返回 `ok=False` 且 error 含响应体；超时返回 `ok=False`
- 超长响应被截断且带 `[truncated...]` 标记
- 确认 `follow_redirects=False`（mock 一个 302 到 localhost，断言没有跟随）

### 7.3 `backend/tests/agent/test_graph_tools.py`

- 无工具时图是线性的（`call_model` 直连 END）
- 有工具时 `should_continue` 在最后一条消息带 `tool_calls` 时返回 `"tools"`
- `should_continue` 在 `iteration >= max_iterations` 时返回 END，**即使最后一条消息带 tool_calls**
- 完整循环：假模型第一次返回 tool_calls、第二次返回普通文本，图跑完后 messages 里有 AIMessage、ToolMessage、AIMessage 三条
- 假模型每次都返回 tool_calls 时，图在 `max_iterations` 轮后停止而不是无限循环（**这条测试要设超时保护**，`max_iterations=3` 然后断言 `iteration == 3`）

构造带工具调用的假回复：

```python
AIMessage(
    content="",
    tool_calls=[{"name": "calculator", "args": {"expression": "1+1"}, "id": "call_1"}],
)
```

### 7.4 `backend/tests/api/routes/test_tools.py`

- 三种类型的创建各测一次
- Function 类型创建后 `parameters_schema` 被自动填充
- 非法 config 返回 400，错误信息有用
- 重名工具（同一 owner）返回 409 或 400（唯一约束）
- 别人的工具读取返回 403
- 绑定到已发布版本的工具删除返回 409
- `/tools/builtin-functions` 返回内置函数列表
- `/tools/{id}/test` 能试跑并返回结果

### 7.5 集成测试

`backend/tests/api/routes/test_conversations.py` 补一条：绑了工具的 agent，假模型先返回 tool_calls 再返回文本，SSE 事件流里有 `tool_called` 和 `tool_result`，且 `run_event` 表里也有这两条。

---

## 任务 8：前端

### 8.1 Tools 管理页

新建 `frontend/src/routes/_layout/tools.tsx` 和 `frontend/src/components/Tools/`。

列表列：name、tool_type（Badge）、description、requires_approval（图标）、is_active、actions。

`AddTool` 对话框按类型分支，这是本阶段前端的主要复杂度：

- 选 `function`：下拉选内置函数（数据来自 `/tools/builtin-functions`），选中后显示它的参数 schema（只读展示），name 和 description 自动填充为函数的但允许改
- 选 `http`：填 method、url、headers（key-value 动态行）、参数映射，以及 `parameters_schema` 的编辑
- 选 `mcp`：填 transport、url、tool_name

`parameters_schema` 对 HTTP 和 MCP 类型需要用户提供。不要给一个裸 JSON 文本域，做一个简单的参数行编辑器：每行是 name + type 下拉 + description + required 勾选，提交时组装成 JSON Schema。这比让用户手写 JSON 友好得多，也避免了格式错误。

**"Test" 按钮**：在工具详情或列表的操作菜单里，点开后根据 `parameters_schema` 动态生成参数表单，填完调 `/tools/{id}/test`，展示结果（成功显示内容，失败显示错误，都显示耗时）。这个功能对调试 HTTP 工具很关键。

侧栏加：

```typescript
{ icon: Wrench, title: "Tools", path: "/tools" },
```

### 8.2 Agent 详情页的工具绑定

在 `agents.$agentId.tsx` 的 Configuration 页签里加一个 "Tools" 区块：多选列表（`Checkbox` 列表或 shadcn 的多选），选项来自 `readTools`（只列 `is_active`），选中的写进 `tool_ids`。

每个工具项旁边显示它的类型 Badge 和 `requires_approval` 标记（后者在阶段 07 才生效，现在显示但加一句"审批功能将在后续启用"的 tooltip）。

Versions 页签的每个版本行显示 `tool_names`。

### 8.3 Playground 的工具调用可视化

这是本阶段最有展示价值的前端工作。在对话流里，当收到 `tool_called` 事件时插入一个特殊的消息块：

```
┌─────────────────────────────────────┐
│ 🔧 calculator                       │
│ 参数: { "expression": "2 * 21" }    │
│ 结果: 42                    (12ms)  │
└─────────────────────────────────────┘
```

实现要点：

- `tool_called` 事件到达时插入一个 pending 状态的工具块（显示转圈）
- `tool_result` 事件到达时用 `tool` 名匹配并填充结果
- 同一轮可能有多个并行工具调用，用事件里的顺序而不是工具名去匹配（工具名可能重复）。给工具块一个自增 index 作为 key
- 工具执行失败时结果区用红色显示错误

`node_started` 事件的 node 为 `"tools"` 时，节点指示器显示 "executing tools"。

Run 详情页的执行轨迹也要能展示这两种事件，样式与 Playground 一致。

---

## 阶段验收清单

- [ ] `uv run alembic check` 输出 `No new upgrade operations detected.`
- [ ] `agent.tool_ids` 列有 `server_default '[]'`，已有行没有变成 null
- [ ] `uv run pytest tests/ -v` 全绿
- [ ] SSRF 防护的参数化测试全部通过（8 个 URL 全被拦）
- [ ] calculator 的四条安全测试全部通过，且 `2 ** 10000000` 那条在 1 秒内结束
- [ ] `max_iterations` 循环守卫测试通过（假模型无限返回 tool_calls 时图能停下）
- [ ] `cd backend && bash scripts/lint.sh` 全绿
- [ ] `cd backend && bash scripts/test.sh` 全绿
- [ ] `bun run lint` 通过
- [ ] 手动验证 Function Tool：建一个 calculator 工具，绑到 agent，发布，在 Playground 问"123 乘 456 等于多少"，模型调用工具并给出 56088
- [ ] 手动验证 HTTP Tool：建一个指向某个公开 API 的工具（例如 `https://api.github.com/repos/{owner}/{repo}`），试跑成功，绑到 agent 后模型能用它
- [ ] 手动验证 SSRF 拦截：建一个 url 为 `http://169.254.169.254/` 的 HTTP 工具，创建时返回 400
- [ ] 手动验证工具失败：把 HTTP 工具的 url 改成不存在的域名，在 Playground 提问，模型收到失败信息并给出合理回应（而不是整个 Run 崩掉）
- [ ] 手动验证 Playground 的工具块显示正确（工具名、参数、结果、耗时）
- [ ] Run 详情页能看到 `tool_called` 和 `tool_result` 事件
- [ ] `SELECT count(*) FROM run_event WHERE event_type IN ('tool_called','tool_result');` 大于 0
- [ ] 绑定到已发布版本的工具，删除时返回 409
- [ ] MCP Tool 至少能创建并通过 `validate_config`（实际连通性测试可选，取决于有无可用的 MCP server）

---

## 常见坑

**注册表是空的，内置函数一个都没有**
`app/agent/tools/__init__.py` 里没有 `import app.agent.tools.builtins`。装饰器只在模块被导入时执行。

**`StructuredTool` 不接受 dict 形式的 args_schema**
LangChain 版本差异。用 `pydantic.create_model` 从 JSON Schema 动态建模型，或者查当前版本的 `args_schema` 支持的类型。在偏差记录里写明实际做法。

**工具执行抛异常导致整个 Run 失败**
`to_langchain_tool` 的 `_run` 里必须捕获所有异常并返回文本。执行器的 `execute` 也保证不抛。两层都要有。

**模型不调用工具**
三个排查方向：`bind_tools` 没调（检查 `call_model` 里 `tools` 参数是否真的传进来了）；工具 description 太模糊，模型不知道什么时候用；`parameters_schema` 格式不对，模型无法构造调用。用 `/tools/{id}/test` 先确认工具本身能跑。

**无限工具调用循环**
`should_continue` 里的 `max_iterations` 守卫。这是必须有的，不是可选优化。

**`tools` 字段进了 checkpoint 导致序列化失败**
不要把工具对象放进 `AgentState`。用闭包捕获，见任务 5.1。

**SSRF 检查被重定向绕过**
`httpx.AsyncClient(follow_redirects=False)`。默认值在不同 httpx 版本里不一致，必须显式写。

**同一轮多个工具调用的结果错位**
用事件顺序索引匹配，不要用工具名。模型可以在一轮里调用同一个工具多次。

**`ToolNode` 找不到工具**
`ToolNode(tools)` 里的工具名必须与模型返回的 `tool_calls[].name` 完全一致。工具名的正则约束（`^[a-zA-Z0-9_-]+$`）就是为了避免模型返回的名字与数据库里的对不上。

**达到 max_iterations 后消息历史不完整**
最后一条 AIMessage 带 tool_calls 但没有对应 ToolMessage。这会让**下一轮对话**报错（某些模型要求 tool_calls 必须有配对的结果）。落库消息时检测这种情况并丢弃那条不完整的 AIMessage，或者补一条说明被截断的 ToolMessage。

---

## 偏差记录

- MCP 实现方式（langchain-mcp-adapters / 官方 SDK）：
- MCP 支持的传输类型：
- `StructuredTool` 的 args_schema 实际写法：
- 其它偏差：
