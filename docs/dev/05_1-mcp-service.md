# 05_1 — 独立 MCP 服务与真实调用验收

## 前置依赖

阶段 [05-tools.md](05-tools.md) 的验收清单全部通过。开工前阅读 `AGENTS.md`、[00-overview.md](00-overview.md) 和阶段 05 的偏差记录。

本阶段是新增的必经阶段，依赖顺序为 **05 → 05_1 → 06**。05 的真实 MCP 连通性原为可选，本阶段将其补为必验项。2026-09-17 已完成实现与实际验收，证据见文末记录；阶段 06 的 worker 与恢复验证仍需独立执行。

## 目标与范围

交付一个随项目启动、可重复测试的独立 MCP Server。AgentHub 通过网络调用它，完成 Tools 试跑、版本绑定、Playground 与 Run 轨迹的真实闭环，为阶段 06 的 worker 使用同一服务建立基线。

- 服务使用官方 Python SDK 的 `FastMCP`，作为独立进程和 Compose 服务 `mcp-docs` 运行，不挂载到 AgentHub API 进程内。
- 新服务以 **Streamable HTTP `/mcp`** 为入口；AgentHub 新增 `streamable_http` 客户端配置，同时保留阶段 05 已有的 `sse` 配置兼容。
- 不实现 stdio，不让用户指定命令或启动进程；`ALLOW_STDIO_MCP_TOOLS` 保持关闭。
- 只提供项目开发文档的只读查询。没有用户数据、外部付费 API、文件写入、任意路径读取或网络代理功能。
- 不新增 MCPServer 数据表、服务注册中心、自动导入全量工具、OAuth 平台或独立管理页面；复用现有 Tool、AgentToolBinding 和 Tools UI。
- 不提前实现 worker、Checkpoint、审批、RAG 或评测；这些继续由 06–10 承担。

## 传输与版本约定

| 场景 | 配置 / 行为 |
|---|---|
| AgentHub 保存的配置 | `transport: "streamable_http"`，只使用这一种拼写 |
| Python SDK 服务启动参数 | 当前锁定 SDK 的 `transport="streamable-http"` |
| 旧 MCP SSE 配置 | 保持 `transport: "sse"`，不自动迁移已有工具 |
| 新服务状态 | `stateless_http=True`、`json_response=True`，不存储用户会话或业务状态 |
| 主机上的 API 访问 | `http://127.0.0.1:3001/mcp` |
| Compose 内的 API / 后续 worker 访问 | `http://mcp-docs:3001/mcp` |

以上两种 URL 对应不同运行拓扑，Tool 配置必须使用执行方可达的地址。容器里的 `localhost` 是容器自身，不能复制主机地址作为 worker 配置。

以当前 `uv.lock` 中 MCP SDK 1.30.0、langchain-mcp-adapters 0.3.2 为起点，先验证本地安装源码和真实握手，再决定是否需要最小依赖调整。服务直接导入 `mcp`，因此应把它声明为后端直接依赖。不要照抄 SDK 主分支的新 API，也不要为追逐新协议版本升级整个依赖树。

参考：[MCP 2025-11-25 传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)、[官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk)。Streamable HTTP 与旧 HTTP+SSE 是不同传输；`json_response=True` 仍是 MCP 协议响应，不是另造普通 REST API。记录实际协商的协议版本，按锁定 SDK 支持范围验收，不宣称覆盖所有协议版本。

## 任务 1：实现只读文档服务

新建 `backend/app/mcp_server/`，包含 `__init__.py`、`server.py` 和必要的局部读取函数。入口约定为 `uv run python -m app.mcp_server.server`，不导入 AgentHub 的全局业务配置，不要求数据库、LLM key 或用户认证初始化。

仅暴露两个工具：

| 工具 | 输入 | 输出约定 |
|---|---|---|
| `search_dev_docs` | `query`：非空字符串，最多 200 字符；`limit`：整数 1–10，默认 5 | `data` 为文档 ID、标题和摘要列表，`count` 为本次返回条数；无结果返回空列表 |
| `read_dev_doc` | `doc_id`：服务目录中存在的文档 ID | 文档 ID、标题、正文及 `truncated` 标记；未知 ID 返回明确的工具错误 |

