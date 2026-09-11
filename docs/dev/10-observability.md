# 10 — 可观测性与管理监控

## 前置依赖

阶段 [06-async-worker.md](06-async-worker.md) 的验收清单全部通过。

本阶段与 07 / 08 / 09 相互独立，但**建议放在最后做**：埋点的价值取决于有多少业务逻辑可埋，越晚做覆盖面越广。如果 07 / 08 / 09 已完成，本阶段要一并覆盖它们的指标。

## 本阶段目标

让平台的运行状况可以被观察和量化：

- 结构化日志，带 run_id 关联
- OpenTelemetry 链路追踪，能看到一次 Run 内部各节点的耗时分布
- Prometheus 指标：请求量、Run 成功率、模型 token 用量、工具调用延迟
- Grafana 面板
- 管理员监控页：用户、Agent、失败 Run、模型用量、系统状态

## 本阶段不做什么

不做告警规则和通知（Grafana 能配但演示价值低）、不做日志聚合系统（Loki / ELK，本地开发看容器日志够用）、不做分布式追踪的跨服务传播（只有两个进程）。

---

## 三类信号的分工

避免重复建设，先明确各自负责什么：

| 信号 | 工具 | 回答什么问题 | 保留期 |
|------|------|------------|--------|
| 业务事件 | `run_event` 表（已有） | 这次 Run 具体发生了什么，用户能看 | 长期 |
| 指标 | Prometheus | 整体趋势如何，成功率降了吗，成本涨了吗 | 15 天 |
| 链路 | OpenTelemetry | 这次慢在哪个环节，哪个节点耗时最长 | 短期（本地不持久化） |
| 异常 | Sentry（已有） | 哪里抛异常了，堆栈是什么 | 按 Sentry 配置 |

**`run_event` 表已经承担了"业务可观测性"**，它是给用户看的。本阶段加的是给运维和开发看的那一层。不要把 `run_event` 的内容重复塞进 Prometheus（高基数标签会拖垮它）。

---

## 任务 1：结构化日志

### 1.1 现状问题

模板里没有统一的日志配置，各处用 `logging.getLogger(__name__)` 直接输出。问题是：worker 里同时跑几个 Run 时，日志交织在一起无法区分哪行属于哪个 Run。

### 1.2 上下文注入

用 `contextvars` 在整条异步调用链上传递 run_id。新建 `backend/app/core/logging.py`：

```python
import logging
from contextvars import ContextVar

run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)


class ContextFilter(logging.Filter):
    """把 contextvar 里的 run_id / user_id 注入每条日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get() or "-"
        record.user_id = user_id_var.get() or "-"
        return True


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s [run=%(run_id)s user=%(user_id)s] "
            "%(name)s: %(message)s"
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # 这些库的 INFO 日志噪音太大
    for noisy in ("httpx", "httpcore", "urllib3", "arq.worker"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
```

**`contextvars` 在 asyncio 里是每个 task 独立的**，这正是我们要的：worker 里并发的几个 Run 各有自己的 run_id，不会串。用 thread-local 做不到这一点。

在 `execute_run` 和 `execute_run_task` 开头设置：

```python
    token = run_id_var.set(str(run.id))
    try:
        ...
    finally:
        run_id_var.reset(token)
```

**必须用 `reset(token)` 而不是 `set(None)`**。前者恢复到之前的值，后者会破坏嵌套场景。

在 `app/main.py` 和 `app/worker/main.py` 的启动处调 `setup_logging()`。

### 1.3 JSON 格式（可选）

如果要接日志聚合系统，改成 JSON 输出。用 `python-json-logger` 或手写 formatter。本地开发用人类可读格式更方便，所以加一个配置开关：

```python
    LOG_FORMAT: Literal["text", "json"] = "text"
```

部署时设 `json`。演示项目可以跳过，在偏差记录里写明。

---

## 任务 2：OpenTelemetry

### 2.1 装依赖

```bash
cd backend
uv add \
  "opentelemetry-api" \
  "opentelemetry-sdk" \
  "opentelemetry-exporter-otlp-proto-grpc" \
  "opentelemetry-instrumentation-fastapi" \
  "opentelemetry-instrumentation-sqlalchemy" \
  "opentelemetry-instrumentation-httpx"
```

这几个包的版本必须互相兼容（它们的版本号是联动的）。如果 `uv add` 报解析冲突，指定一个统一版本。

### 2.2 初始化

新建 `backend/app/core/telemetry.py`：

