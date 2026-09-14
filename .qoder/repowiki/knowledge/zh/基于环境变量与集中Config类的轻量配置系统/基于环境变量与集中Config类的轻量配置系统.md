---
kind: configuration_system
name: 基于环境变量与集中Config类的轻量配置系统
category: configuration_system
scope:
    - '**'
source_files:
    - utils/config.py
    - .env
    - 01_backendServer.py
---

## 1. 使用的系统与方案

本项目采用最轻量的 Python 原生配置方案：
- 通过 `os.getenv` 从进程环境读取运行时配置（如数据库连接串、LLM API Key）。
- 使用一个集中式 `utils/config.py` 中的 `Config` 类作为所有模块共享的配置入口，将默认值、常量、服务地址等统一暴露为类属性。
- 没有引入 `pydantic-settings`、`python-dotenv`、`dynaconf`、`configparser` 或任何 YAML/TOML/JSON 配置文件解析器；也没有 `.env` 文件的自动加载逻辑。

## 2. 关键文件

- `utils/config.py`：唯一配置中心，定义 `Config` 类，集中声明日志、PostgreSQL、Redis、Celery、LLM、HTTP 服务等全部配置项。
- `.env`：存放密钥与环境变量（如 `DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL`、`DB_URI`），但项目代码中并未显式调用 `dotenv` 加载该文件；仅 `DB_URI` 通过 `os.getenv("DB_URI", ...)` 被读取。
- `01_backendServer.py`：FastAPI 后端主入口，启动时通过 `from utils.config import Config` 消费配置，用于初始化日志轮转、PostgreSQL 连接池、会话 TTL、Uvicorn 监听 host/port 等。

## 3. 架构与约定

### 3.1 分层方式：默认值 + 环境变量覆盖
`Config` 类对每个配置项提供硬编码默认值，并通过 `os.getenv("KEY", default)` 实现“环境变量优先”的覆盖机制。例如：
- `DB_URI = os.getenv("DB_URI", "postgresql://kevin:123456@localhost:5432/postgres?sslmode=disable")`
- 其他如 `REDIS_HOST`、`REDIS_PORT`、`CELERY_BROKER_URL`、`HOST`、`PORT`、`LLM_TYPE` 等则直接以类属性形式给出默认值，未做环境变量覆盖。

这意味着：**只有 `DB_URI` 真正支持通过环境变量覆盖**；其余配置项只能通过修改源码中的 `Config` 类来调整。

### 3.2 资源初始化集中在 FastAPI lifespan
后端在 `lifespan` 上下文中一次性消费 `Config` 完成所有外部依赖初始化：
- Redis 会话管理器：`get_session_manager()`（内部也依赖 `Config.REDIS_*`）。
- PostgreSQL 连接池：`AsyncConnectionPool(conninfo=Config.DB_URI, min_size=Config.MIN_SIZE, max_size=Config.MAX_SIZE)`。
- LangGraph Checkpointer / Store：基于上述连接池创建。
- 日志：使用 `ConcurrentRotatingFileHandler(Config.LOG_FILE, maxBytes=Config.MAX_BYTES, backupCount=Config.BACKUP_COUNT)`。
- Uvicorn 启动参数：`uvicorn.run(app, host=Config.HOST, port=Config.PORT)`。

### 3.3 配置项分类
| 类别 | 配置项 | 说明 |
|---|---|---|
| 日志 | `LOG_FILE`, `MAX_BYTES`, `BACKUP_COUNT` | 日志写入 `logfile/app.log`，类初始化时自动创建目录；`MAX_BYTES` 实际是一个元组 `(5*1024*1024,)`，存在尾逗号导致的类型偏差 |
| 数据库 | `DB_URI`, `MIN_SIZE`, `MAX_SIZE` | 通过 `psycopg_pool.AsyncConnectionPool` 使用 |
| 缓存/会话 | `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `SESSION_TIMEOUT`, `TTL` | 用于 Redis 会话状态与任务队列 |
| 任务队列 | `CELERY_BROKER_URL`, `TASK_TTL` | Celery broker 指向 Redis |
| LLM | `LLM_TYPE` | 注释列出 openai/qwen/oneapi/ollama 四种模式，当前默认 `qwen` |
| HTTP | `HOST`, `PORT` | 默认 `0.0.0.0:8001` |

## 4. 约定与约束

- **单一配置入口**：所有模块通过 `from utils.config import Config` 访问配置，禁止在各业务文件中散落硬编码字符串。
- **环境变量命名**：目前仅 `DB_URI` 被 `os.getenv` 消费；`.env` 中定义的 `DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL` 由上层调用方（如 `utils/llms.py`）自行通过 `os.getenv` 读取，不属于本 `Config` 类管理范围。
- **无配置校验**：`Config` 类不做类型转换、格式校验或必填字段检查；错误会在下游初始化阶段（如连接失败）暴露。
- **无多环境切换机制**：没有 development/staging/production 等多套配置文件或环境变量前缀约定；切换环境需手动修改 `.env` 或进程环境。
- **配置即常量**：除 `DB_URI` 外，其余配置项在运行时不可变，本质是“带默认值的常量表”，而非可热更新的配置对象。
- **日志目录自动创建**：`Config` 类在导入时检查并创建 `logfile/` 目录，确保日志落盘路径可用。

## 5. 评价

这是一个极简、无依赖的配置层：以 `Config` 类为中心、以 `os.getenv` 为唯一注入通道，配合 `.env` 文件承载敏感信息。其优点是零外部依赖、结构简单；缺点是覆盖能力有限（仅 `DB_URI` 走环境变量）、缺少校验与多环境管理，属于适合原型/单实例服务的轻量实践。