实现要求：

1. 固定读取只读目录内的 `*.md`，目录由部署方通过 `MCP_DOCS_ROOT` 配置；请求不能修改根目录。使用文件 stem 作为 `doc_id`，从启动时建立的目录映射取路径，不把用户输入直接拼成文件路径。
2. 验证解析后的文件仍在指定根目录内，拒绝逃逸根目录的符号链接；不读取 `.env`、凭据、用户上传文件、仓库其它目录或隐藏文件。
3. 搜索采用确定性的文本匹配、稳定排序和数量限制，不接入向量库、Embedding 或模型。标题来自首个 Markdown 标题，摘要最多 500 字符。
4. 每个响应序列化后的内容限制在 16000 字符内，为平台的 20000 字符工具输出上限留余量；裁剪后仍须是完整 JSON，不截断序列化字符串造成无效 JSON。读大文档返回 `truncated=true`。
5. 提供 `/health` 健康端点；根目录不存在或无法读取时启动失败，不返回假健康。启动、关闭使用 SDK 的正式生命周期。
6. 工具声明只读注解，但注解不作为权限或审批依据。返回的文档正文是工具数据，不作为系统指令；AgentHub 仍按普通 ToolMessage 处理。

项目开发文档在本阶段被视为部署方明确允许平台用户读取的共享资料。不得直接把该目录替换成私有业务资料；私有知识的用户隔离由阶段 08 实现。

## 任务 2：增加部署方式与定向网络授权

按现有 Compose 结构增加 `mcp-docs`：

- 优先复用后端构建镜像和依赖，以不同启动命令运行；需要时在 `backend/Dockerfile` 中把开发文档复制到独立只读目录。开发覆盖文件可挂载 `docs/dev` 为只读。
- 容器监听 3001；只在开发覆盖文件发布 `127.0.0.1:3001:3001`。部署不发布宿主机端口，不添加 Traefik 公网路由。
- 不向服务传入数据库连接、LLM key、S3 凭据或宿主环境的全部变量。使用非 root 用户，仅给予读取文档所需权限。
- 配置健康检查。主 API 不因可选文档服务宕机而无法启动；真正调用失败时由工具错误反馈。

内部地址会被阶段 05 的默认 SSRF 策略阻断，**不能用 `ALLOW_PRIVATE_TOOL_URLS=true` 全局放开作为本阶段交付方案**。增加仅针对 MCP 的部署方配置 `MCP_TRUSTED_SERVER_URLS: list[str] = []`：

1. 主机开发环境显式配置 `http://127.0.0.1:3001/mcp`；Compose API 配置 `http://mcp-docs:3001/mcp`，阶段 06 worker 透传同一配置。
2. 仅精确匹配规范化后的 scheme、host、port 和 path；拒绝用户信息、query、fragment、通配符及路径前缀匹配。可仅统一 host 大小写和默认端口，不做宽松路径解码。
3. MCP 创建/更新、执行和每次实际 HTTP 请求都执行检查。新 Streamable HTTP 服务所有协议请求仅向 `/mcp` 发起，禁止重定向。
4. 信任列表为空时保持现有公网 DNS/IP 校验；列表不是公开 API 字段，普通用户不能扩展它。该例外不适用于普通 HTTP 工具，也不应用于旧 SSE 端点的任意后续地址。
5. 默认全局私网开关和 stdio 开关继续为 false。服务端同时保留 Host / Origin 检查，精确配置实际主机名；不要通过关闭 SDK transport security 来解决 403。

这是对阶段 05“默认禁止私网”的定向补充，只允许访问部署方提供的共享只读 MCP 服务。该服务不提供公网或私有用户数据访问；若将来需要外网暴露或带身份的第三方 MCP，另行设计 TLS、身份认证和凭据存储，本阶段不承诺支持。

## 任务 3：扩展现有 MCP 客户端

修改 `backend/app/agent/tools/mcp.py`，复用现有执行器和 ToolResult，不另建一套运行链路。

配置示例（Compose 场景）：