```python
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.core.config import settings


def setup_telemetry(*, service_name: str) -> None:
    """
    初始化 OpenTelemetry。OTEL_EXPORTER_OTLP_ENDPOINT 未配置时不启用，
    这样本地开发和测试不需要跑 collector。
    """
    if not settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        return

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": settings.PROJECT_VERSION,
            "deployment.environment": settings.FASTAPI_ENV or "production",
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)
        )
    )
    trace.set_tracer_provider(provider)


def instrument_app(app: FastAPI) -> None:
    if not settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        return
    FastAPIInstrumentor.instrument_app(app, excluded_urls="/metrics,/api/v1/utils/health-check/")
    SQLAlchemyInstrumentor().instrument(engine=engine)
    HTTPXClientInstrumentor().instrument()


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("agenthub")
```

要点：

- **端点未配置时直接返回，不启用**。这让测试和本地开发不依赖 collector。如果强制启用，每次测试都会有一堆连接失败的告警
- `excluded_urls` 排除 `/metrics` 和健康检查。它们被高频调用，埋进去只是噪音
- `service_name` 参数化，API 进程传 `agenthub-api`，worker 传 `agenthub-worker`，这样在 Grafana 里能区分
- `PROJECT_VERSION` 需要加到 `config.py`，从 `pyproject.toml` 读或者硬编码

配置项加到 `config.py`：

```python
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    PROJECT_VERSION: str = "0.1.0"
```

### 2.3 自定义 span

自动埋点覆盖 HTTP 请求、数据库查询和 httpx 调用。**Agent 执行的内部结构需要手工埋**，这才是真正有价值的部分。

在关键位置加 span：

```python
# app/services/run_service.py
async def execute_run(...) -> Run:
    tracer = get_tracer()
    with tracer.start_as_current_span("agent.run") as span:
        span.set_attribute("agenthub.run_id", str(run.id))
        span.set_attribute("agenthub.agent_version_id", str(run.agent_version_id))
        span.set_attribute("agenthub.llm_model", snapshot.llm_model)
        span.set_attribute("agenthub.trigger", run.trigger.value)
        ...
        # 结束时记录结果
        span.set_attribute("agenthub.status", run.status.value)
        span.set_attribute("agenthub.prompt_tokens", run.prompt_tokens)
        span.set_attribute("agenthub.completion_tokens", run.completion_tokens)
        if run.status == RunStatus.FAILED:
            span.set_status(Status(StatusCode.ERROR, run.error or ""))
```

节点级 span 在 `nodes.py` 的每个节点里：

```python
async def call_model(state, *, tools=None):
    with get_tracer().start_as_current_span("agent.node.call_model") as span:
        span.set_attribute("agenthub.iteration", state["iteration"])
        span.set_attribute("agenthub.tool_count", len(tools or []))
        ...
```

同样给 `execute_tools`、`retrieve_context`、`approval_gate` 加。工具执行的 span 放在 `to_langchain_tool` 的 `_run` 里，带上工具名和类型。

**span 属性不要放大字段**。prompt 内容、模型回复、检索到的文档全文都不要放进 span attribute，会让 trace 体积爆炸。要看内容就去 `run_event` 表。

命名约定：`agent.run`、`agent.node.<name>`、`agent.tool.<type>`、`agent.retrieval`。自定义属性统一 `agenthub.` 前缀。

### 2.4 worker 里的 trace

worker 是独立进程，需要单独初始化。在 `app/worker/main.py` 的 `startup` 里调 `setup_telemetry(service_name="agenthub-worker")`。

**API 进程和 worker 之间的 trace 不会自动关联**（任务通过 Redis 传递，没有 HTTP header 承载 trace context）。要关联需要手工传播：

```python
# 入队时
from opentelemetry.propagate import inject

carrier: dict[str, str] = {}
inject(carrier)
await pool.enqueue_job("execute_run_task", str(run.id), trace_context=carrier)

# worker 里
from opentelemetry.propagate import extract

async def execute_run_task(ctx, run_id: str, trace_context: dict | None = None):
    parent = extract(trace_context or {})
    with get_tracer().start_as_current_span("worker.execute_run", context=parent):
        ...
```

这个关联做出来效果很好（一条 trace 从 HTTP 请求一直延伸到 worker 里的每个节点），值得做。但如果时间紧可以跳过，两个服务的 trace 分开看也能用，在偏差记录里写明。

---

## 任务 3：Prometheus 指标

