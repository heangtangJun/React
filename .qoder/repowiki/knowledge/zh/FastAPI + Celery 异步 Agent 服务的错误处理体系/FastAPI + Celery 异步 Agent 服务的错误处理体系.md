---
kind: error_handling
name: FastAPI + Celery 异步 Agent 服务的错误处理体系
category: error_handling
scope:
    - '**'
source_files:
    - 01_backendServer.py
    - 02_frontendServer.py
    - utils/tasks.py
    - utils/redis.py
    - utils/models.py
---

## 1. 整体方案

该仓库是一个基于 FastAPI + Celery + Redis + PostgreSQL 的 ReAct Agent 多用户多会话服务，错误处理贯穿 HTTP 层、Celery 任务层、Redis/DB 访问层以及富文本前端 CLI。核心思路是：**HTTP 层用 `fastapi.HTTPException` 表达业务校验失败；Celery 任务内部捕获异常后统一写入 Redis 会话状态为 `error`/`failed`，再向上抛出让 Celery 记录失败日志；前端 CLI 对非 200 响应直接 `raise Exception` 并配合 rich 面板展示。**

没有独立的 `errors/` 包或自定义异常类层次，错误类型以“内置异常 + Pydantic 模型字段 + Redis 状态枚举”组合实现。

## 2. 关键文件与职责

- `01_backendServer.py`：FastAPI 应用入口，集中使用 `HTTPException` 表达参数/资源不存在等 HTTP 语义错误；`lifespan` 中初始化 Redis/Postgres 连接池失败时 `raise RuntimeError` 阻止启动。
- `utils/tasks.py`：Celery Worker 侧的核心逻辑，定义 `invoke_agent_task`、`resume_agent_task` 两个任务函数，以及 `process_agent_result`、`read_long_term_info` 等辅助函数。所有外部调用（LLM、工具、Postgres）被包裹在 `try/except Exception` 中，统一构造 `AgentResponse(status="error", message=...)` 并更新 Redis 状态为 `error`/`failed`，最后 `raise e` 让 Celery 记录堆栈。
- `utils/models.py`：通过 Pydantic `BaseModel` 定义请求/响应契约，包括 `AgentRequest`、`InterruptResponse`、`AgentResponse`、`SessionStatusResponse` 等。Pydantic 自动负责入参校验失败时的 422 错误。
- `utils/redis.py`：`RedisSessionManager` 封装会话/任务状态 CRUD，所有方法返回布尔值或 `None` 表示失败，不抛异常；上层根据返回值决定 `HTTPException(404)` 还是正常流程。
- `02_frontendServer.py`：rich 终端客户端，每个对外部 API 的调用函数在 `response.status_code != 200` 时 `raise Exception(f"... {response.status_code} - {response.text}")`，并在主循环 `try/except Exception` 中打印 rich 错误面板并继续交互。

## 3. 架构约定与设计决策

### 3.1 HTTP 层（FastAPI）
- **业务校验失败**：显式 `raise HTTPException(status_code=..., detail=...)`，例如：
  - 会话/任务不存在 → `404`（`/agent/resume`、`/agent/write/longterm`、`/agent/session/{user_id}/{session_id}`、`/agent/task/{user_id}/{session_id}/{task_id}`）。
  - 状态非法 → `400`（如恢复非中断状态的会话）。
- **未预期异常**：在 `write_long_term_info` 等函数内 `except Exception as e` 后仍转为 `HTTPException(500, detail=f"存储记忆失败: {str(e)}")`，避免裸异常泄漏。
- **生命周期异常**：`lifespan` 中初始化 Redis/Postgres 失败时 `raise RuntimeError("服务初始化失败: ...")`，由 Uvicorn/FastAPI 终止进程。
- **无全局异常处理器**：未发现 `@app.exception_handler` 或中间件统一捕获，错误由 FastAPI 默认 JSON 响应格式输出。