```json
{
  "transport": "streamable_http",
  "url": "http://mcp-docs:3001/mcp",
  "tool_name": "search_dev_docs"
}
```

要求：

- `validate_config` 接受 `sse` 和 `streamable_http`，拒绝 stdio 和其它值。网络失败只在试跑/执行中处理；保存配置时继续检查 URL 和结构。
- Streamable HTTP 使用 adapters 的对应连接类型。新服务是无状态的，优先每次调用建立独立 MCP 会话，使用上下文管理关闭；不为它复用现有 SSE 的单队列会话。一次超时或取消不得连带取消其它并发调用。
- 旧 SSE 会话保留 TTL 回收；缓存键至少包含事件循环、transport、URL，避免不同传输混用。补齐执行器的幂等异步关闭方法，保证生命周期结束后无悬挂连接任务。
- 连接、初始化、工具调用和响应读取都纳入工具超时。预期服务错误返回 `ok=False`；`CancelledError` 继续传播给 Run 取消流程。
- 不在执行器内自动重试 `call_tool`：连接中断时服务可能已执行，自动重试可能重复副作用。下一次显式调用可以重建连接；阶段 06 负责整体任务恢复策略。
- 处理 text、structured content 和 `isError`，保证空结果、未知工具和协议错误有可解释结果。错误不暴露内部堆栈或请求凭据。
- API lifespan 关闭 MCP 执行器；未来 worker 在自身 shutdown 调同一关闭入口。服务端进程由部署管理，客户端不得在请求里启动或关闭它。

## 任务 4：完成 Tools UI 与使用说明

扩展现有 `frontend/src/components/Tools/ToolForm.tsx`：

- MCP 传输下拉新增 Streamable HTTP，新增工具默认选它；编辑已有 SSE 工具保持原值。
- URL 提示区分主机与 Compose 网络地址，工具名填写服务暴露的协议名称。不要把 SDK 参数拼写混入用户配置。
- 仍使用现有参数行编辑器。通过真实 `tools/list` 获取并核对上述两个工具的 `inputSchema`，在使用文档中给出与服务一致的 Tool 配置；暂不增加“导入所有工具”接口。
- `TestTool`、版本发布和 `ToolTrace` 复用现有能力。若 OpenAPI 发生变化，立即重新生成客户端，禁止手改生成物。
- 在 `development.md` 中记录服务启动、健康检查、两种地址、定向信任配置、两个工具的参数及预期结果；README 更新当前支持的传输方式。

验证数据通过正常 Tool/Agent API 或 UI 创建，不添加生产自动种子逻辑。集成测试自行创建并清理其专用资源，不删除开发者已有数据。

## 任务 5：测试与真实验收

### 5.1 不依赖真实模型的自动化测试

- 文档工具：稳定搜索、无结果、未知 ID、参数上下界、路径穿越、符号链接逃逸、输出大小与合法 JSON。
- 网络边界：可信的指定 `/mcp` 可访问；同主机其它路径、其它端口、带用户信息的 URL、metadata 地址和重定向被拒绝；普通 HTTP 工具仍拒绝私网。
- 使用临时文档目录和独立子进程启动真实 MCP Server，在回环端口完成初始化、`tools/list` 和两个 `tools/call`。不能用 monkeypatch 代替这组真实网络验证。
- 经 AgentHub `/tools/{id}/test` 调用真实服务，验证输出、参数错误、未知工具、服务停机后的错误、重启后新调用成功。不能只用 SDK 直连代替平台验证。
- 超时/取消：延迟工具只存在于测试用 MCP 服务，不暴露到正式服务；验证并发调用互不误取消、连接被清理、之后仍可调用。
- 无 LLM key 时，真实服务集成测试仍必须运行；真实模型端到端测试独立标记，验收时必须实际配置并跑通一次。现有默认不联网的单元测试不因新增集成测试失效。
- SSE 客户端回归、Function/HTTP 工具、迭代守卫及同名并行工具事件匹配保持通过。

