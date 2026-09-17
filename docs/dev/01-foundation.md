# 01 — 基础改造

## 前置依赖

无。这是第一个阶段。

开工前确认已读 [AGENTS.md](../../AGENTS.md) 和 [00-overview.md](00-overview.md)。

## 本阶段目标

把一个未改动的 FastAPI 模板改造成能承载 Agent 平台的骨架：

- 验证 LangGraph 全家桶能在当前 Python 版本上安装（这决定后面所有阶段能不能走）
- 项目更名为 AgentHub
- `models.py` / `crud.py` 拆成包，为后面十几张表做准备
- compose 里补上 pgvector、Redis、MinIO
- 建立 async engine，让 Agent 执行路径能用异步
- 建好后续阶段要往里填的目录骨架

## 本阶段不做什么

不写任何 Agent 业务逻辑，不建 Agent / Run 等业务表，不碰前端页面（除了改标题和文案）。**不要删 Item**，它是阶段 02 的参照物。

---

## 任务 1：依赖可行性验证（最高优先，先做这个）

这一步决定后续所有阶段是否可行。如果这里过不去，其它任务先不要动。

### 1.1 尝试安装核心依赖

在 `backend/` 目录下执行（注意本项目是 uv workspace，根 `pyproject.toml` 的 `members = ["backend"]`，所以必须在 `backend/` 里 `uv add`）：

```bash
cd backend
uv add "langgraph>=0.2" "langchain-core>=0.3" "langchain-openai>=0.2" "langgraph-checkpoint-postgres" "pgvector"
uv add --group dev "pytest-asyncio"
```

关于 async 数据库驱动：**不要装 asyncpg**。项目已有 `psycopg[binary]`（psycopg3），SQLAlchemy 的 `postgresql+psycopg` dialect 同时支持同步和异步，`config.py` 里的 `_use_psycopg_driver` 校验器已经把 URL 改写成了 `postgresql+psycopg://`。同一个 URL 可以直接喂给 `create_async_engine`。

但 `langgraph-checkpoint-postgres` 需要连接池，确认它把 `psycopg_pool` 作为依赖带进来了：

```bash
uv run python -c "import psycopg_pool; print(psycopg_pool.__version__)"
```

如果没有，显式补上：`uv add psycopg-pool`。

### 1.2 验证能真的 import

安装成功不等于能用。逐个验证：

```bash
cd backend
uv run python -c "
import langgraph, langchain_core, langchain_openai, pgvector
from langgraph.graph import StateGraph
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langchain_openai import ChatOpenAI
from pgvector.sqlalchemy import Vector
print('all imports ok')
"
```

必须打印 `all imports ok` 才算通过。

### 1.3 如果失败：回退到 Python 3.13

LangChain 生态对 Python 3.14 的支持可能滞后。如果 `uv add` 报依赖解析失败，或者 import 时报 C 扩展相关错误，回退到 3.13。

**必须同步改完以下全部位置**，漏一处就会在 CI 或镜像构建时爆掉：

1. `.python-version`：`3.14` → `3.13`
2. `backend/pyproject.toml`：`requires-python = ">=3.14,<4.0"` → `">=3.13,<4.0"`
3. `backend/pyproject.toml`：`[tool.ruff]` 的 `target-version = "py314"` → `"py313"`
4. `backend/Dockerfile`：`FROM python:3.14` → `FROM python:3.13`
5. `backend/app/api/deps.py` 第 36 行：

   ```python
   # 3.14 语法（PEP 758，无括号多异常），3.13 下是语法错误
   except InvalidTokenError, ValidationError:
   # 改成
   except (InvalidTokenError, ValidationError):
   ```

6. 重建虚拟环境并全局检查其它 3.14-only 语法：

   ```bash
   cd backend
   rm -rf ../.venv
   uv sync
   uv run ruff check app
   uv run mypy app
   ```

7. 检查 `.github/workflows/` 里是否有硬编码的 Python 版本（目前 workflow 用 `.python-version`，但改完要确认）

回退后在本文档末尾的"偏差记录"区域写明实际使用的版本和原因。

### 1.4 验收