### 3.1 装依赖

```bash
cd backend
uv add "prometheus-client"
```

不用 `prometheus-fastapi-instrumentator`：它提供的 HTTP 指标有用但不够，业务指标还是要自己定义，不如统一手写。

### 3.2 指标定义

新建 `backend/app/core/metrics.py`：

```python
from prometheus_client import Counter, Gauge, Histogram

# --- HTTP ---
http_requests_total = Counter(
    "agenthub_http_requests_total",
    "HTTP requests",
    ["method", "path", "status"],
)
http_request_duration_seconds = Histogram(
    "agenthub_http_request_duration_seconds",
    "HTTP request duration",
    ["method", "path"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

# --- Run ---
runs_total = Counter(
    "agenthub_runs_total",
    "Agent runs by outcome",
    ["status", "trigger"],
)
run_duration_seconds = Histogram(
    "agenthub_run_duration_seconds",
    "Agent run duration",
    ["trigger"],
    buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
runs_in_progress = Gauge(
    "agenthub_runs_in_progress",
    "Currently executing runs",
)

# --- 模型 ---
llm_tokens_total = Counter(
    "agenthub_llm_tokens_total",
    "LLM tokens consumed",
    ["model", "kind"],          # kind: prompt | completion
)
llm_cost_usd_total = Counter(
    "agenthub_llm_cost_usd_total",
    "Estimated LLM cost in USD",
    ["model"],
)
llm_call_duration_seconds = Histogram(
    "agenthub_llm_call_duration_seconds",
    "LLM call duration",
    ["model"],
    buckets=(0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
llm_errors_total = Counter(
    "agenthub_llm_errors_total",
    "LLM call errors",
    ["model", "error_type"],
)

# --- 工具 ---
tool_calls_total = Counter(
    "agenthub_tool_calls_total",
    "Tool invocations",
    ["tool_type", "outcome"],   # outcome: ok | error | timeout
)
tool_duration_seconds = Histogram(
    "agenthub_tool_duration_seconds",
    "Tool execution duration",
    ["tool_type"],
    buckets=(0.05, 0.1, 0.5, 1, 2.5, 5, 15, 30),
)

# --- 检索（阶段 08） ---
retrieval_duration_seconds = Histogram(
    "agenthub_retrieval_duration_seconds",
    "Vector retrieval duration",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)
retrieval_results_count = Histogram(
    "agenthub_retrieval_results",
    "Number of chunks retrieved",
    buckets=(0, 1, 2, 3, 5, 10),
)

# --- 审批（阶段 07） ---
approvals_total = Counter(
    "agenthub_approvals_total",
    "Approval decisions",
    ["decision"],               # approved | rejected | expired
)

# --- 队列 ---
queue_depth = Gauge(
    "agenthub_queue_depth",
    "Pending jobs in the arq queue",
)
```

### 3.3 标签基数纪律

**这是 Prometheus 最容易搞坏的地方。** 每个不同的标签值组合会创建一个独立的时间序列，基数爆炸会让 Prometheus 内存爆掉。

绝对禁止的标签：

- `run_id`、`user_id`、`agent_id`、`conversation_id` —— 无界基数
- 具体的工具名（用户可以创建任意多个） —— 所以 `tool_calls_total` 用 `tool_type` 而不是 `tool_name`
- 原始 URL 路径（`/agents/{uuid}` 每个 uuid 一个序列） —— 必须用路由模板

`model` 标签是可接受的：一个部署里实际使用的模型数量有限（个位数）。但如果担心用户能自由填模型名导致基数增长，可以只保留白名单里的模型名，其它归为 `other`。

HTTP 的 `path` 标签**必须用路由模板**而不是实际路径：

```python
# 正确：/api/v1/agents/{id}
# 错误：/api/v1/agents/3f2a-....
route = request.scope.get("route")
path = getattr(route, "path", "unknown")
```

拿不到 route 时用 `"unknown"` 而不是实际路径。

### 3.4 HTTP 中间件

```python
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    if request.url.path in ("/metrics", "/api/v1/utils/health-check/"):
        return await call_next(request)

    start = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - start

    route = request.scope.get("route")
    path = getattr(route, "path", "unknown")
    http_requests_total.labels(
        method=request.method, path=path, status=str(response.status_code)
    ).inc()
    http_request_duration_seconds.labels(
        method=request.method, path=path
    ).observe(duration)
    return response
```