新增测试建议位于 `backend/tests/mcp_server/test_documents.py`、`backend/tests/integration/test_mcp_service.py`、现有 `test_tools_mcp.py` 与 `frontend/tests/tools.spec.ts`。在 pytest 配置中注册集成标记，普通全量命令默认不运行需独立服务的测试；显式集成命令必须覆盖真实网络组，不能全部 skip 后报告通过。固定命令与所用服务地址写入偏差记录。

### 5.2 浏览器与 Compose 验收

1. 启动 `mcp-docs`，确认健康端点和真实协议调用均成功。
2. 在 Tools 创建 `search_dev_docs`，试跑查询“Checkpoint”，验证结果包含开发文档 ID 和摘要。
3. 创建 `read_dev_doc`，试跑读取 `06-async-worker`，验证返回该阶段标题和正文。
4. 绑定两个工具、发布 Agent 版本，在 Playground 要求“先搜索项目的 Checkpoint 说明，再读取对应阶段文档，说明恢复职责，并给出文档 ID”。确认真实模型调用两个 MCP 工具，不能只凭最终答案正确判定成功。
5. 检查 Playground 工具块和持久化 RunEvent：名称、参数、index、结果、耗时一致，模型回复引用文档 ID。
6. 停止服务后试跑，确认返回工具错误而非未捕获 500；让模型执行失败工具调用后合理解释失败，Run 正常收尾。重启服务后再次调用成功。
7. Compose API 使用 `http://mcp-docs:3001/mcp` 重复真实调用；全局私网与 stdio 开关均为 false。保存 Run ID、命令结果和页面截图。

Windows 验证沿用已确认的 SelectorEventLoop API 启动方式；不要假设测试进程和正式 API 的事件循环一致。无 bash 时使用脚本内部等价命令并记录，不以缺少 bash 为理由跳过验证。

## 后续阶段的使用契约

| 阶段 | 必须保持的约定 |
|---|---|
| 06 异步与恢复 | worker 使用容器可达 URL 和相同信任配置；独立创建/关闭客户端。工具实例、连接、队列、future 和 MCP session ID 不入 Checkpoint；恢复时按 AgentVersion 重新加载工具并建立新连接。用本阶段只读服务验收重投，不宣称外部调用 exactly-once |
| 07 审批 | 复用相同工具执行器；审批前不得发出 `tools/call`，拒绝后不得执行。可将只读 MCP 工具标记需审批来验证门禁，无需新增有副作用的演示服务；服务工具注解不覆盖平台的 `requires_approval` |
| 08 RAG | 此服务检索固定开发文档，仅作外部工具示例；用户私有知识继续走知识库归属校验、MinIO 与 pgvector，不借此服务绕过隔离 |
| 09 评测 | 可以复用这两个工具；固定文档语料版本，断言调用名和文档 ID，避免断言模型回复逐字一致；服务不可用必须计入失败，不能静默跳过 |
| 10 可观测性 | 复用工具事件统计 MCP 延迟/错误，区分连接错误、协议错误和工具错误；不要把完整 URL、文档正文或动态参数作为指标标签 |

阶段 06 的 worker 真实 MCP 调用与恢复验证仍属于 06，不能在本阶段提前勾选。

## 允许修改的文件范围

- 服务：`backend/app/mcp_server/`、`backend/Dockerfile`、`compose.yml`、`compose.override.yml`、`compose.deploy.yml`。
- 客户端与配置：`backend/app/agent/tools/mcp.py`、`registry.py`、`backend/app/core/config.py`、`backend/app/main.py`；必要时局部修改 HTTP 校验辅助函数，不放宽普通 HTTP 策略。
- UI：`frontend/src/components/Tools/`；仅 OpenAPI 改动时涉及路由和生成客户端。
- 测试与依赖：上述测试文件、必要 fixture、`backend/pyproject.toml`、`uv.lock`。
- 文档：`development.md`、`README.md`、本文件及关联阶段的契约说明。

默认不改数据库模型、迁移、Agent 发布语义或 Run 公共接口；确有必要先记录原因。保留现有 `.env` 用户修改，不写入密钥。仅更新依赖树中的阶段关系不代表已授权实现后续功能。

## 阶段验收清单

