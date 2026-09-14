import os
import logging
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from .config import Config
from .logger import setup_logger, log_exception, mask_secrets


# 统一通过 utils.logger.setup_logger 初始化 logger，
# 避免此前在本模块中复制粘贴的 20 行样板代码（handler / formatter / level 等）。
# 日志级别、格式、轮转策略均由 Config 与 utils.logger 集中管理。
logger = setup_logger(__name__)


# 模型配置字典
MODEL_CONFIGS = {
    "openai": {
        "base_url": os.getenv("OPENAI_BASE_URL"),
        "api_key": os.getenv("OPENAI_API_KEY"),
        "chat_model": "gpt-4o-mini",
        "embedding_model": "text-embedding-3-small"
    },
    "oneapi": {
        "base_url": "http://139.224.72.218:3000/v1",
        "api_key": "sk-GseYmJ8pX1D0I200W7a5062e8f12122342323C4B724FfD66aD9",
        "chat_model": "qwen-max",
        "embedding_model": "text-embedding-v1"
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": os.getenv("DASHSCOPE_API_KEY"),
        "chat_model": "qwen-max",
        "embedding_model": "text-embedding-v1"
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
        "chat_model": "llama3.1:8b",
        "embedding_model": "nomic-embed-text:latest"
    }
}


# 默认配置
DEFAULT_LLM_TYPE = "qwen"
DEFAULT_TEMPERATURE = 0


class LLMInitializationError(Exception):
    """自定义异常类用于LLM初始化错误"""
    pass


def initialize_llm(llm_type: str = DEFAULT_LLM_TYPE) -> tuple[ChatOpenAI, OpenAIEmbeddings]:
    """
    初始化LLM实例

    Args:
        llm_type (str): LLM类型，可选值为 'openai', 'oneapi', 'qwen', 'ollama'

    Returns:
        ChatOpenAI: 初始化后的LLM实例

    Raises:
        LLMInitializationError: 当LLM初始化失败时抛出
    """
    try:
        # 检查llm_type是否有效
        if llm_type not in MODEL_CONFIGS:
            raise ValueError(f"不支持的LLM类型: {llm_type}. 可用的类型: {list(MODEL_CONFIGS.keys())}")

        config = MODEL_CONFIGS[llm_type]

        # 特殊处理ollama类型
        if llm_type == "ollama":
            os.environ["OPENAI_API_KEY"] = "NA"

        # 创建LLM实例
        llm_chat = ChatOpenAI(
            base_url=config["base_url"],
            api_key=config["api_key"],
            model=config["chat_model"],
            temperature=DEFAULT_TEMPERATURE,
            timeout=30,  # 添加超时配置（秒）
            max_retries=2  # 添加重试次数
        )

        llm_embedding = OpenAIEmbeddings(
            base_url=config["base_url"],
            api_key=config["api_key"],
            model=config["embedding_model"],
            deployment=config["embedding_model"]
        )

        # 日志中不直接输出 api_key，使用 mask_secrets 脱敏后记录配置概要
        logger.info(
            f"成功初始化 LLM | llm_type={llm_type!r} | "
            f"chat_model={config['chat_model']!r} | "
            f"embedding_model={config['embedding_model']!r} | "
            f"base_url={config['base_url']!r} | "
            f"config={mask_secrets(config)}"
        )
        return llm_chat, llm_embedding

    except ValueError as ve:
        # ValueError 属于配置错误，不需要完整 traceback，仅记录异常字符串与上下文
        logger.error(
            f"LLM 配置错误 | llm_type={llm_type!r} | "
            f"available_types={list(MODEL_CONFIGS.keys())!r} | error={ve!s}"
        )
        raise LLMInitializationError(f"LLM配置错误: {str(ve)}")
    except Exception as e:
        # 未预期错误：使用 log_exception 记录完整 traceback + 结构化上下文，
        # 便于事后根据日志定位到 ChatOpenAI / OpenAIEmbeddings 构造时的具体堆栈。
        log_exception(
            logger,
            "初始化 LLM 失败",
            e,
            extra_context={"llm_type": llm_type},
        )
        raise LLMInitializationError(f"初始化LLM失败: {str(e)}")


def get_llm(llm_type: str = DEFAULT_LLM_TYPE) -> ChatOpenAI:
    """
    获取LLM实例的封装函数，提供默认值和错误处理

    Args:
        llm_type (str): LLM类型

    Returns:
        ChatOpenAI: LLM实例
    """
    try:
        return initialize_llm(llm_type)
    except LLMInitializationError as e:
        # 使用 log_exception 记录带 traceback 的警告，明确标注"即将降级到默认配置"
        log_exception(
            logger,
            "LLM 初始化失败，准备使用默认配置重试",
            e,
            level=logging.WARNING,
            extra_context={
                "requested_llm_type": llm_type,
                "fallback_llm_type": DEFAULT_LLM_TYPE,
            },
        )
        if llm_type != DEFAULT_LLM_TYPE:
            return initialize_llm(DEFAULT_LLM_TYPE)
        raise  # 如果默认配置也失败，则抛出异常



# 示例使用
if __name__ == "__main__":
    try:
        # 测试不同类型的LLM初始化
        llm_openai, llm_embedding = get_llm()

        # 测试无效类型
        llm_invalid = get_llm("invalid_type")
    except LLMInitializationError as e:
        # 程序终止前记录完整 traceback，便于开发者直接看到失败原因
        log_exception(logger, "程序终止", e)