**中间件里的异常要注意**：如果 `call_next` 抛异常，指标不会被记录。用 `try/finally` 或者接受这个缺口（异常会被 Sentry 抓到）。

### 3.5 `/metrics` 端点

```python
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest


@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

`include_in_schema=False` 让它不出现在 OpenAPI 文档里，这样也不会进生成的前端客户端。

**端点不能暴露到公网。** 它泄漏内部结构和用量信息。三个选择：

1. 放在 `app` 上而不是 `api_router` 上，路径是 `/metrics`，然后在 Traefik 层限制访问
2. 要求 superuser 鉴权
3. 只在内网监听

选 2 最简单可靠（用 `Depends(get_current_active_superuser)`），但 Prometheus 抓取需要配 bearer token。演示项目里选 1 + `compose.deploy.yml` 里不给它加 Traefik 路由规则，这样公网访问不到。在代码注释和 `deployment.md` 里写明这个约束。

### 3.6 多进程的坑

**`prometheus-client` 的默认模式在多 worker 下会出问题。** `fastapi run --workers 4` 会起 4 个进程，每个进程有独立的指标注册表。Prometheus 抓取时会随机命中其中一个，得到的计数只是那一个进程的。

解决方式是多进程模式：

```python
# 需要设置环境变量 PROMETHEUS_MULTIPROC_DIR 指向一个可写目录
from prometheus_client import CollectorRegistry, multiprocess

def metrics() -> Response:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

`compose.yml` 里给 backend 加环境变量和 tmpfs 挂载：

```yaml
    environment:
      PROMETHEUS_MULTIPROC_DIR: /tmp/prometheus
    tmpfs:
      - /tmp/prometheus
```

注意多进程模式下 `Gauge` 的语义会变（需要指定 `multiprocess_mode`），`runs_in_progress` 这类 Gauge 要加 `multiprocess_mode="livesum"`。

这个坑不处理的话，指标数值会莫名其妙地偏低和跳动，很难排查。

### 3.7 队列深度

`queue_depth` 是 Gauge，需要定期采样。在 `/metrics` 被抓取时实时查：

```python
    depth = await arq_pool.zcard("arq:queue")   # key 名看 arq 版本
    queue_depth.set(depth)
```

或者在 worker 的 cron 里每分钟更新一次。选前者，抓取时才查，没有额外开销。

---

## 任务 4：Compose 加监控栈

### 4.1 OTel Collector

`compose.yml`：

```yaml
  otel-collector:
    image: otel/opentelemetry-collector-contrib:latest
    command: ["--config=/etc/otel-collector-config.yaml"]
    volumes:
      - ./monitoring/otel-collector-config.yaml:/etc/otel-collector-config.yaml:ro
```

新建 `monitoring/otel-collector-config.yaml`：

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

processors:
  batch:
    timeout: 5s

exporters:
  debug:
    verbosity: basic
  otlphttp/tempo:
    endpoint: http://tempo:4318

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
```

**第一版只导到 `debug` exporter**（打印到 collector 日志）。这已经能验证埋点工作正常，而且不需要跑 Tempo/Jaeger。

如果要在 Grafana 里看 trace，加 Tempo 服务并把 exporter 换成 `otlphttp/tempo`。这会多一个服务和一份配置，按时间决定。上面的配置里注释掉的 tempo exporter 就是为此预留的。

### 4.2 Prometheus

```yaml
  prometheus:
    image: prom/prometheus
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.retention.time=15d
    volumes:
      - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - app-prometheus-data:/prometheus
```

`monitoring/prometheus.yml`：

```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: agenthub-api
    metrics_path: /metrics
    static_configs:
      - targets: ["backend:8000"]

  - job_name: postgres
    static_configs:
      - targets: ["postgres-exporter:9187"]

  - job_name: redis
    static_configs:
      - targets: ["redis-exporter:9121"]
```

数据库和 Redis 的 exporter 是可选的（`prometheuscommunity/postgres-exporter` 和 `oliver006/redis_exporter`）。它们提供的指标很有用（连接数、慢查询、内存占用），加上成本也低。

**worker 进程没有 HTTP 端点，Prometheus 抓不到它。** 两个选择：

1. 给 worker 起一个只提供 `/metrics` 的极简 HTTP server（`prometheus_client.start_http_server(9000)`）
2. 用 Pushgateway

选 1，一行代码的事：在 worker 的 `startup` 里调 `start_http_server(9000)`，然后 Prometheus 里加一个 `agenthub-worker` job 指向 `worker:9000`。

### 4.3 Grafana

```yaml
  grafana:
    image: grafana/grafana
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_PASSWORD:-admin}
      - GF_USERS_ALLOW_SIGN_UP=false
    volumes:
      - app-grafana-data:/var/lib/grafana
      - ./monitoring/grafana/provisioning:/etc/grafana/provisioning:ro
      - ./monitoring/grafana/dashboards:/var/lib/grafana/dashboards:ro