- [x] MCP SDK / adapters 版本、实际协议版本与 Streamable HTTP 配置已记录。
- [x] 独立 `mcp-docs` 可在主机及 Compose 启动，健康检查有效，部署没有公网暴露。
- [x] 两个只读工具通过真实初始化、`tools/list`、`tools/call`，schema 与 UI 配置一致。
- [x] 路径隔离、参数边界、合法 JSON 和输出上限测试通过。
- [x] 默认安全开关关闭时，只有指定 MCP 私网端点可访问；SSRF 与重定向回归通过。
- [x] 服务停机、重启、错误、超时及取消测试通过，无连接任务泄漏、无自动重试调用。
- [x] API 退出关闭客户端，并发调用的取消互不误伤，SSE 兼容测试通过。
- [x] Tools 试跑、Agent 发布、真实模型调用两个工具及 RunEvent 可追踪全部通过。
- [x] Compose API 到独立 MCP 服务的真实调用通过，记录 Run ID 和截图。
- [x] 后端 lint、普通全量测试及独立 MCP 集成测试通过；前端 lint、构建和相关 Playwright 通过。
- [x] `alembic check` 无差异；OpenAPI 客户端已重新生成。
- [x] 使用文档已说明启动、URL、信任配置、工具 schema 与 06 worker 的交接要求。

## 偏差记录

