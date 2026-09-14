import os
from dotenv import load_dotenv

# 集中加载 .env（项目此前未自动加载环境变量文件），
# 必须在 class Config 之前，因为类体与后续模块会以 os.getenv 读取这些变量
load_dotenv()


class Config:
    """统一的配置类，集中管理所有常量"""

    # ---------------- 日志持久化存储 ----------------
    # 日志文件路径，支持通过环境变量 LOG_FILE 覆盖
    LOG_FILE = os.getenv("LOG_FILE", "logfile/app.log")
    # 确保日志目录存在（仅在类定义时执行一次，避免运行期重复 IO）
    _log_dir = os.path.dirname(LOG_FILE)
    if _log_dir and not os.path.exists(_log_dir):
        os.makedirs(_log_dir, exist_ok=True)

    # 修复：原写法 `MAX_BYTES = 5*1024*1024,` 因尾部多了一个逗号，
    # 被 Python 解析为 tuple `(5242880,)`，传入 ConcurrentRotatingFileHandler
    # 的 maxBytes 参数时会引发 TypeError 或未预期的轮转行为。
    # 现在统一用 int() 包装，并支持通过环境变量覆盖，便于生产调优。
    MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
    BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))

    # 日志级别：DEBUG / INFO / WARNING / ERROR，可通过环境变量 LOG_LEVEL 覆盖。
    # 默认 INFO：生产环境避免 DEBUG 日志膨胀；开发调试时可设为 DEBUG。
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # 是否同时输出到控制台（stdout）。默认关闭，保持与原实现一致的"仅落盘"行为；
    # 开发调试时可通过 LOG_TO_CONSOLE=true 开启，方便实时观察。
    LOG_TO_CONSOLE = os.getenv("LOG_TO_CONSOLE", "false").strip().lower() in ("1", "true", "yes", "on")

    # 统一日志格式：包含时间、进程 ID、日志级别、logger 名（模块名）、
    # 文件名:行号、函数名、消息内容。
    # 相比原格式 "%(asctime)s - %(name)s - %(levelname)s - %(message)s"，
    # 新增 PID 可区分 FastAPI（Uvicorn）与 Celery Worker 双进程；
    # 新增 文件名:行号 与 函数名 可直接定位到代码位置，大幅提升排错效率。
    LOG_FORMAT = (
        "%(asctime)s | PID=%(process)d | %(levelname)-8s | "
        "%(name)s | %(filename)s:%(lineno)d | %(funcName)s | %(message)s"
    )
    LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

    # PostgreSQL数据库配置参数
    DB_URI = os.getenv("DB_URI", "postgresql://kevin:123456@localhost:5432/postgres?sslmode=disable")
    MIN_SIZE = 5
    MAX_SIZE = 10

    # Redis数据库配置参数
    REDIS_HOST = "localhost"
    REDIS_PORT = 6379
    REDIS_DB = 0
    SESSION_TIMEOUT = 300
    TTL = 3600

    CELERY_BROKER_URL = "redis://localhost:6379/0"
    TASK_TTL = 3600

    # openai:调用gpt模型,qwen:调用阿里通义千问大模型,oneapi:调用oneapi方案支持的模型,ollama:调用本地开源大模型
    LLM_TYPE = "qwen"

    # API服务地址和端口
    HOST = "0.0.0.0"
    PORT = 8001
