# AgentHub — Agent 开发总纲

本文件是 AgentHub 项目的开发总纲。**任何 subagent 在动手写代码前必须先完整读完本文件**，然后再读取 `docs/dev/` 下对应阶段的任务文档。

本文件描述的是"不可违背的约定"。阶段文档描述的是"这一步具体做什么"。两者冲突时以本文件为准。

---

## 1. 项目定位

AgentHub 是基于 [Full Stack FastAPI Template](https://github.com/fastapi/full-stack-fastapi-template) 二次开发的 **AI Agent 管理与运行平台**。

保留模板已有的用户认证、管理后台、FastAPI API、PostgreSQL 和 React 管理界面，把示例性的 Item 管理扩展为 **Agent 定义、版本、会话、运行记录、工具和知识库管理**。以 LangGraph 作为编排引擎，提供状态化执行、工具调用、运行恢复、人工审批和执行过程追踪。

**目标产物**：一个可实际运行和演示的 Agent 后端平台，而不是单次模型调用封装或简单聊天页面。

**范围纪律**：这是一个个人能够完整开发、测试和部署的项目。遇到可以无限扩展的方向（多模型供应商、复杂 RAG 策略、分布式调度）时，按阶段文档写明的最小可用方案实现，把扩展点留在接口层，不要主动扩大范围。

---

## 2. 仓库结构速览

```
AgentHub/
├── AGENTS.md                  # 本文件
├── docs/dev/                  # 开发指导文档（00 总览 + 01-10 阶段任务）
├── pyproject.toml             # uv workspace 根，members = ["backend"]
├── package.json               # bun workspace 根，workspaces = ["frontend", "packages/*"]
├── .env                       # 本地开发配置（已被 git 跟踪，不放部署密钥）
├── .python-version            # 3.14
├── compose.yml                # 共享服务定义
├── compose.override.yml       # 本地开发覆盖（自动加载）
├── compose.deploy.yml         # 部署覆盖（显式指定）
├── scripts/generate-client.sh # 后端 OpenAPI -> 前端 TS 客户端
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI 应用入口，同时挂载前端静态文件
│   │   ├── models.py          # 所有 SQLModel 模型（阶段 1 拆成 models/ 包）
│   │   ├── crud.py            # 模块级 CRUD 函数
│   │   ├── api/
│   │   │   ├── main.py        # api_router 汇总
│   │   │   ├── deps.py        # SessionDep / CurrentUser / 超级用户依赖
│   │   │   └── routes/        # login, users, items, utils, private
│   │   ├── core/              # config.py, db.py, security.py
│   │   └── alembic/versions/  # 迁移脚本
│   ├── tests/                 # 注意：在 backend/tests/，不在 backend/app/tests/
│   ├── scripts/               # lint.sh, format.sh, test.sh, prestart.sh, tests-start.sh
│   └── pyproject.toml         # 后端依赖与 ruff/mypy 配置
└── frontend/
    ├── src/
    │   ├── client/            # openapi-ts 生成物，禁止手改
    │   ├── routes/            # TanStack Router 文件路由
    │   ├── components/
    │   └── hooks/
    ├── tests/                 # Playwright E2E
    └── openapi-ts.config.ts
```

---

## 3. 不可违背的技术决策

这些是 subagent 自行决定容易出乱子的地方，已经定好，**不要改，不要讨论替代方案**。

| 议题 | 决策 |
|------|------|
| 同步 / 异步 | 现有 CRUD 路由继续用同步 `Session`。Agent 执行路径（LangGraph 节点、Checkpointer、pgvector 检索、worker）用独立的 `async_engine` + `AsyncSession`。两套 engine 共用同一份 SQLModel 表定义 |
| 模型供应商 | 第一版只实现 **OpenAI 兼容适配器**（`base_url` 可配置，因此同时覆盖 OpenAI / DeepSeek / Qwen / vLLM / Ollama）。适配器接口预留多 provider 扩展，但不要实现第二个 |
| 编排引擎 | LangGraph。所有 Agent 执行必须走状态图，不允许在路由里直接调模型 |
| 任务队列 | `arq`（原生 asyncio、与 Redis 直连）。**不用 Celery** |
| 向量存储 | pgvector，跑在同一个 Postgres 实例里。db 镜像换成 `pgvector/pgvector:pg18` |
| 对象存储 | MinIO（S3 兼容），仅用于知识库原始文档和运行产物 |
| 模型组织 | 阶段 1 把 `app/models.py` 拆成 `app/models/` 包，`__init__.py` 重导出全部符号以保证 `from app.models import X` 不破 |
| Item 的处置 | **阶段 1 不要删**。Item 是唯一四层齐全的端到端样例（路由 + pytest + 前端页面 + Playwright spec），是写新功能时的参照物。等阶段 2 的 Agent CRUD 全链路打通后再整体移除 |

### 3.1 Python 版本风险（阶段 1 必须先处理）

`.python-version` 锁定 3.14，但 LangChain / LangGraph 生态对 3.14 的支持可能滞后。**阶段 1 的第一个任务就是依赖可行性验证**。

如果依赖解析失败需要回退到 3.13，必须**同步**修改以下全部位置，漏掉任何一处都会导致 CI 或镜像构建失败：

- `.python-version` → `3.13`
- `backend/pyproject.toml` 的 `requires-python = ">=3.14,<4.0"` → `">=3.13,<4.0"`
- `backend/pyproject.toml` 的 `[tool.ruff] target-version = "py314"` → `"py313"`
- `backend/Dockerfile` 的 `FROM python:3.14` → `FROM python:3.13`
- `backend/app/api/deps.py:36` 的 `except InvalidTokenError, ValidationError:` → `except (InvalidTokenError, ValidationError):`（无括号多异常是 PEP 758 的 3.14 新语法，3.13 下是语法错误）
- 全局搜索其它 3.14-only 语法，确保 `uv run ruff check app` 通过

回退后要在 `docs/dev/01-foundation.md` 的验收清单里记录实际使用的版本。

---

## 4. 后端代码铁律

### 4.1 CRUD 函数

`app/crud.py`（或阶段文档指定的 `app/crud/` 子模块）里全是**模块级函数，没有 class，没有 Repository 模式**。所有参数 keyword-only：

```python
def create_agent(*, session: Session, agent_in: AgentCreate, owner_id: uuid.UUID) -> Agent:
    db_obj = Agent.model_validate(agent_in, update={"owner_id": owner_id})
    session.add(db_obj)
    session.commit()
    session.refresh(db_obj)
    return db_obj
```

命名遵循 `create_*` / `update_*` / `get_*_by_*` / `delete_*`。更新用 `db_obj.sqlmodel_update(data, update=extra)`，不要手写字段赋值。持久化一律 `session.add` → `session.commit` → `session.refresh`。

### 4.2 路由

每个资源一个文件放在 `app/api/routes/`，并在 `app/api/main.py` 里 `include_router` 注册。

```python
router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/", response_model=AgentsPublic)
def read_agents(
    session: SessionDep, current_user: CurrentUser, skip: int = 0, limit: int = 100
) -> Any:
    """
    Retrieve agents.
    """
```

硬性要求：

- 依赖注入只用 `app/api/deps.py` 里的类型别名 `SessionDep`、`CurrentUser`，管理员接口用 `Depends(get_current_active_superuser)`。不要在路由里自己写 `Depends(get_db)`
- handler 返回标注 `-> Any`，由 `response_model` 约束输出；只返回 `Message` 的接口直接标注 `-> Message`
- 带 body 的写操作用 keyword-only（函数第一个参数是 `*`）
- 每个 handler 有一句英文 docstring，它会出现在 OpenAPI 文档里
- `tags` 必填且只有一个，因为 `app/main.py` 的 `custom_generate_unique_id` 用 `route.tags[0]` 拼 operation id，进而决定前端生成的 Service 类名

### 4.3 列表响应与分页

列表统一返回 `{ data, count }` 包装模型，查询参数统一 `skip` / `limit`：

```python
count_statement = select(func.count()).select_from(Agent).where(Agent.owner_id == current_user.id)
count = session.exec(count_statement).one()
statement = (
    select(Agent)
    .where(Agent.owner_id == current_user.id)
    .order_by(col(Agent.created_at).desc())
    .offset(skip)
    .limit(limit)
)
```

### 4.4 归属与权限

所有用户数据按 `owner_id` 隔离。非 superuser 只能看到和操作自己的资源，superuser 不加 owner 过滤。错误信息沿用模板原文，不要自创新文案：

- 找不到：`HTTPException(status_code=404, detail="Agent not found")`
- 越权：`HTTPException(status_code=403, detail="Not enough permissions")`

### 4.5 模型定义

SQLModel 模型按 `XxxBase` / `XxxCreate` / `XxxUpdate` / `Xxx`（`table=True`）/ `XxxPublic` / `XxxsPublic` 六件套组织。

- 主键统一 `uuid.UUID`，`default_factory=uuid.uuid4`
- 时间戳用 `Field(default_factory=get_datetime_utc, sa_type=DateTime(timezone=True))`，复用 `app/models` 里已有的 `get_datetime_utc`
- 外键写明级联：`Field(foreign_key="user.id", nullable=False, ondelete="CASCADE")`
- 字符串字段必须给 `max_length`，否则 Postgres 会建成无限长 varchar
- 关系用 `Relationship(back_populates=..., cascade_delete=True)`，同时外键上要写 `ondelete="CASCADE"`（前者管 ORM 层，后者管数据库层，缺一不可）
- 单词类名不写 `__tablename__`（默认的小写类名就对）。**多词类名必须显式写蛇形表名**，否则 `AgentVersion` 会变成 `agentversion`：`__tablename__ = "agent_version"`
- **不要用 `model_` 前缀的字段名**。Pydantic v2 保留了 `model_` 命名空间，`model_name` / `model_settings` 会触发告警。统一用 `llm_` 前缀：`llm_model`、`llm_settings`
- JSONB 字段写 `Field(default_factory=dict, sa_type=JSONB)`，`JSONB` 从 `sqlalchemy.dialects.postgresql` 引入。写入时用 `model_dump(mode="json")`，否则 UUID 和 datetime 会序列化失败

### 4.6 类型检查与 lint

`mypy` 配置是 `strict = true`，另外还跑 `ty check`。这意味着：

- 所有函数必须有完整类型标注，包括返回值
- 禁止 `print`（ruff 规则 T201）。需要输出用 `logging`
- 自动修复：`bash scripts/format.sh`（在 `backend/` 目录下）
- 提交前检查：`bash scripts/lint.sh`（在 `backend/` 目录下）

### 4.7 数据库迁移

改完模型**必须**生成迁移，不要依赖 `create_all`：

```bash
cd backend
uv run alembic revision --autogenerate -m "Add agent and agent version models"
uv run alembic upgrade head
```

注意事项：

- `alembic/env.py` 通过 `from app.models import SQLModel` 拿 metadata。新模型文件必须在 `app/models/__init__.py` 里被导入，否则 autogenerate 会认为该表需要被删除
- autogenerate 的产物**必须人工检查**，特别是 pgvector 的 `Vector` 列和 JSONB 列，它经常生成错的类型
- 生成的迁移文件要提交到 git
- 当前基线 head 是 `fe56fa70289e`（`add_created_at_to_user_and_item`）

### 4.8 测试

测试在 `backend/tests/`，镜像 `app/` 的目录结构（`tests/api/routes/test_agents.py`、`tests/crud/test_agent.py`）。

复用已有 fixture，不要自己造：

- `db`：session 级 autouse 的 `Session`，teardown 会清表
- `client`：module 级 `TestClient(app)`
- `superuser_token_headers` / `normal_user_token_headers`：module 级，直接拿到 `Authorization` 头

新增模型时，在 `tests/utils/` 加对应的 `create_random_xxx(db)` 辅助函数，参照 `tests/utils/item.py`。随机数据用 `tests/utils/utils.py` 的 `random_lower_string()` / `random_email()`。

**重要**：`db` fixture 的 teardown 只删 `Item` 和 `User`。每新增一张表，都要在 `tests/conftest.py` 的 teardown 里加对应的 `delete(NewModel)` 语句，**且顺序必须是子表先删、父表后删**，否则外键约束会让清理失败。

运行：

```bash
cd backend
bash scripts/test.sh        # 带 coverage
uv run pytest tests/api/routes/test_agents.py -x   # 单文件，快速迭代
```

---

## 5. 前端代码铁律

### 5.1 API 客户端是生成物

`frontend/src/client/` 下所有文件都由 `@hey-api/openapi-ts` 生成，**禁止手工编辑**。

**后端 API 一有改动（新增路由、改模型、改 docstring、改 tags），立刻在仓库根目录执行**：

```bash
bash scripts/generate-client.sh
```

这个脚本会导出 `openapi.json`、重新生成 `frontend/src/client/`、再跑一遍 lint。不跑它，前端拿不到新接口，而且 pre-commit hook 会在提交时替你跑并产生意外 diff。

业务代码从聚合入口引入，不要深入到 `.gen.ts`：

```typescript
import { AgentsService, type AgentPublic, type AgentCreate } from "@/client"
```

Service 类名由后端的 `tags[0]` 决定（`tags=["agents"]` → `AgentsService`），方法名是去掉 tag 前缀的 operation id（`agents-read_agents` → `readAgents`）。

### 5.2 路由

TanStack Router 文件路由。需要登录的页面放在 `src/routes/_layout/` 下，`_layout.tsx` 已经统一做了未登录重定向，页面自己不用再判断。

```typescript
export const Route = createFileRoute("/_layout/agents")({
  component: Agents,
  head: () => ({ meta: [{ title: "Agents - AgentHub" }] }),
})
```

`routeTree.gen.ts` 由 `@tanstack/router-plugin` 自动生成，不要手改。

新页面必须同时在 `src/components/Sidebar/AppSidebar.tsx` 的 `baseItems` 数组里注册导航项（仅管理员可见的项追加到 `is_superuser` 分支）：

```typescript
const baseItems: Item[] = [
  { icon: Home, title: "Dashboard", path: "/" },
  { icon: Bot, title: "Agents", path: "/agents" },
]
```

图标从 `lucide-react` 引入。

### 5.3 数据获取

列表页用 `useSuspenseQuery` + 外层 `Suspense` + 骨架屏组件（参照 `src/components/Pending/PendingItems.tsx`）：

```typescript
function getAgentsQueryOptions() {
  return {
    queryFn: async () =>
      (await AgentsService.readAgents({ query: { skip: 0, limit: 100 } })).data,
    queryKey: ["agents"],
  }
}
```

`queryKey` 用资源名复数的字符串数组。写操作统一模式：

```typescript
const mutation = useMutation({
  mutationFn: (data: AgentCreate) => AgentsService.createAgent({ body: data }),
  onSuccess: () => {
    showSuccessToast("Agent created successfully")
    form.reset()
    setIsOpen(false)
  },
  onError: handleError.bind(showErrorToast),
  onSettled: () => {
    queryClient.invalidateQueries({ queryKey: ["agents"] })
  },
})
```

`onError` 必须是 `handleError.bind(showErrorToast)`，它统一处理后端的 `detail` 字段。`onSettled` 里的 `invalidateQueries` 必须带 `queryKey`。

### 5.4 表单

`react-hook-form` + `zodResolver`，`mode: "onBlur"`。用 shadcn 的 `Form` / `FormField` / `FormControl` / `FormMessage` 组合，提交按钮用 `LoadingButton` 并绑定 `mutation.isPending`。完整可抄的样例是 `src/components/Items/AddItem.tsx`。

### 5.5 表格与分页

现有 `src/components/Common/DataTable.tsx` 是**客户端分页**：API 固定拉 `limit: 100`，表格用 `getPaginationRowModel()` 在前端翻页。

这对 Agent 列表够用，但对 **Run 列表（可能上万条）不够用**。阶段文档会指明何时需要扩展 `DataTable` 支持服务端分页，在那之前不要擅自重构它。

### 5.6 lint

前端 lint 是 **Biome**，不是 ESLint：

```bash
bun run lint     # 在仓库根目录，等价于 biome check --write
```

---

## 6. 常用命令速查

### 本地启动

```bash
# 仓库根目录：起依赖服务
docker compose up -d db mailpit redis minio

# backend/ 目录：装依赖、建表、起 API
cd backend
uv sync
uv run bash scripts/prestart.sh
uv run fastapi dev

# 仓库根目录（另一个终端）：起前端
bun install
bun run dev
```

访问地址：前端 <http://localhost:5173>，API <http://localhost:8000>，API 文档 <http://localhost:8000/docs>，Mailpit <http://localhost:8025>，Adminer <http://localhost:8080>。

### 加依赖

后端是 uv workspace（根 `pyproject.toml` 的 `members = ["backend"]`），加后端依赖要在 `backend/` 目录：

```bash
cd backend
uv add langgraph
uv add --group dev pytest-asyncio
```

前端是 bun workspace：

```bash
bun add --filter frontend some-package
```

### 提交前必跑

```bash
cd backend && bash scripts/lint.sh && bash scripts/test.sh && cd ..
bash scripts/generate-client.sh    # 若改过后端 API
bun run lint
```

或者装上 hook 让它自动跑：`uv run prek install -f`。

---

## 7. 阶段文档索引

阶段有严格依赖顺序，**不要跳阶段**。每份文档都以"前置依赖"开头，开工前先确认前置阶段的验收清单已全部通过。

| 文档 | 内容 | 前置 |
|------|------|------|
| [docs/dev/00-overview.md](docs/dev/00-overview.md) | 架构总览、完整数据模型全景、阶段依赖图。**只读参考，不含执行任务** | — |
| [docs/dev/01-foundation.md](docs/dev/01-foundation.md) | 依赖验证、项目重命名、models 包化、compose 扩展、async engine、pgvector 扩展 | — |
| [docs/dev/02-agent-crud.md](docs/dev/02-agent-crud.md) | Agent / AgentVersion 模型与 CRUD、前端管理页、移除 Item | 01 |
| [docs/dev/03-langgraph-runtime.md](docs/dev/03-langgraph-runtime.md) | 模型适配器、最小状态图、Run 模型、同步执行 | 02 |
| [docs/dev/04-conversation-streaming.md](docs/dev/04-conversation-streaming.md) | 会话记忆、上下文裁剪、SSE 流式、Playground | 03 |
| [docs/dev/05-tools.md](docs/dev/05-tools.md) | Function / HTTP / MCP 工具系统、ToolNode 接入 | 04 |
| [docs/dev/06-async-worker.md](docs/dev/06-async-worker.md) | arq worker、异步 Run、Checkpoint 恢复、限流 | 05 |
| [docs/dev/07-approval.md](docs/dev/07-approval.md) | 高风险工具拦截、interrupt 暂停、审批链路 | 06 |
| [docs/dev/08-rag.md](docs/dev/08-rag.md) | 文档上传、切片 Embedding、向量检索、来源引用 | 06 |
| [docs/dev/09-eval.md](docs/dev/09-eval.md) | 测试用例、跨版本批量评测、结果对比 | 06 |
| [docs/dev/10-observability.md](docs/dev/10-observability.md) | OpenTelemetry、Prometheus、Grafana、监控页 | 06 |

阶段 07 / 08 / 09 / 10 都只依赖 06，彼此独立，可以按需调整先后顺序。

---

## 8. 工作纪律

1. **先读文档再动手**：本文件 + `00-overview.md` + 当前阶段文档。
2. **按任务编号顺序执行**，不要并行改动同一批文件。
3. **每完成一个任务就跑一次 lint**，不要攒到最后。
4. **阶段结束时逐条走验收清单**，每一条都要实际执行命令并确认输出，不能靠"看起来应该没问题"。
5. **不要修改阶段文档之外的文件**。如果发现必须改，先在阶段文档的"偏差记录"里写明原因。
6. **遇到与本文件约定冲突的情况就停下来报告**，不要自行发明新约定。