- 计划调整：新增 05_1，将真实 MCP 服务接入由 05 的可选验收改为 06 的必备前置；新服务采用 Streamable HTTP，保留已有 SSE 客户端兼容，stdio 不实现。
- 实际版本：Python 3.14.5、`mcp==1.30.0`、`langchain-mcp-adapters==0.3.2`；真实初始化协商协议 `2025-11-25`。服务使用 `stateless_http=True`、`json_response=True`、`/mcp`，平台配置为 `streamable_http`。直接声明 MCP 依赖，没有升级其它依赖。
- 服务数据：启动时加载 `docs/dev/*.md` 快照；编辑文档后须由运维重启服务才会更新快照。工具使用 text content 内的合法 JSON（`structured_output=False`），避免 SDK 对返回字符串再包装；序列化后的总长度限制 16000 字符，读取截断时标记 `truncated=true`。本次 06 文档读取确实触发截断，模型明确说明了这一限制。
- 安全与网络：主机 API 使用 `http://127.0.0.1:3001/mcp`，Compose API 使用 `http://mcp-docs:3001/mcp`；分别配置精确的 `MCP_TRUSTED_SERVER_URLS`。普通 HTTP 工具没有取得该豁免，私网及 stdio 开关均为 false。客户端每次请求重新校验地址、拒绝重定向；禁用环境代理，避免本机代理将回环 MCP 请求转发并返回 502。可信端点匹配包含协议、主机、端口和路径，不接受查询、凭据、通配、转义路径或端口 0。
- 运行方式：主机独立 Python 进程及 Compose 服务均启动并通过健康/协议检查。Docker 镜像构建成功；服务以 UID/GID 65532、只读根文件系统运行。开发覆盖仅发布 `127.0.0.1:3001`；显式组合 `compose.yml + compose.deploy.yml` 的配置检查确认无端口发布、无 Traefik 标签、无业务密钥环境变量。只进行了部署配置检查，没有执行生产部署。
- 后端正式检查：在 `backend/` 执行 `uv run mypy app`（61 文件）、`uv run ty check app`、`uv run ruff check app`、`uv run ruff format app --check`，全部通过。新增测试另行 Ruff 检查通过。额外尝试不限定目录的 `ty check` 会扫描测试与阶段 06 探针，发现既有测试类型问题和未安装的 arq；它不是 `scripts/lint.sh` 的检查范围，本阶段未扩大修改。
- 普通全量：独立数据库 `agenthub_phase0102_audit`，`uv run coverage run -m pytest tests/`，151 passed、2 skipped，覆盖率 89%；独立集成测试由默认 marker 排除。端口 0 边界修正后另跑 MCP 单测 9 passed。Windows 跳过的两项仅因无符号链接权限（WinError 1314）；Linux 容器 `pytest tests/mcp_server -q` 实际 8 passed，覆盖两项链接隔离，未以 skip 代替验收。
- 真实集成：`uv run pytest tests/integration/test_mcp_service.py -m mcp_integration -x -q`；不设真实模型开关时三个协议/平台/取消并发测试不需要 LLM。设置 `MCP_LIVE_MODEL_TEST=1` 后实跑 4 passed（18.39 秒），增加真实模型的服务停机失败解释与重启恢复。测试均只启动/停止自己创建的临时服务；工具/Agent 经 API 创建并在结束后清理，测试库与开发库隔离。
- 浏览器：主机 API 的真实 MCP 场景 2 passed（含登录准备）；Compose API 相关回归 11 passed，覆盖 Agent、Run、SSE、Tools、旧 SSE 编辑、SSRF、同名事件匹配及真实 MCP 搜索→读取。命令见下方。`bun run lint`、`bun run --filter frontend build`（TypeScript/Vite）通过。
- 数据库及生成物：未新增模型和迁移；开发库及独立测试库 `alembic check` 无差异，head 仍为 `6b47e6b4b2fe`。按生成脚本等价步骤导出 OpenAPI、生成客户端并 lint，未手工修改生成物。Windows 无 bash 时执行对应工具命令。
- 实际证据：Compose 成功 Run `4cea575f-740b-454f-8fbc-0512c3d3945d`，工具搜索/读取分别成功，最终输出引用 `06-async-worker`；截图和完整 Run/Event JSON 位于 `frontend/test-results/phase051-compose/tools-real-model-searches--6c453-the-independent-MCP-service-chromium/`（`mcp-test-0.png`、`mcp-test-1.png`、`mcp-playground.png`、`mcp-run.png`、`mcp-live-run.json`）。主机截图在 `frontend/test-results/phase051-host-live/` 对应目录。截图已经打开检查。
- 失败证据：隔离服务停机的真实模型 Run `95f18abb-39c2-4b22-913c-f1b9bede6c7f`，仅一次工具调用，结果 `ok=false` / `ConnectError`，模型说明无法读取，Run 为 succeeded，服务恢复后试跑成功。JSON 位于 `frontend/test-results/phase051-isolated/mcp-unavailable-run.json`。测试清理会删除专用 Agent 及级联 Run；这些 Run ID 用于关联保留的本地证据，不能假定开发库永久保存测试数据。`test-results` 属于忽略的本地产物，需交付证据时单独归档。
- 停机验收调整：自动审批拒绝包含停止当前 `mcp-docs` 容器的命令，仅返回“策略阻止”。未重试停止该容器；改为对测试自行创建的隔离服务执行停机/重启，并通过真实模型与平台 API 验证，现有 Compose 服务保持运行。停机后的真实模型解释由后端集成验证，成功链路由浏览器验证。
- 最终运行状态：完成端口校验修正后重新构建后端镜像并仅更新 backend 容器；容器内通过正式执行器再次调用 `search_dev_docs` 成功，安全开关仍为 false，backend / mcp-docs 均 healthy。现有 mcp-docs 保持原进程，其文档快照不包含本次最后追加的验收记录。

复现命令（PowerShell，先配置独立测试数据库与真实模型环境，不要对开发库运行清表 fixture）：

```powershell
# backend/；DATABASE_URL 指向专用测试库
$env:MCP_LIVE_MODEL_TEST='1'
$env:MCP_LIVE_EVIDENCE_DIR='D:\AgentHub\frontend\test-results\phase051-isolated'
uv run pytest tests/integration/test_mcp_service.py -m mcp_integration -x -q

# frontend/；API 已在 Compose 运行
$env:PLAYWRIGHT_BASE_URL='http://localhost:8000'
$env:VITE_API_URL='http://127.0.0.1:8000'
$env:MCP_SERVICE_URL='http://mcp-docs:3001/mcp'
bunx playwright test tests/tools.spec.ts tests/agents.spec.ts tests/runs.spec.ts tests/sse.spec.ts --grep-invert 'uses function and HTTP' --workers=1 --reporter=line --output=test-results/phase051-compose
```