- `uv sync` 无错误
- 1.2 的 import 脚本打印 `all imports ok`
- `cd backend && bash scripts/lint.sh` 全绿
- `cd backend && bash scripts/test.sh` 全绿（这一步证明新依赖没破坏现有功能）
- `uv.lock` 有改动并已提交

---

## 任务 2：项目更名为 AgentHub

纯文本替换，但要改全。逐个文件改：

| 文件 | 改什么 |
|------|--------|
| `.env` | `PROJECT_NAME="Full Stack FastAPI Project"` → `PROJECT_NAME="AgentHub"` |
| `README.md` | 整体重写为 AgentHub 的项目介绍（定位、功能、技术栈、快速开始、指向 `docs/dev/`）。保留原模板的 License 段落和对 `development.md` / `deployment.md` 的链接 |
| `package.json` | `"name": "fastapi-full-stack-template"` → `"name": "agenthub"` |
| `frontend/src/routes/__root.tsx` | 默认页面标题里的模板名改成 AgentHub |
| `frontend/src/routes/_layout/*.tsx` | 各页面 `head()` 里的 `title` 后缀 `- FastAPI Template` → `- AgentHub` |
| `frontend/src/components/Common/Logo.tsx` | 品牌名文案 |
| `frontend/src/components/Common/Footer.tsx` | 页脚文案 |
| `frontend/src/components/Common/AuthLayout.tsx` | 登录页品牌文案 |
| `frontend/tests/*.spec.ts` | 若断言里包含旧标题字符串，同步更新 |

改完全局搜一遍确认没有遗漏：

```bash
rg -i "full.stack.fastapi|fastapi.template" --glob '!uv.lock' --glob '!bun.lock' --glob '!frontend/src/client/**' --glob '!release-notes.md' --glob '!docs/**'
```

`release-notes.md` 是上游模板的历史记录，不用改；也可以直接清空成 AgentHub 自己的版本记录。

### 验收

- `rg` 搜索无业务代码命中
- `bun run lint` 通过
- `cd backend && uv run pytest tests/ -x` 通过（标题改动不影响后端，但确认一下）

---

## 任务 3：`models.py` 拆成 `models/` 包

这是有风险的重构，**严格按步骤做，每步跑一次验证**。

### 3.1 目标结构

```
backend/app/models/
├── __init__.py      # 重导出全部符号
├── base.py          # get_datetime_utc + Message / Token / TokenPayload / NewPassword
├── user.py          # User 六件套 + UpdatePassword
└── item.py          # Item 六件套（阶段 02 会整体删掉这个文件）
```

### 3.2 步骤

**第一步**：建目录，把 `app/models.py` 的内容按上表切分到三个文件里。切分规则：

- `base.py`：`get_datetime_utc`、`Message`、`Token`、`TokenPayload`、`NewPassword`
- `user.py`：`UserBase`、`UserCreate`、`UserRegister`、`UserUpdate`、`UserUpdateMe`、`UpdatePassword`、`User`、`UserPublic`、`UsersPublic`
- `item.py`：`ItemBase`、`ItemCreate`、`ItemUpdate`、`Item`、`ItemPublic`、`ItemsPublic`

**第二步**：处理 `User` 与 `Item` 的双向关系。`User.items` 引用 `Item`，`Item.owner` 引用 `User`，跨文件后会形成循环导入。解法是在 `user.py` 里用字符串前向引用 + `TYPE_CHECKING`：

```python
# app/models/user.py
from typing import TYPE_CHECKING

from sqlmodel import Relationship

if TYPE_CHECKING:
    from app.models.item import Item


class User(UserBase, table=True):
    # ... 其它字段
    items: list["Item"] = Relationship(back_populates="owner", cascade_delete=True)
```

`item.py` 里同理处理 `owner: "User" | None`。

**第三步**：写 `__init__.py`，重导出全部符号。这一步是**关键**，`alembic/env.py` 的 `from app.models import SQLModel` 和全项目的 `from app.models import User` 都依赖它：

