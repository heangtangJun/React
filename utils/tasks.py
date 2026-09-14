import asyncio
import logging
from typing import Dict, Any, Optional, List
from celery import Celery
import time
from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore
from psycopg_pool import AsyncConnectionPool
from langchain_core.messages.utils import trim_messages
from .config import Config
from .llms import get_llm
from .tools import get_tools
from .models import AgentResponse
from .redis import RedisSessionManager, get_session_manager
from .logger import setup_logger, log_exception, truncate
from langgraph.types import interrupt, Command


logger = setup_logger(__name__)


# 创建Celery实例
celery_app = Celery(
    main='01_backendServer',
    broker=Config.CELERY_BROKER_URL
)

celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='Asia/Shanghai',
    enable_utc=True,
)


# 针对短期记忆修剪聊天历史消息 限制消息的token数量（create_agent 中间件写法，替代已废弃的 pre_model_hook）
@wrap_model_call
async def trimmed_messages_middleware(request, handler):
    """在每次调用模型前修剪聊天历史，仅影响本次模型调用输入，不改持久化会话历史。"""
    trimmed_messages = trim_messages(
        messages=request.messages,
        max_tokens=20,
        strategy="last",
        token_counter=len,
        start_on="human",
        allow_partial=False
    )
    return await handler(request.override(messages=trimmed_messages))

# 读取指定用户长期记忆中的内容
async def read_long_term_info(user_id: str, store):
    """读取指定用户长期记忆中的内容，返回拼接后的文本。"""
    namespace = ("memories", user_id)
    memories = await store.asearch(namespace, query="")
    return " ".join(
        d.value["data"] for d in memories
        if isinstance(d.value, dict) and "data" in d.value
    ) if memories else ""

# 解析state消息列表进行格式化展示（DEBUG级别，生产环境默认不输出）
def parse_messages(messages: List[Any]) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return
    logger.debug(f"=== 消息解析结果 | total_messages={len(messages)} ===")
    for idx, msg in enumerate(messages, 1):
        logger.debug(
            f"消息[{idx}/{len(messages)}] | type={msg.__class__.__name__} | "
            f"id={getattr(msg, 'id', '未知')} | content={truncate(getattr(msg, 'content', ''), 300)!r} | "
            f"tool_calls={truncate(getattr(msg, 'tool_calls', None), 300)!r}"
        )

# 处理智能体返回结果 可能是中断，也可能是最终结果
async def process_agent_result(
        session_id: str,
        task_id: str,
        result: Dict[str, Any],
        user_id: Optional[str] = None,
        session_manager: Optional[RedisSessionManager] = None
) -> AgentResponse:
    """处理智能体执行结果，统一处理中断和结果，并更新会话状态。"""
    try:
        if "__interrupt__" in result:
            interrupt_data = result["__interrupt__"][0].value
            interrupt_data.setdefault("interrupt_type", "unknown")
            response = AgentResponse(
                session_id=session_id,
                task_id=task_id,
                status="interrupted",
                interrupt_data=interrupt_data
            )
            action_request = interrupt_data.get("action_request", {}) if isinstance(interrupt_data, dict) else {}
            logger.info(
                f"触发工具调用中断 | user_id={user_id!r} | session_id={session_id!r} | "
                f"task_id={task_id!r} | interrupt_type={interrupt_data.get('interrupt_type')!r} | "
                f"tool={action_request.get('action')!r}"
            )
        else:
            response = AgentResponse(
                session_id=session_id,
                task_id=task_id,
                status="completed",
                result=result
            )
            logger.info(
                f"智能体返回最终结果 | user_id={user_id!r} | session_id={session_id!r} | "
                f"task_id={task_id!r} | status=completed"
            )
    except Exception as e:
        response = AgentResponse(
            session_id=session_id,
            task_id=task_id,
            status="error",
            message=f"处理智能体结果时出错: {str(e)}"
        )
        log_exception(
            logger, "处理智能体结果时出错", e,
            extra_context={"user_id": user_id, "session_id": session_id, "task_id": task_id},
        )

    if await session_manager.session_id_exists(user_id, session_id):
        await session_manager.update_session(
            user_id=user_id,
            session_id=session_id,
            task_id=task_id,
            status=response.status,
            last_query=None,
            last_response=response,
            last_updated=time.time(),
            ttl=Config.TTL
        )
    return response

# 筛选最近一次完整对话
def filter_last_human_conversation(data):
    result = {'session_id': data['session_id'], 'status': data['status'],
              'timestamp': data['timestamp'], 'message': data['message'], 'result': {'messages': []}}
    if data.get('interrupt_data') is not None:
        result['result'] = {'interrupt_data': data['interrupt_data']}
    elif data.get('result') is not None:
        messages = data['result']['messages']
        last_human_index = max((i for i, m in enumerate(messages) if m['type'] == 'human'), default=-1)
        if last_human_index != -1:
            result['result'] = {'messages': messages[last_human_index:]}
    return result