```

`compose.override.yml` 里暴露端口：

```yaml
  prometheus:
    ports:
      - "9090:9090"
  grafana:
    ports:
      - "3000:3000"
  otel-collector:
    ports:
      - "4317:4317"
```

`.env` 加 `OTEL_EXPORTER_OTLP_ENDPOINT` 和 `GRAFANA_PASSWORD`。**本地开发时 `OTEL_EXPORTER_OTLP_ENDPOINT` 留空**（不跑 collector 也能开发），在 `compose.override.yml` 里给 backend 和 worker 设成 `http://otel-collector:4317`。

新增三个 volume 声明。

**监控栈不应该在 `compose.deploy.yml` 里暴露到公网**，不加 Traefik 路由规则即可。

### 4.4 Grafana 自动配置

`monitoring/grafana/provisioning/datasources/prometheus.yml`：

```yaml
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
```

`monitoring/grafana/provisioning/dashboards/default.yml` 指向 `/var/lib/grafana/dashboards`，把面板 JSON 放在 `monitoring/grafana/dashboards/agenthub.json`。

**自动配置很值得做**：手工点出来的面板在 `docker compose down -v` 后就没了，而 JSON 文件在 git 里，任何人 clone 下来 `docker compose up` 就有完整面板。

面板 JSON 的生成方式：在 Grafana UI 里搭好，然后用面板设置里的 "JSON Model" 导出，保存成文件。不要手写 JSON。

### 4.5 面板内容

一个 dashboard，分四行：

**Overview 行**：
- Run 总量（按 status 堆叠的时间序列）：`sum by (status) (rate(agenthub_runs_total[5m]))`
- Run 成功率（Stat 面板）：`sum(rate(agenthub_runs_total{status="succeeded"}[1h])) / sum(rate(agenthub_runs_total[1h]))`
- 当前执行中：`agenthub_runs_in_progress`
- 队列深度：`agenthub_queue_depth`

**Latency 行**：
- Run 耗时 p50 / p95 / p99：`histogram_quantile(0.95, sum by (le, trigger) (rate(agenthub_run_duration_seconds_bucket[5m])))`
- 模型调用耗时 p95（按 model 分组）
- 工具执行耗时 p95（按 tool_type 分组）
- HTTP 请求耗时 p95（按 path 分组，取 top 10）

**Cost 行**：
- Token 消耗速率（按 model 和 kind 分组）：`sum by (model, kind) (rate(agenthub_llm_tokens_total[5m]))`
- 累计成本：`sum by (model) (agenthub_llm_cost_usd_total)`
- 每小时成本：`sum(rate(agenthub_llm_cost_usd_total[1h])) * 3600`

**Errors 行**：
- Run 失败率
- 模型调用错误（按 error_type）
- 工具失败率（按 tool_type）
- HTTP 5xx 速率

**关于 histogram_quantile 的常见错误**：必须对 `_bucket` 后缀的指标用 `rate()` 再 `sum by (le, ...)`，`le` 标签必须保留。写成 `histogram_quantile(0.95, agenthub_run_duration_seconds_bucket)` 不会报错但结果是错的。

---

## 任务 5：埋点落地

把任务 3 定义的指标接到实际代码里。逐个位置：

| 指标 | 埋点位置 |
|------|---------|
| `runs_total` | `execute_run` / `execute_run_with_checkpoint` 结束时（含失败路径的 `_fail_run`） |
| `run_duration_seconds` | 同上，用 `run.duration_ms / 1000` |
| `runs_in_progress` | `execute_run` 开头 `.inc()`，`finally` 里 `.dec()` |
| `llm_tokens_total` | `call_model` 节点里从 `usage_metadata` 取值 |
| `llm_cost_usd_total` | Run 结束时用 `estimate_cost_usd` 的结果，`None` 时不记 |
| `llm_call_duration_seconds` | `call_model` 节点里计时 |
| `llm_errors_total` | `call_model` 的 `except` 里，`error_type` 用 `type(exc).__name__` |
| `tool_calls_total` / `tool_duration_seconds` | `to_langchain_tool` 的 `_run` 里，用 `ToolResult` 的 `ok` 和 `duration_ms` |
| `retrieval_*` | `retrieve_context` 节点里 |
| `approvals_total` | `decide_approval` 路由里 |
| `queue_depth` | `/metrics` 端点里实时查 |