```python
# app/models/__init__.py
from sqlmodel import SQLModel

from app.models.base import (
    Message,
    NewPassword,
    Token,
    TokenPayload,
    get_datetime_utc,
)
from app.models.item import (
    Item,
    ItemBase,
    ItemCreate,
    ItemPublic,
    ItemsPublic,
    ItemUpdate,
)
from app.models.user import (
    UpdatePassword,
    User,
    UserBase,
    UserCreate,
    UserPublic,
    UserRegister,
    UsersPublic,
    UserUpdate,
    UserUpdateMe,
)

__all__ = [
    "SQLModel",
    "Message",
    "NewPassword",
    "Token",
    "TokenPayload",
    "get_datetime_utc",
    "Item",
    "ItemBase",
    "ItemCreate",
    "ItemPublic",
    "ItemsPublic",
    "ItemUpdate",
    "UpdatePassword",
    "User",
    "UserBase",
    "UserCreate",
    "UserPublic",
    "UserRegister",
    "UsersPublic",
    "UserUpdate",
    "UserUpdateMe",
]
```

注意：`SQLModel` 必须从这里导出，因为 `alembic/env.py` 里写的是 `from app.models import SQLModel  # noqa`。

**第四步**：删掉旧的 `app/models.py`。

**第五步**：解决前向引用。SQLModel 用字符串引用关系时，需要在所有模型都导入后调用一次 `model_rebuild()`。在 `__init__.py` 末尾加上：

```python
User.model_rebuild()
Item.model_rebuild()
```

如果不加，运行时访问 `user.items` 会报 `Relationship` 未解析。

**第六步**：验证。这一步必须全绿才能继续：

```bash
cd backend
uv run python -c "from app.models import SQLModel, User, Item; print(sorted(SQLModel.metadata.tables))"
# 期望输出：['item', 'user']

uv run alembic check
# 期望：No new upgrade operations detected.
# 如果它说要删表或建表，说明 __init__.py 漏了导出，回去补

bash scripts/lint.sh
bash scripts/test.sh
```

`alembic check` 输出 "No new upgrade operations detected" 是这次重构成功的核心证据 —— 说明拆包前后 metadata 完全一致。

### 3.3 不要做的事

- 不要顺手改字段、加字段、改类型。这次重构必须是**纯搬移**，metadata 一字不差
- 不要生成新的 migration

---

## 任务 4：`crud.py` 拆成 `crud/` 包

比任务 3 简单，因为没有循环引用问题。

### 4.1 目标结构

```
backend/app/crud/
├── __init__.py      # 重导出，保持 from app import crud; crud.create_user(...) 可用
├── user.py          # create_user, update_user, get_user_by_email, authenticate, DUMMY_HASH
└── item.py          # create_item（阶段 02 删掉）
```

`__init__.py`：

```python
from app.crud.item import create_item
from app.crud.user import (
    authenticate,
    create_user,
    get_user_by_email,
    update_user,
)

__all__ = [
    "authenticate",
    "create_item",
    "create_user",
    "get_user_by_email",
    "update_user",
]
```

调用方式保持不变（`app/core/db.py`、`app/api/routes/*.py`、`backend/tests/utils/*.py` 里都是 `crud.create_user(...)` 这种形式），所以**不需要改任何调用点**。

### 4.2 验收

```bash
cd backend
uv run python -c "from app import crud; print(crud.authenticate, crud.create_item)"
bash scripts/lint.sh
bash scripts/test.sh
```

---

## 任务 5：compose 补齐基础设施

### 5.1 db 服务换 pgvector 镜像

`compose.yml` 里：

```yaml
  db:
    image: pgvector/pgvector:pg18
```

`pgvector/pgvector:pg18` 是官方 postgres:18 镜像加装 vector 扩展，环境变量、数据目录、`pg_isready` 健康检查全部兼容，其它配置不用动。

**注意**：现有 volume `app-db-data` 里的数据是 `postgres:18` 建的，换镜像后数据可以直接用。但如果启动失败报 volume 版本不兼容，本地开发环境直接重置：

```bash
docker compose down -v
docker compose up -d db
cd backend && uv run bash scripts/prestart.sh
```

### 5.2 新增 redis 服务

`compose.yml` 的 `services` 下加：

```yaml
  redis:
    image: redis:8-alpine
    command: ["redis-server", "--appendonly", "yes"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    volumes:
      - app-redis-data:/data
```

