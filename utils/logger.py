"""
统一的日志初始化工具模块。

本模块集中管理项目所有 logger 的初始化流程，避免各业务模块重复复制
"getLogger -> setLevel -> handlers = [] -> ConcurrentRotatingFileHandler -> addHandler"
这套样板代码（此前在 01_backendServer.py / utils/tasks.py / utils/redis.py /
utils/tools.py / utils/llms.py 中各出现一次）。

设计要点：
1. **进程内共享 handler**：同一进程内所有通过 setup_logger 创建的 logger 共用
   同一个文件 handler，避免多次打开同一日志文件带来的句柄浪费与轮转冲突。
2. **多进程安全**：使用 concurrent_log_handler.ConcurrentRotatingFileHandler，
   FastAPI（Uvicorn）与 Celery Worker 可同时写入同一日志文件。
3. **可配置**：日志级别、是否输出到控制台、格式字符串均通过 Config 或环境变量控制。
4. **结构化辅助**：提供 log_exception / truncate / mask_secrets 三个工具函数，
   帮助业务代码写出更具体、更健壮的日志（完整 traceback、上下文字段、长文本截断、敏感字段脱敏）。
5. **防御性兜底**：日志目录被人为删除时会自动重建；级别字符串拼写错误时回退到 INFO。

业务模块典型用法：

    from utils.logger import setup_logger, log_exception, truncate
    logger = setup_logger(__name__)

    try:
        ...
    except Exception as e:
        log_exception(logger, "处理请求失败", e,
                      extra_context={"user_id": user_id, "session_id": session_id})
        raise
"""

import logging
import os
import sys
import threading
from typing import Optional, Tuple

from concurrent_log_handler import ConcurrentRotatingFileHandler

from .config import Config


# ------------------------------------------------------------------
# 内部状态：进程内共享的 handler 与初始化锁
# ------------------------------------------------------------------

# 保护 _FILE_HANDLER / _CONSOLE_HANDLER 的初始化只执行一次（多线程环境下也安全）
_INIT_LOCK = threading.Lock()

# 进程内共享的文件 handler，所有通过 setup_logger 创建的 logger 都指向它
_FILE_HANDLER: Optional[ConcurrentRotatingFileHandler] = None

# 进程内共享的控制台 handler（仅当 Config.LOG_TO_CONSOLE 为 True 时创建）
_CONSOLE_HANDLER: Optional[logging.StreamHandler] = None


# ------------------------------------------------------------------
# 内部工具函数
# ------------------------------------------------------------------

def _resolve_level(level) -> int:
    """
    将字符串或整数日志级别安全转换为 logging 的整数级别。

    非法值回退到 INFO，避免因环境变量拼写错误（如 "info " 带空格、"VERBOSE" 等
    非标准级别名）导致整个日志系统崩溃。
    """
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        resolved = logging.getLevelName(level.strip().upper())
        if isinstance(resolved, int):
            return resolved
    # 兜底：任何无法识别的值都按 INFO 处理
    return logging.INFO


def _build_formatter() -> logging.Formatter:
    """
    构造统一的日志格式化器。

    格式包含：时间、进程 ID、日志级别、logger 名（模块名）、
    文件名:行号、函数名、消息内容。

    这样 FastAPI（Uvicorn）与 Celery Worker 双进程写入同一日志文件时，
    可以通过 PID 清晰区分来源，通过 文件:行号 快速定位到代码位置。
    """
    return logging.Formatter(
        fmt=Config.LOG_FORMAT,
        datefmt=Config.LOG_DATE_FORMAT,
    )


def _ensure_log_dir() -> None:
    """
    保证日志文件所在目录存在。

    Config 类体内已经尝试创建过一次，但若运行期间目录被人为删除
    （例如清理脚本误删），handler 创建会失败。这里做一次防御性兜底。
    创建失败时降级为 stderr 警告，不阻塞应用启动。
    """
    log_dir = os.path.dirname(Config.LOG_FILE)
    if log_dir and not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError as exc:
            print(
                f"[logging] 无法创建日志目录 {log_dir!r}: {exc}，"
                f"日志可能无法正常写入文件。",
                file=sys.stderr,
            )


def _ensure_handlers() -> Tuple[ConcurrentRotatingFileHandler, Optional[logging.StreamHandler]]:
    """
    懒加载并复用文件 handler 与控制台 handler。

    首次调用时创建并缓存，后续直接返回缓存实例，避免重复打开同一日志文件。
    """
    global _FILE_HANDLER, _CONSOLE_HANDLER

    with _INIT_LOCK:
        if _FILE_HANDLER is None:
            _ensure_log_dir()
            _FILE_HANDLER = ConcurrentRotatingFileHandler(
                filename=Config.LOG_FILE,
                maxBytes=Config.MAX_BYTES,
                backupCount=Config.BACKUP_COUNT,
                encoding="utf-8",
            )
            _FILE_HANDLER.setLevel(_resolve_level(Config.LOG_LEVEL))
            _FILE_HANDLER.setFormatter(_build_formatter())

        if Config.LOG_TO_CONSOLE and _CONSOLE_HANDLER is None:
            _CONSOLE_HANDLER = logging.StreamHandler(stream=sys.stdout)
            _CONSOLE_HANDLER.setLevel(_resolve_level(Config.LOG_LEVEL))
            _CONSOLE_HANDLER.setFormatter(_build_formatter())

    return _FILE_HANDLER, _CONSOLE_HANDLER


# ------------------------------------------------------------------
# 对外 API：logger 初始化
# ------------------------------------------------------------------