`runs_in_progress` 的 `.dec()` **必须在 `finally` 里**。漏了会让这个 Gauge 只增不减，最后显示几百个"正在执行"而实际上一个都没有。

`llm_errors_total` 的 `error_type` 标签用异常类名，基数有界（异常类型数量有限）。不要用异常消息（无界基数）。

---

## 任务 6：管理员监控页

这是给平台管理员看的业务视角，与 Grafana 的技术视角互补。Grafana 回答"系统健康吗"，这个页面回答"谁在用、用得怎么样、哪里出问题了"。

### 6.1 后端聚合接口

新建 `backend/app/api/routes/admin_monitoring.py`，`tags=["monitoring"]`。**所有接口都要 `Depends(get_current_active_superuser)`。**

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/monitoring/overview` | 概览数字 |
| GET | `/monitoring/usage` | 按天的用量时间序列 |
| GET | `/monitoring/top-users` | 用量排名 |
| GET | `/monitoring/recent-failures` | 最近失败的 Run |
| GET | `/monitoring/system` | 系统组件状态 |

`/overview` 返回：

```python
class MonitoringOverview(SQLModel):
    total_users: int
    active_users_7d: int          # 7 天内有 Run 的用户数
    total_agents: int
    total_runs: int
    runs_24h: int
    success_rate_24h: float | None
    avg_duration_ms_24h: int | None
    total_cost_usd_30d: Decimal | None
    total_tokens_30d: int
    pending_approvals: int
    documents_processing: int
    queue_depth: int
```

实现上是一组聚合查询。**注意不要写成十几个独立查询串行执行**，页面加载会很慢。能合并的合并，例如 Run 相关的统计一次查完：

```python
    row = session.exec(
        select(
            func.count().label("total"),
            func.count(case((Run.created_at >= day_ago, 1))).label("runs_24h"),
            func.count(
                case(((Run.status == RunStatus.SUCCEEDED) & (Run.created_at >= day_ago), 1))
            ).label("succeeded_24h"),
            func.avg(case((Run.created_at >= day_ago, Run.duration_ms))).label("avg_ms"),
        ).select_from(Run)
    ).one()
```

`case` 从 `sqlalchemy` 引入。这种条件聚合比多次查询快得多。

`/usage` 返回按天分组的时间序列：

```python
    select(
        func.date_trunc("day", Run.created_at).label("day"),
        func.count().label("runs"),
        func.sum(Run.prompt_tokens + Run.completion_tokens).label("tokens"),
        func.sum(Run.cost_usd).label("cost"),
    ).group_by("day").order_by("day")
```

接受 `days: int = 30` 参数。

`/recent-failures` 返回最近 50 条失败 Run，带 agent 名、用户邮箱、错误摘要、时间。这是管理员最常用的排查入口。

`/system` 检查各组件连通性：

```python
class ComponentStatus(SQLModel):
    name: str
    healthy: bool
    detail: str | None = None
    latency_ms: int | None = None


class SystemStatus(SQLModel):
    components: list[ComponentStatus]
```

逐个 ping：Postgres（`SELECT 1`）、Redis（`PING`）、MinIO（`bucket_exists`）、LLM（列模型或一次极小的调用）、arq worker（检查 arq 的 health key）。

**每个检查都要有超时**，否则一个组件挂了整个接口就挂了：

```python
    try:
        async with asyncio.timeout(3):
            ...
    except TimeoutError:
        return ComponentStatus(name=name, healthy=False, detail="Timeout")