`compose.override.yml` 里暴露端口：

```yaml
  redis:
    ports:
      - "6379:6379"
```

### 5.3 新增 minio 服务

`compose.yml`：

```yaml
  minio:
    image: minio/minio
    command: ["server", "/data", "--console-address", ":9001"]
    environment:
      - MINIO_ROOT_USER=${S3_ACCESS_KEY:?Variable not set}
      - MINIO_ROOT_PASSWORD=${S3_SECRET_KEY:?Variable not set}
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 10s
      timeout: 5s
      retries: 5
    volumes:
      - app-minio-data:/data
```

`compose.override.yml`：

```yaml
  minio:
    ports:
      - "9000:9000"
      - "9001:9001"
```

MinIO 控制台在 <http://localhost:9001>。

### 5.4 声明新 volume

`compose.yml` 末尾：

```yaml
volumes:
  app-db-data:
  app-redis-data:
  app-minio-data:
```

### 5.5 backend 服务传递新环境变量

`compose.yml` 的 `backend.environment` 下追加（变量名与任务 6 的 `config.py` 对齐）：

```yaml
      LLM_API_KEY: ${LLM_API_KEY:-}
      LLM_BASE_URL: ${LLM_BASE_URL:-}
      LLM_MODEL: ${LLM_MODEL:-}
      EMBEDDING_MODEL: ${EMBEDDING_MODEL:-}
      EMBEDDING_DIM: ${EMBEDDING_DIM:-}
      REDIS_URL: redis://redis:6379/0
      S3_ENDPOINT: http://minio:9000
      S3_ACCESS_KEY: ${S3_ACCESS_KEY:?Variable not set}
      S3_SECRET_KEY: ${S3_SECRET_KEY:?Variable not set}
      S3_BUCKET: ${S3_BUCKET:-agenthub}
```

同时把 `backend.depends_on` 扩展为同时等 db / redis / minio 健康：

```yaml
    depends_on:
      db:
        condition: service_healthy
        restart: true
      redis:
        condition: service_healthy
      minio:
        condition: service_healthy
```

注意 `REDIS_URL` 和 `S3_ENDPOINT` 在 compose 里用服务名（`redis` / `minio`），而 `.env` 里用 `localhost` —— 这与模板对 `DATABASE_URL` 和 `SMTP_HOST` 的处理方式一致。

### 5.6 `compose.deploy.yml`

部署文件里 redis 和 minio 不需要暴露到公网，不用加 Traefik 标签。只需确认 `backend` 服务的环境变量透传（它继承 `compose.yml`，通常不用改）。

### 5.7 验收

```bash
docker compose up -d db redis minio mailpit
docker compose ps
# 三个新服务都是 healthy

docker compose exec db psql -U postgres -d app -c "SELECT * FROM pg_available_extensions WHERE name = 'vector';"
# 能查到 vector 扩展可用

docker compose exec redis redis-cli ping
# PONG
```

---

## 任务 6：配置项扩展

### 6.1 `backend/app/core/config.py`

在 `Settings` 类里追加（放在 `DATABASE_URL` 相关代码之后、SMTP 配置之前）：

```python
    # --- LLM ---
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TIMEOUT_SECONDS: int = 120
    LLM_MAX_RETRIES: int = 2

    # --- Embedding ---
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIM: int = 1536

    # --- Agent 运行限制 ---
    AGENT_MAX_ITERATIONS: int = 10
    AGENT_DEFAULT_TIMEOUT_SECONDS: int = 300
    AGENT_MAX_CONCURRENT_RUNS_PER_USER: int = 3

    # --- Redis ---
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- 对象存储 ---
    S3_ENDPOINT: str = "http://localhost:9000"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_BUCKET: str = "agenthub"
    S3_SECURE: bool = False
```

`LLM_API_KEY` 默认空字符串而不是必填，这样不配 key 也能跑起来做 CRUD 开发和测试。真正调模型时才需要它 —— 阶段 03 会在适配器初始化时检查并给出明确错误。

`EMBEDDING_DIM` 必须与 `EMBEDDING_MODEL` 匹配（`text-embedding-3-small` 是 1536）。阶段 08 的 `Chunk.embedding` 列维度由它决定，**改这个值需要重建向量表**，所以一开始就要定准。