### 3.2 Celery 任务层
- 每个任务函数内部定义嵌套 `async def run_invoke()` / `resume_invoke()`，并用 `try/except Exception` 包裹整个执行体。
- 捕获到异常后：
  1. 构造 `AgentResponse(status="error", message=f"处理请求时出错: {str(e)}")`。
  2. 调用 `session_manager.update_session(..., status="error", last_response=error_response, ...)` 持久化错误上下文。
  3. 调用 `session_manager.set_task_status(..., status="failed", error=str(e), ...)` 标记任务失败。
  4. `finally` 中关闭 Redis 连接。
  5. 重新 `raise e`，交由 Celery 记录 worker 日志。
- `process_agent_result` 专门处理 LangGraph 的 `__interrupt__` 与 `completed` 两种成功分支，异常分支统一归入 `status="error"`。

### 3.3 数据层（Redis / Postgres）
- `RedisSessionManager` 的所有查询方法返回 `Optional[dict]` 或 `bool`，**不抛异常**；上层通过 `if not exists:` 判断并返回 `HTTPException(404)` 或空列表响应。
- 长期记忆读写（`read_long_term_info`、`write_long_term_info`）在底层异常时 `raise HTTPException(500, ...)`，将 DB 错误上抛给 HTTP 层。

### 3.4 前端 CLI 层
- 每个 `requests` 调用函数遵循同一模式：`if response.status_code == 200: return json()` 否则 `raise Exception(f"API调用失败: {response.status_code} - {response.text}")`。
- 主循环 `while True` 外层 `try/except Exception` 捕获所有异常，用 rich 面板打印 `[error]...[/error]` 和 `traceback.format_exc()`，然后重置会话状态继续运行。
- 对 `delete_agent_session` 做了特判：404 视为成功（`return {"status": "success", "message": "...不存在"}`），体现幂等删除的业务约定。

### 3.5 日志与可观测性
- 三个模块（backend、tasks、redis）均独立配置 `logging.getLogger(__name__)` + `ConcurrentRotatingFileHandler`，按 `Config.LOG_FILE`、`MAX_BYTES`、`BACKUP_COUNT` 轮转。
- 错误路径统一 `logger.error(f"status_code=404,...")` 或 `logger.error(f"... {str(e)}")`，便于从 `logfile/app.log` 回溯。

## 4. 观察到的约束与惯例

| 场景 | 约定 | 依据 |
|---|---|---|
| HTTP 业务校验失败 | 必须 `raise HTTPException(status_code=..., detail=...)` | 所有 404/400 路径一致 |
| 未预期运行时错误 | 先 `logger.error`，再 `raise HTTPException(500, ...)` | `write_long_term_info`、`read_long_term_info` |
| Celery 任务异常 | 写 Redis 状态为 `error`/`failed` 后 `raise e` | `invoke_agent_task`、`resume_agent_task` 的 except 块 |
| Redis 查询失败 | 返回 `None`/`False`，由调用方决定 404 或空结果 | `RedisSessionManager` 全部方法 |
| 前端网络错误 | `raise Exception(f"... {status_code} - {text}")` | 每个 `*_agent` / `get_*` 函数 |
| 会话状态枚举 | `idle` / `running` / `interrupted` / `completed` / `error` / `not_found` / `failed` | `models.py` 注释及各处赋值 |
| 启动期致命错误 | `raise RuntimeError("服务初始化失败: ...")` | `lifespan` except 块 |

## 5. 缺失与风险

- 没有统一的错误码常量或自定义异常类层次，新增错误需重复手写 `HTTPException` 字符串。
- 没有全局异常处理器（`@app.exception_handler(Exception)`），第三方库抛出的未捕获异常会以 FastAPI 默认 500 响应，不包含业务上下文。
- Celery Worker 侧异常最终 `raise e`，但 `process_agent_result` 内部异常仅写 Redis 状态而不抛，两者策略略有差异。
- 前端 CLI 对 404 做幂等处理（删除会话），但对其他状态码一律抛异常，缺少重试/降级策略。