```

LLM 的检查不要真的调模型（花钱且慢），只检查配置是否完整。

### 6.2 前端监控页

新建 `frontend/src/routes/_layout/monitoring.tsx`。**只对 superuser 可见**，在 `AppSidebar.tsx` 里放进 `is_superuser` 分支。

页面还要防直接输 URL 访问，在路由的 `beforeLoad` 里检查：

```typescript
export const Route = createFileRoute("/_layout/monitoring")({
  component: Monitoring,
  beforeLoad: async () => {
    // isLoggedIn 已由 _layout 保证，这里只检查权限
    // 具体实现参照 admin.tsx 的做法，如果它没做就在这里补
  },
})
```

看一下 `admin.tsx` 是怎么处理的，保持一致。如果它也没做前端检查（依赖后端 403），那就跟着不做，后端 403 会被 `ErrorComponent` 兜住。

布局：

**第一行**：四个 Stat 卡片 —— 24h Run 数、成功率、30 天成本、活跃用户。成功率低于 90% 时数字标红。

**第二行**：用量图表。Run 数和 Token 数的双轴折线图。图表库需要选一个 —— 项目目前没有图表依赖，加 `recharts`（与 shadcn 生态兼容好）：

```bash
bun add --filter frontend recharts
```

如果不想加依赖，用 CSS 画简单的柱状图也行，但折线图用 recharts 省事得多。

**第三行**：两个表格并列 —— Top users（邮箱、Run 数、Token、成本）和 Recent failures（时间、agent、用户、错误摘要，点击跳 Run 详情）。

**第四行**：系统状态。每个组件一个小卡片，绿点/红点 + 延迟。`refetchInterval: 15000` 定时刷新。

**待办提示区**：pending approvals 和 processing documents 的数量，非零时显示可点击的提示条跳到对应页面。

---

## 任务 7：测试

### 7.1 `backend/tests/core/test_metrics.py`

- `/metrics` 端点返回 200 且 content-type 正确
- 响应体里包含自定义指标名（`agenthub_runs_total`）
- 跑一次 Run 后 `agenthub_runs_total` 的值增加（用 `prometheus_client` 的 registry 直接读取，不要解析文本）
- `runs_in_progress` 在 Run 结束后回到 0（**这条要有**，验证 `finally` 里的 `.dec()`）
- 失败的 Run 也会记 `runs_total{status="failed"}`

指标测试的注意点：Counter 是进程级全局状态，测试之间会累积。**读取前后的差值**而不是绝对值：

```python
    before = _counter_value(runs_total, status="succeeded", trigger="playground")
    # 跑一次 Run
    after = _counter_value(runs_total, status="succeeded", trigger="playground")
    assert after - before == 1
```

### 7.2 `backend/tests/core/test_telemetry.py`

- `OTEL_EXPORTER_OTLP_ENDPOINT` 为空时 `setup_telemetry` 不报错且不创建 provider
- 配置了端点时能创建 span（用 `InMemorySpanExporter` 断言 span 被创建，不需要真实 collector）
- span 的属性名都带 `agenthub.` 前缀
- span 里没有大字段（断言所有 attribute 值的长度小于某个阈值，比如 500 字符）

`InMemorySpanExporter` 来自 `opentelemetry.sdk.trace.export.in_memory_span_exporter`，测试 OTel 的标准做法。

### 7.3 `backend/tests/core/test_logging.py`

- `run_id_var` 设置后，日志记录里的 `run_id` 字段正确（用 `caplog` fixture）
- 未设置时是 `-`
- 并发的两个 task 各自的 run_id 不串（这条验证 contextvars 的隔离，是本任务的核心）

```python
async def test_context_isolation() -> None:
    seen: dict[str, str] = {}

    async def _task(name: str) -> None:
        run_id_var.set(name)
        await asyncio.sleep(0.01)       # 让出控制权，制造交错
        seen[name] = run_id_var.get() or ""

    await asyncio.gather(_task("a"), _task("b"))
    assert seen == {"a": "a", "b": "b"}
```

### 7.4 `backend/tests/api/routes/test_monitoring.py`

- 所有接口对普通用户返回 403
- superuser 能访问
- `/overview` 的数字与手工构造的数据一致（建 3 个 Run，2 成功 1 失败，断言 `success_rate_24h` 约等于 0.667）
- `/usage` 返回按天分组的数据，天数正确
- `/recent-failures` 只返回失败的 Run，按时间倒序
- `/system` 在某个组件不可用时返回 `healthy=false` 而不是 500（**这条要测**，把 `REDIS_URL` 指向无效地址）
- `/system` 的响应时间在组件挂掉时仍然在超时范围内（验证 `asyncio.timeout` 生效）

### 7.5 手动验证

```bash
docker compose up -d
# 跑几个 Run（成功的和失败的都要有），触发几次工具调用

# 1. 指标
curl -s http://localhost:8000/metrics | grep agenthub_runs_total
# 能看到按 status 和 trigger 分组的计数

# 2. Prometheus
open http://localhost:9090/targets
# agenthub-api 和 agenthub-worker 两个 target 都是 UP