### 6.2 `.env`

追加（本地开发默认值）：

```bash
# LLM（OpenAI 兼容接口，base_url 可指向任意兼容服务）
LLM_API_KEY=
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1536

# Redis
REDIS_URL=redis://localhost:6379/0

# 对象存储（MinIO）
S3_ENDPOINT=http://localhost:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=agenthub
```

`.env` 是被 git 跟踪的本地开发默认值文件，`LLM_API_KEY` 留空，开发者自己在本地填上但**不要提交带 key 的版本**。在 README 的快速开始里提醒这一点。

### 6.3 验收

```bash
cd backend
uv run python -c "from app.core.config import settings; print(settings.LLM_MODEL, settings.EMBEDDING_DIM, settings.REDIS_URL)"
bash scripts/lint.sh
```

---

## 任务 7：建立 async engine

### 7.1 新建 `backend/app/core/async_db.py`

```python
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# DATABASE_URL 已被 config 的校验器改写为 postgresql+psycopg://，
# psycopg3 的 SQLAlchemy dialect 同时支持同步和异步，无需额外驱动。
async_engine: AsyncEngine = create_async_engine(
    str(settings.DATABASE_URL),
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

async_session_maker = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_async_session() -> AsyncGenerator[AsyncSession]:
    async with async_session_maker() as session:
        yield session
```

`expire_on_commit=False` 是必须的：LangGraph 节点会在 commit 之后继续访问 ORM 对象的属性，默认的 expire 行为会触发一次 lazy load，在 async 上下文里会抛 `MissingGreenlet`。

### 7.2 在 `deps.py` 里加类型别名

`backend/app/api/deps.py` 追加：

```python
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.async_db import get_async_session

AsyncSessionDep = Annotated[AsyncSession, Depends(get_async_session)]
```

### 7.3 使用纪律（写进代码注释里）

- 所有现有和新增的 **CRUD 路由继续用 `SessionDep`**，不要迁移
- 只有 **Agent 执行路径**用 `AsyncSessionDep`：`/runs/stream`、worker 任务、checkpointer、向量检索
- 同一个请求里不要混用两种 session。如果一个接口既要查配置又要跑 Agent，统一用 async 版本
- SQLModel 的 `session.exec()` 在 AsyncSession 上不可用，要用 SQLAlchemy 原生写法：

  ```python
  # 同步（现有风格）
  agent = session.exec(select(Agent).where(Agent.id == agent_id)).first()

  # 异步
  result = await session.execute(select(Agent).where(Agent.id == agent_id))
  agent = result.scalar_one_or_none()
  ```

### 7.4 加连通性测试

新建 `backend/tests/core/test_async_db.py`：

```python
import pytest
from sqlalchemy import text

from app.core.async_db import async_session_maker


@pytest.mark.asyncio
async def test_async_engine_connects() -> None:
    async with async_session_maker() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1
```

`pytest-asyncio` 需要配置默认模式，在 `backend/pyproject.toml` 里加：

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

用 `auto` 模式可以省掉每个 async 测试上的 `@pytest.mark.asyncio` 装饰器，但上面的测试保留装饰器也无妨。

### 7.5 验收

```bash
cd backend
uv run pytest tests/core/test_async_db.py -x
bash scripts/lint.sh
bash scripts/test.sh
```

---

## 任务 8：启用 pgvector 扩展的 migration

向量列本身在阶段 08 才建，但扩展要现在就启用，因为它是数据库级别的一次性操作。

### 8.1 生成空迁移

```bash
cd backend
uv run alembic revision -m "Enable pgvector extension"
```

注意用 `revision` 而不是 `revision --autogenerate` —— 这个迁移不来自模型变更。

### 8.2 填内容

编辑生成的文件（`app/alembic/versions/<hash>_enable_pgvector_extension.py`）：

```python
def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade():
    op.execute("DROP EXTENSION IF EXISTS vector")
```

确认 `down_revision` 指向当前 head `fe56fa70289e`。

### 8.3 验收