# 共享的智能体执行逻辑：建池、建Agent、调用、处理结果、更新任务状态
# build_input: 异步回调，接收 store，返回 agent.ainvoke 的输入（invoke拼记忆、resume返回Command）
async def _run_agent(user_id: str, session_id: str, task_id: str,
                     build_input, session_manager: RedisSessionManager) -> AgentResponse:
    async with AsyncConnectionPool(
        conninfo=Config.DB_URI,
        min_size=Config.MIN_SIZE,
        max_size=Config.MAX_SIZE,
        kwargs={"autocommit": True, "prepare_threshold": 0}
    ) as pool:
        checkpointer = AsyncPostgresSaver(pool)
        store = AsyncPostgresStore(pool)
        agent_input = await build_input(store)
        llm_chat, _ = get_llm(Config.LLM_TYPE)
        tools = await get_tools()
        agent = create_agent(
            model=llm_chat,
            tools=tools,
            middleware=[trimmed_messages_middleware],
            checkpointer=checkpointer,
            store=store
        )
        result = await agent.ainvoke(agent_input, config={"configurable": {"thread_id": task_id}})

        parse_messages(result['messages'])
        response = await process_agent_result(session_id, task_id, result, user_id, session_manager)
        filtered_data = filter_last_human_conversation(response.model_dump())
        await session_manager.set_task_status(
            task_id=task_id,
            status="completed",
            result=filtered_data,
            user_id=user_id,
            session_id=session_id
        )
        return response

# 定义Celery任务：异步运行智能体
@celery_app.task
def invoke_agent_task(user_id: str, session_id: str, task_id: str, query: str, system_prompt: str):
    """异步运行智能体，处理用户请求并返回结果"""
    async def run_invoke():
        session_manager = get_session_manager()
        try:
            await session_manager.update_session(
                user_id=user_id, session_id=session_id, task_id=task_id,
                status="running", last_query=query, last_updated=time.time(), ttl=Config.TTL
            )
            logger.info(
                f"Celery 任务 invoke_agent_task 开始执行 | user_id={user_id!r} | "
                f"session_id={session_id!r} | task_id={task_id!r} | query={truncate(query, 200)!r}"
            )

            # 拼接长期记忆到系统提示词后构造输入消息体
            async def build_invoke_input(store):
                long_term_info = await read_long_term_info(user_id, store)
                prompt = f"{system_prompt}我的附加信息有:{long_term_info}" if long_term_info else system_prompt
                return {"messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": query}
                ]}

            response = await _run_agent(user_id, session_id, task_id, build_invoke_input, session_manager)

            logger.info(
                f"Celery 任务 invoke_agent_task 执行完成 | user_id={user_id!r} | "
                f"session_id={session_id!r} | task_id={task_id!r} | final_status=completed"
            )
            return response.model_dump()

        except Exception as e:
            await _handle_task_error(session_manager, user_id, session_id, task_id, e, query=query)
            raise
        finally:
            await session_manager.close()

    return asyncio.run(run_invoke())


# 定义Celery任务：异步恢复被中断的智能体运行
@celery_app.task
def resume_agent_task(user_id: str, session_id: str, task_id: str, command_data: dict):
    """异步恢复被中断的智能体运行并返回结果"""
    async def resume_invoke():
        session_manager = get_session_manager()
        try:
            logger.info(
                f"Celery 任务 resume_agent_task 开始执行 | user_id={user_id!r} | "
                f"session_id={session_id!r} | task_id={task_id!r} | "
                f"command_type={command_data.get('type') if isinstance(command_data, dict) else type(command_data).__name__!r}"
            )
            async def build_resume_input(store):
                return Command(resume=command_data)

            response = await _run_agent(user_id, session_id, task_id, build_resume_input, session_manager)
            logger.info(
                f"Celery 任务 resume_agent_task 执行完成 | user_id={user_id!r} | "
                f"session_id={session_id!r} | task_id={task_id!r} | final_status=completed"
            )
            return response.model_dump()

        except Exception as e:
            await _handle_task_error(session_manager, user_id, session_id, task_id, e,
                                     command=truncate(command_data, 300))
            raise
        finally:
            await session_manager.close()

    return asyncio.run(resume_invoke())


# Celery任务统一的错误处理：记录日志并更新会话/任务状态为失败
async def _handle_task_error(session_manager: RedisSessionManager, user_id: str, session_id: str,
                             task_id: str, e: Exception, **context):
    log_exception(logger, f"Celery 任务执行失败 | task_id={task_id!r}", e,
                  extra_context={"user_id": user_id, "session_id": session_id, **context})
    error_response = AgentResponse(
        session_id=session_id, task_id=task_id, status="error", message=f"处理请求时出错: {str(e)}"
    )
    await session_manager.update_session(
        user_id=user_id, session_id=session_id, task_id=task_id,
        status="error", last_response=error_response, last_updated=time.time(), ttl=Config.TTL
    )
    await session_manager.set_task_status(
        task_id=task_id, status="failed", error=str(e), user_id=user_id, session_id=session_id
    )