# 3. Grafana
open http://localhost:3000
# 用 .env 里的密码登录，dashboard 已自动加载，四行面板都有数据

# 4. Trace
docker compose logs otel-collector | grep "agent.run"
# 能看到 span 输出，包含 agent.node.call_model 等子 span

# 5. 日志
docker compose logs worker | head -50
# 每行都带 [run=<uuid> user=<uuid>] 前缀，同时跑多个 Run 时能区分

# 6. 监控页
# 用 superuser 登录，打开 /monitoring，四行内容都有数据
# 用普通用户登录，侧栏看不到 Monitoring 入口

# 7. 组件挂掉
docker compose stop redis
# 刷新监控页，Redis 组件显示红色，页面其余部分正常
docker compose start redis
# 15 秒内自动恢复绿色
```

第 7 步验证了监控页本身的健壮性 —— 监控系统在被监控对象挂掉时必须还能用。

---

## 阶段验收清单

- [ ] `uv run pytest tests/ -v` 全绿
- [ ] 测试不依赖 OTel collector（`OTEL_EXPORTER_OTLP_ENDPOINT` 为空时全绿）
- [ ] `runs_in_progress` 在 Run 结束后归零的测试通过
- [ ] contextvars 并发隔离测试通过
- [ ] `/system` 在组件不可用时返回 200 + `healthy=false` 而不是 500
- [ ] `cd backend && bash scripts/lint.sh` 全绿
- [ ] `cd backend && bash scripts/test.sh` 全绿
- [ ] `bun run lint` 通过
- [ ] 手动完成任务 7.5 的七步验证
- [ ] Prometheus 的 `/targets` 页面里 api 和 worker 都是 UP
- [ ] Grafana 面板从 provisioning 自动加载（`docker compose down -v` 后重新 up 仍然有面板）
- [ ] 检查标签基数：`curl -s localhost:8000/metrics | wc -l` 的行数在合理范围（几百行，不是几万行）。如果异常多，说明有高基数标签
- [ ] `/metrics` 在 `compose.deploy.yml` 下**无法**从公网访问
- [ ] 普通用户访问 `/api/v1/monitoring/overview` 返回 403
- [ ] worker 日志里每行都有 run_id，并发执行时不串味
- [ ] OTel collector 日志里能看到 `agent.run` 和 `agent.node.*` 的 span 层级关系

---

## 常见坑

**多 worker 下指标数值偏低且跳动**
`PROMETHEUS_MULTIPROC_DIR` 没配。见任务 3.6。这个坑很隐蔽，单 worker 开发时完全正常。

**Prometheus 内存暴涨**
高基数标签。检查有没有把 uuid、用户提供的字符串、实际 URL 路径当标签。用 `/metrics` 的行数快速判断。

**`runs_in_progress` 只增不减**
`.dec()` 不在 `finally` 里，异常路径漏了。

**`histogram_quantile` 结果明显不对**
必须是 `histogram_quantile(0.95, sum by (le, ...) (rate(xxx_bucket[5m])))`。漏了 `rate` 或者 `le` 标签被聚合掉都会静默给出错误结果。

**Grafana 面板 down -v 后消失**
手工建的面板存在 Grafana 的数据库里。必须用 provisioning 把 JSON 放进 git。

**worker 的指标抓不到**
worker 没有 HTTP 端点。需要 `start_http_server(9000)`。

**OTel 在测试里刷一堆连接失败告警**
`setup_telemetry` 必须在端点未配置时直接返回。

**span 太大导致 collector 报错或丢数据**
span attribute 里放了 prompt 或模型回复。只放 id、模型名、token 数这类小字段。

**日志里 run_id 串味**
用了 thread-local 而不是 contextvars，或者 `run_id_var.set()` 之后没有 `reset`。

**监控页加载很慢**
十几个聚合查询串行执行。用条件聚合（`func.count(case(...))`）合并成少数几个查询。

**监控页在某个组件挂掉时整页 500**
`/system` 的每个检查都要独立 try/except + 超时。

**`/metrics` 出现在前端生成的客户端里**
路由缺 `include_in_schema=False`。

---

## 偏差记录

- 是否实现 API 到 worker 的 trace 关联：
- 是否接 Tempo（还是只用 debug exporter）：
- 是否加 postgres / redis exporter：
- `/metrics` 的访问控制方式：
- 日志格式（text / json）：
- 图表库选择：
- 其它偏差：