```bash
cd backend
uv run alembic upgrade head
uv run alembic current
# 显示新迁移为 head

docker compose exec db psql -U postgres -d app -c "SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';"
# 有一行结果

uv run alembic downgrade -1 && uv run alembic upgrade head
# 回滚再升级不报错，证明 downgrade 写对了
```

---

## 任务 9：建目录骨架

为后续阶段建好空目录和占位文件，避免各阶段自己发明结构。

```
backend/app/agent/
├── __init__.py
├── models/
│   └── __init__.py
└── tools/
    └── __init__.py
backend/app/services/
└── __init__.py
backend/app/worker/
└── __init__.py
backend/tests/core/
└── __init__.py
backend/tests/agent/
└── __init__.py
```

每个 `__init__.py` 留空即可（或者写一行模块 docstring 说明用途，例如 `"""Agent runtime: LangGraph state machine, model adapters and tool execution."""`）。

**不要**在这一步写任何实现代码。

前端目录等到对应阶段再建，因为空目录在 git 里不会被跟踪。

### 验收

```bash
cd backend
uv run python -c "import app.agent, app.services, app.worker; print('skeleton ok')"
bash scripts/lint.sh
```

---

## 任务 10：更新开发文档

`development.md` 里的启动命令需要加上新服务。把：

```bash
docker compose up -d db mailpit
```

改成：

```bash
docker compose up -d db redis minio mailpit
```

同时在"Docker Compose Files and Environment Variables"章节补一段说明 Redis / MinIO 的用途和本地访问地址。`backend/README.md` 里的同类命令也要改。

---

## 阶段验收清单

2026-09-17 按当前阶段 05 代码补验闭环。原来仅适用于阶段 01 的 Item 和相对迁移命令，按下列等价验证处理；不声称重新运行了历史代码。提交代码仍需用户另行授权，不作为运行验收的一部分。

- [x] `cd backend && uv sync --locked` 无错误，锁文件与依赖一致；本次不提交代码
- [x] StateGraph、AsyncPostgresSaver、ChatOpenAI、Vector 实际 import 成功
- [x] 无需回退：实际 Python 3.14.5，原 3.13 回退检查不适用
- [x] `rg -i "full.stack.fastapi|fastapi.template" backend/app frontend/src` 在业务源码无命中
- [x] models 包导入和 metadata 正常：当前包含 User、Agent 等九张业务表；Item 已按阶段 02 移除，不再要求导入 Item
- [x] app 库和专用测试库 `uv run alembic check` 均输出 `No new upgrade operations detected.`
- [x] `from app import crud` 后 `callable(crud.authenticate)` 为 true
- [x] `docker compose ps` 中 db、redis、minio、mailpit 均 healthy（复用已运行服务）
- [x] `docker compose exec -T redis redis-cli ping` 返回 PONG
- [x] MinIO 控制台使用 `.env` 的 S3 账号登录，HTTP 204 后进入 `/browser`，截图已检查
- [x] app 库 `uv run alembic upgrade head` 成功，`vector` 扩展版本为 0.8.6
- [x] 专用库阶段 01 迁移 `88cdef47b355 → fe56fa70289e → 88cdef47b355` 往返成功
- [x] `tests/core/test_async_db.py` 在本次全量后端测试中通过
- [x] lint 脚本的等价命令 mypy、ty、ruff check、ruff format --check 全通过
- [x] test 脚本的等价 coverage/pytest 命令全通过：141 项、覆盖率 90%，生成 HTML 报告
- [x] `bun run lint` 和前端生产构建通过
- [x] Windows 已验证的 Uvicorn Selector 启动方式下 `/docs` 返回 200，Playwright 超管登录通过；不将其冒充为原 `fastapi dev` 命令的重跑

---

## 常见坑

**`alembic check` 说要删表**
`app/models/__init__.py` 漏了导出某个模型。SQLModel 的 metadata 只包含被 import 过的表。

**`Relationship` 报 "could not determine join condition" 或 NameError**
前向引用没 rebuild。确认 `__init__.py` 末尾有 `User.model_rebuild()` 和 `Item.model_rebuild()`，且它们在所有 import 之后。

**`MissingGreenlet` 错误**
在 async 上下文里触发了同步 lazy load。检查 `async_sessionmaker` 是否设了 `expire_on_commit=False`，以及是否在 async 代码里用了 `session.exec()`（应该用 `await session.execute()`）。