def setup_logger(name: str) -> logging.Logger:
    """
    获取（或创建）一个带统一 handler 的 logger。

    业务模块用法：
        from utils.logger import setup_logger
        logger = setup_logger(__name__)

    参数:
        name: logger 名称，建议传入 __name__，便于按模块过滤日志。

    返回:
        logging.Logger 实例，已配置文件 handler（以及可选的控制台 handler），
        并设置 propagate=False 以避免消息冒泡到 root logger 造成重复输出
        （uvicorn / celery 都会给 root logger 挂 handler）。

    幂等性:
        同一 name 多次调用不会重复添加 handler；模块被 reload 也安全。
    """
    logger = logging.getLogger(name)
    logger.setLevel(_resolve_level(Config.LOG_LEVEL))
    # 阻止消息向 root logger 冒泡，避免 uvicorn / celery 的 root handler 重复输出
    logger.propagate = False

    # 幂等地清空旧 handler：如果同一模块被多次 import 或 reload，不会重复添加
    for h in list(logger.handlers):
        logger.removeHandler(h)

    file_handler, console_handler = _ensure_handlers()
    logger.addHandler(file_handler)
    if console_handler is not None:
        logger.addHandler(console_handler)

    return logger


# ------------------------------------------------------------------
# 对外 API：结构化日志辅助函数
# ------------------------------------------------------------------

def log_exception(
    logger: logging.Logger,
    message: str,
    exc: Optional[BaseException] = None,
    *,
    level: int = logging.ERROR,
    extra_context: Optional[dict] = None,
) -> None:
    """
    记录带完整 traceback 的异常日志，并可选附加结构化上下文。

    相比 `logger.error(f"xxx: {e}")`，本函数的优势：
    1. 自动带 exc_info=True，输出完整调用栈，定位到具体代码行；
    2. 将 user_id / session_id / task_id 等上下文字段以 key=value 形式拼接，
       便于 grep 与后续日志平台解析；
    3. 对异常对象做 repr 兜底，避免 str(e) 为空时日志"什么都看不到"
       （例如 `raise ValueError()` 时 str(e) 为空字符串）。

    参数:
        logger: 目标 logger。
        message: 业务侧的错误描述，例如 "处理请求失败"。
        exc: 捕获到的异常对象；为 None 时自动取 sys.exc_info()（在 except 块内调用时）。
        level: 日志级别，默认 ERROR；对可恢复错误可传 logging.WARNING。
        extra_context: 需要附加到日志的上下文字段字典，例如
            {"user_id": "u1", "session_id": "s1", "task_id": "t1"}。
    """
    # 组装上下文后缀：key1=value1 key2=value2 ...
    ctx_suffix = ""
    if extra_context:
        pairs = [f"{k}={v!r}" for k, v in extra_context.items() if v is not None]
        if pairs:
            ctx_suffix = " | " + " ".join(pairs)

    # 异常对象兜底：优先用传入的 exc，其次用当前 except 上下文
    if exc is None:
        exc_info = sys.exc_info()
        current_exc = exc_info[1] if exc_info else None
        exc_repr = repr(current_exc) if current_exc else "<no active exception>"
    else:
        exc_info = exc  # logging 模块会自动处理 BaseException 实例
        exc_repr = repr(exc) or f"<{type(exc).__name__}>"

    logger.log(
        level,
        f"{message}{ctx_suffix} | exception={exc_repr}",
        exc_info=exc_info,
    )


def truncate(text, max_len: int = 500) -> str:
    """
    将长文本截断为不超过 max_len 的字符串，末尾追加截断标记与原始长度。

    适用于将用户 query、system_message、LLM 响应、Redis 中的完整 session dict
    等长字段写入日志时，避免单行日志过长导致：
    - 日志文件体积暴涨
    - 阅读困难
    - 潜在的敏感信息暴露面扩大

    参数:
        text: 待截断的内容，None 会被转换为 "<None>"。
        max_len: 最大保留字符数，默认 500。

    返回:
        截断后的字符串。若未超过 max_len 则原样返回 str(text)。

    示例:
        >>> truncate("hello", 10)
        'hello'
        >>> truncate("a" * 600, 500)
        'aaa...(truncated, total=600)'
    """
    if text is None:
        return "<None>"
    s = str(text)
    if len(s) <= max_len:
        return s
    return f"{s[:max_len]}...(truncated, total={len(s)})"


# 默认需要脱敏的字段名关键词（不区分大小写，子串匹配）
_SENSITIVE_KEY_PATTERNS = (
    "api_key", "apikey", "token", "password",
    "secret", "authorization", "credential",
)


def mask_secrets(data, key_patterns: Tuple[str, ...] = _SENSITIVE_KEY_PATTERNS) -> str:
    """
    将 dict 中的敏感字段替换为 '***'，返回 repr 字符串，用于安全地写入日志。

    典型场景：把 LLM 配置（含 api_key）、请求头（含 Authorization）、
    数据库连接串等写入日志前做脱敏，防止凭据通过日志文件泄露。

    仅处理顶层 key，嵌套结构不做递归（如需递归可按需扩展）。
    非 dict 输入直接返回 repr，不做脱敏。

    参数:
        data: 待脱敏的数据，通常是 dict。
        key_patterns: 触发脱敏的字段名关键词元组，默认覆盖常见凭据字段。

    返回:
        脱敏后的 repr 字符串。

    示例:
        >>> mask_secrets({"api_key": "sk-xxx", "model": "qwen-max"})
        "{'api_key': '***', 'model': 'qwen-max'}"
    """
    if isinstance(data, dict):
        masked = {}
        for k, v in data.items():
            key_lower = str(k).lower()
            if any(pattern in key_lower for pattern in key_patterns):
                masked[k] = "***"
            else:
                masked[k] = v
        return repr(masked)
    return repr(data)
