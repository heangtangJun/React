---
kind: logging_system
name: 基于标准logging + ConcurrentRotatingFileHandler的模块级日志系统
category: logging_system
scope:
    - '**'
source_files:
    - 01_backendServer.py
    - utils/tasks.py
    - utils/redis.py
    - utils/tools.py
    - utils/config.py
---

## 1. 使用的系统与框架

本项目未引入第三方日志库（如 loguru、structlog、python-json-logger），而是直接使用 Python 标准库 `logging`，并通过第三方包 `concurrent_log_handler` 提供的 `ConcurrentRotatingFileHandler` 实现并发安全的文件轮转输出。所有业务模块（后端服务、Celery 任务、Redis 会话管理、工具层）均遵循同一模式。

## 2. 关键文件与位置

- 日志配置集中声明在以下模块的模块级作用域中：
  - `01_backendServer.py`（FastAPI 入口，HTTP 请求/响应链路）
  - `utils/tasks.py`（Celery Worker 异步任务执行链路）
  - `utils/redis.py`（Redis 会话状态持久化）
  - `utils/tools.py`（工具调用与人工审查流程）
  - `utils/llms.py`、`utils/models.py`（LLM 调用与数据模型）
- 日志输出路径与轮转策略由 `utils/config.py` 中的 `Config` 类统一管理：
  - `LOG_FILE = "logfile/app.log"`
  - `MAX_BYTES = 5*1024*1024`（5MB 轮转）
  - `BACKUP_COUNT = 3`（保留 3 个历史文件）
- 实际日志文件位于根目录 `logfile/app.log`。

## 3. 架构与约定

### 3.1 每个模块独立初始化 logger
每个使用日志的模块都采用相同的四步初始化模式：
1. `logger = logging.getLogger(__name__)` — 以模块名作为 logger 名称，便于按模块过滤。
2. `logger.setLevel(logging.DEBUG)` — 将 logger 级别设为 DEBUG。
3. `logger.handlers = []` — **清空默认处理器**，避免重复输出到 stdout。
4. 创建 `ConcurrentRotatingFileHandler(Config.LOG_FILE, maxBytes=..., backupCount=...)`，设置 formatter 为 `"%(asctime)s - %(name)s - %(levelname)s - %(message)s"`，并 `addHandler` 到 logger。

该模式在 `01_backendServer.py`、`utils/tasks.py`、`utils/redis.py`、`utils/tools.py` 中完全一致地复制粘贴实现，属于强约定的工程实践。

### 3.2 日志级别使用约定
- `logger.info(...)`：用于正常业务流程记录，包括接口调用入口、Redis/DB 初始化成功、会话创建/更新、任务状态变更、最终结果返回等。
- `logger.error(...)`：用于异常与错误分支，包括 HTTPException 抛出前、存储失败、转换失败、工具调用失败、处理智能体结果出错等。
- `logger.warning(...)`：用于可恢复的非致命问题，如用户会话不存在时提示新建会话。
- `logger.debug(...)`：仅用于初始化阶段（如 handler 级别设置为 DEBUG），业务代码中未见 debug 级别的日志输出。

### 3.3 结构化字段约定
日志本身是纯文本行，但通过 f-string 将关键上下文字段拼入 message，形成事实上的结构化语义：
- 请求追踪：`user_id`、`session_id`、`task_id` 几乎在所有 info/error 日志中出现，构成跨进程（FastAPI → Celery Worker）的关联键。
- 状态码标注：错误日志中常以 `status_code=404`、`status_code=400` 等形式显式写出 HTTP 状态码，便于外部解析。
- 对象序列化：对 Pydantic 模型（如 `AgentResponse`）统一使用 `response.model_dump()` 后再写入日志，保证输出为 JSON 兼容字典。

### 3.4 多进程/多线程安全
使用 `ConcurrentRotatingFileHandler` 而非标准 `RotatingFileHandler`，因为 FastAPI（Uvicorn）与 Celery Worker 可能同时向同一 `logfile/app.log` 写入；该 Handler 内部加锁保证并发安全。

### 3.5 日志与配置解耦
日志目标路径、大小、备份数全部从 `Config` 读取，不硬编码在 logger 初始化处，便于通过环境变量或修改配置类调整。

## 4. 约定与约束

- **每个模块必须自行完成 logger 初始化**：没有全局日志配置中心，新增模块需复制上述四步模式，否则不会输出到文件。
- **禁止使用 `print()` 作为生产日志**：项目中存在 `parse_messages` 函数使用 `print(...)` 调试消息结构，但该函数被标记为调试用途且不在核心路径外暴露；生产日志应一律走 `logger`。
- **日志格式固定**：统一使用 `%(asctime)s - %(name)s - %(levelname)s - %(message)s`，未启用 JSON 格式或自定义字段扩展。
- **无日志分级路由**：所有模块的 logger 级别均为 DEBUG，handler 级别也为 DEBUG，未根据模块或环境动态调整。
- **无控制台输出**：通过 `logger.handlers = []` 主动清空默认 StreamHandler，确保日志只落盘到 `logfile/app.log`。
- **无日志采集/聚合组件**：未集成 ELK、Fluentd、Prometheus 等外部日志系统，仅本地文件轮转。
- **Celery Worker 与 FastAPI 共享同一日志文件**：两者均写入 `logfile/app.log`，依赖文件名中的模块名区分来源。

## 5. 已知不足

- 缺少统一的日志初始化入口，新增模块容易遗漏 handler 配置。
- 未使用结构化日志格式（JSON），不利于后续接入日志分析平台。
- 未对敏感信息（如数据库连接串、API Key）做脱敏处理。
- `parse_messages` 仍使用 `print` 输出消息详情，属于调试残留。