**MinIO 健康检查一直 unhealthy**
老版本 minio 镜像没有 `mc` 命令。改用 HTTP 检查：

```yaml
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:9000/minio/health/live || exit 1"]
```

**db 换镜像后启动失败**
本地开发直接 `docker compose down -v` 重置数据卷，然后重跑 `prestart.sh`。

**`uv add` 报 workspace 相关错误**
确认在 `backend/` 目录执行，不是仓库根目录。根目录的 `pyproject.toml` 只有 dev 工具依赖。

---

## 偏差记录

- 2026-09-17 闭环范围：用户授权修复现有数据库排序规则并补齐 01/02 验收。先备份再重建索引并刷新 collation 版本；历史迁移回退仅在新建专用测试库执行，不回退 app 库。Windows worker 使用隔离安装的 arq 做兼容性探针，配套 `backend/scripts/check_windows_worker.py` 和 06 的启动约定属于本次明确范围，不提前实现生产 worker。为补齐阶段 02 的 v1/v2 不可变性浏览器证据，扩展现有 `frontend/tests/agents.spec.ts`。

在这里记录实际执行时与本文档不一致的决定及原因，供后续阶段参考。

- Python 版本：3.14（实际验证为 3.14.5，无需回退）
- LangGraph / LangChain 实际版本：langgraph 1.2.11、langchain-core 1.6.2、langchain-openai 1.6.2、langgraph-checkpoint-postgres 3.1.2
- 其它偏差：Windows 本地测试的默认 ProactorEventLoop 不受 psycopg 异步连接支持，因此 `test_async_db.py` 使用 Selector event-loop policy fixture；当前 pytest-asyncio 1.4.0 与 Python 3.14 会对此兼容方式给出弃用警告。当前环境无 WSL `/bin/bash`，阶段中的 shell 包装脚本使用其内部等价的 `uv run` 命令逐项验证。

### 2026-09-17 数据库排序规则修复与补验

- 原因：当前 pgvector 镜像提供的 libc collation 为 2.36，既有 `app`、`postgres`、`template1` 记录为 2.41。检查未发现 public schema 的分区表、物化视图、CHECK 或排斥约束；修复所有索引而非仅更新版本号。
- 操作前分别执行 `pg_dump -Fc`，备份保存在 `C:/Users/ZhangYan/AppData/Local/AgentHub/backups/collation-20260917/`，三个归档均通过 `pg_restore --list` 检查。`app.dump` 为 48830 字节；未重置数据卷、未删除业务数据。
- 对三个库逐个执行 `REINDEX DATABASE <db>`、`REINDEX SYSTEM <db>`，成功后再 `ALTER DATABASE <db> REFRESH COLLATION VERSION`；每个连接设置 lock_timeout 5 秒、statement_timeout 60 秒。依据：[PostgreSQL collation 修复说明](https://www.postgresql.org/docs/18/sql-altercollation.html)。
- 结果：三个库记录版本和运行版本均为 2.36，重连不再产生该告警，app 无无效索引。`template0` 的版本为空是原有状态，未修改；另外两个旧测试库本来就是 2.36。
- 修复前后 app 全部十张 public 表（含 alembic_version）的行数和内容 MD5 一致，记录在备份目录的 `app-before.txt` / `app-after.txt`。当时 Run 29 条、RunEvent 141 条、消息 47 条，校验未暴露正文或凭据。
- 修复后使用默认 template1 成功创建 `agenthub_phase0102_audit`，历史迁移往返和全量测试均只在该专用库执行。测试后保留其空业务表，未回退 app 库。
- MinIO 登录截图为备份目录内 `minio-login.png`；后端 coverage 位于 `backend/htmlcov/index.html`；Agent 管理浏览器验证含登录共 2 项通过。
- Windows worker 独立兼容验证通过，生产依赖未新增 arq。启动和已确认限制见 [阶段 06 Windows 约定](06-async-worker.md#windows-本地-worker-兼容约定)。本次没有消除 pytest-asyncio 的历史 policy 弃用告警，不影响当前测试通过。
