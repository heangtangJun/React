import asyncio
from fastapi import HTTPException
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


# 统一通过 utils.logger.setup_logger 初始化 logger，
# 避免此前在本模块中复制粘贴的 20 行样板代码（handler / formatter / level 等）。
# 日志级别、格式、轮转策略均由 Config 与 utils.logger 集中管理。
logger = setup_logger(__name__)


# 创建Celery实例
celery_app = Celery(
    # Celery应用名称
    main='01_backendServer',
    # 消息代理URL，使用Redis
    broker=Config.CELERY_BROKER_URL
)

# 设置Celery配置参数
celery_app.conf.update(
    # 任务序列化格式
    task_serializer='json',
    # 可接受的内容类型
    accept_content=['json'],
    # 结果序列化格式
    result_serializer='json',
    # 时区设置
    timezone='Asia/Shanghai',
    # 启用UTC时间
    enable_utc=True,
)


# 针对短期记忆修剪聊天历史消息 限制消息的token数量（create_agent 中间件写法，替代已废弃的 pre_model_hook）
@wrap_model_call
async def trimmed_messages_middleware(request, handler):
    """
    在每次调用模型前修剪聊天历史，限制送入模型的 token 数量。

    通过 request.override(messages=...) 仅影响本次模型调用的输入，
    不改动持久化的会话历史（等价于旧版 pre_model_hook 返回 llm_input_messages 的效果）。

    Args:
        request: ModelRequest，包含 messages/state/tools 等
        handler: 下游处理器，调用后执行真正的模型请求

    Returns:
        ModelResponse | AIMessage
    """
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
    """
    读取指定用户长期记忆中的内容

    Args:
        user_id: 用户的唯一标识

    Returns:
        Dict[str, Any]: 包含记忆内容和状态的响应
    """
    try:
        # 指定命名空间
        namespace = ("memories", user_id)

        # 搜索记忆内容
        memories = await store.asearch(namespace, query="")

        # 处理查询结果
        if memories is None:
            raise HTTPException(
                status_code=500,
                detail="查询返回无效结果，可能是存储系统错误。"
            )

        # 提取并拼接记忆内容
        long_term_info = " ".join(
            [d.value["data"] for d in memories if isinstance(d.value, dict) and "data" in d.value]
        ) if memories else ""

        # 记录查询成功的日志，带结构化上下文键与记忆条数
        logger.info(
            f"成功获取长期记忆 | user_id={user_id!r} | "
            f"memory_count={len(memories) if memories else 0} | "
            f"content_length={len(long_term_info)} chars"
        )

        # 返回结构化响应
        return {
            "success": True,
            "user_id": user_id,
            "long_term_info": long_term_info,
            "message": "长期记忆获取成功" if long_term_info else "未找到长期记忆内容"
        }

    except HTTPException:
        # HTTPException 是业务主动抛出的，不需要 traceback，直接向上抛出
        raise
    except Exception as e:
        # 未预期错误：使用 log_exception 记录完整 traceback + 结构化上下文，
        # 便于事后定位到 store.asearch 失败的具体堆栈（例如 PostgreSQL 连接中断）。
        log_exception(
            logger,
            "获取长期记忆时发生意外错误",
            e,
            extra_context={"user_id": user_id},
        )
        raise HTTPException(
            status_code=500,
            detail=f"获取长期记忆失败: {str(e)}"
        )

# 解析state消息列表进行格式化展示
async def parse_messages(messages: List[Any]) -> None:
    """
    解析消息列表，以 DEBUG 级别记录 HumanMessage、AIMessage 和 ToolMessage 的详细信息。

    实现说明：
    - 原实现使用 print()，在 Celery Worker 进程中这些调试信息根本进不了日志文件
      （print 只输出到 stdout，不会被 ConcurrentRotatingFileHandler 捕获）。
    - 现改为 logger.debug()，既保证生产环境（LOG_LEVEL=INFO）不会输出冗余信息，
      又能在调试环境（LOG_LEVEL=DEBUG）完整落盘到 logfile/app.log，
      便于事后回放智能体的消息流。
    - 对长文本字段统一使用 truncate() 截断，避免单行日志过大。

    Args:
        messages: 包含消息的列表，每个消息是一个对象
    """
    # 若当前日志级别高于 DEBUG，直接跳过整个解析逻辑，避免无谓的字符串拼接开销
    if not logger.isEnabledFor(logging.DEBUG):
        return

    logger.debug(f"=== 消息解析结果 | total_messages={len(messages)} ===")
    for idx, msg in enumerate(messages, 1):
        # 获取消息类型
        msg_type = msg.__class__.__name__
        # 提取消息内容
        content = getattr(msg, 'content', '')
        # 处理附加信息
        additional_kwargs = getattr(msg, 'additional_kwargs', {})
        # 打印消息 ID
        msg_id = getattr(msg, 'id', '未知')

        logger.debug(
            f"消息[{idx}/{len(messages)}] | type={msg_type} | id={msg_id} | "
            f"content={truncate(content, 300)!r}"
        )

        if additional_kwargs:
            # 对 tool_calls 做特殊处理，其他附加信息直接输出
            tool_calls_kw = additional_kwargs.get('tool_calls')
            if tool_calls_kw:
                for tc in tool_calls_kw:
                    logger.debug(
                        f"  附加 tool_call | id={tc.get('id')!r} | "
                        f"function={tc.get('function', {}).get('name')!r} | "
                        f"arguments={truncate(tc.get('function', {}).get('arguments'), 300)!r}"
                    )
            other_kwargs = {k: v for k, v in additional_kwargs.items() if k != 'tool_calls'}
            if other_kwargs:
                logger.debug(f"  附加信息(other)={truncate(other_kwargs, 300)!r}")

        # 处理 ToolMessage 特有字段
        if msg_type == 'ToolMessage':
            tool_name = getattr(msg, 'name', '')
            tool_call_id = getattr(msg, 'tool_call_id', '')
            logger.debug(
                f"  ToolMessage 详情 | tool_name={tool_name!r} | "
                f"tool_call_id={tool_call_id!r}"
            )

        # 处理 AIMessage 的工具调用和元数据
        if msg_type == 'AIMessage':
            tool_calls = getattr(msg, 'tool_calls', [])
            if tool_calls:
                for tc in tool_calls:
                    logger.debug(
                        f"  AIMessage tool_call | name={tc.get('name')!r} | "
                        f"id={tc.get('id')!r} | args={truncate(tc.get('args'), 300)!r}"
                    )
            # 提取元数据
            metadata = getattr(msg, 'response_metadata', {})
            if metadata:
                token_usage = metadata.get('token_usage', {})
                logger.debug(
                    f"  AIMessage metadata | model_name={metadata.get('model_name', '未知')!r} | "
                    f"finish_reason={metadata.get('finish_reason', '未知')!r} | "
                    f"token_usage={token_usage!r}"
                )

        logger.debug("-" * 50)

# 处理智能体返回结果 可能是中断，也可能是最终结果
async def process_agent_result(
        session_id: str,
        task_id: str,
        result: Dict[str, Any],
        user_id: Optional[str] = None,
        session_manager: Optional[RedisSessionManager] = None
) -> AgentResponse:
    """
    处理智能体执行结果，统一处理中断和结果

    Args:
        session_id: 会话ID
        task_id: 任务ID
        result: 智能体执行结果
        user_id: 用户ID，如果提供，将更新会话状态
        session_manager: Redis会话管理器实例，用于更新会话状态

    Returns:
        AgentResponse: 标准化的响应对象
    """
    response = None
    try:
        # 检查是否有中断
        if "__interrupt__" in result:
            interrupt_data = result["__interrupt__"][0].value
            # 确保中断数据有类型信息
            if "interrupt_type" not in interrupt_data:
                interrupt_data["interrupt_type"] = "unknown"
            # 返回中断信息
            response = AgentResponse(
                session_id=session_id,
                task_id=task_id,
                status="interrupted",
                interrupt_data=interrupt_data
            )
            # 日志中记录中断类型与工具名，便于追踪人工审批流
            action_request = interrupt_data.get("action_request", {}) if isinstance(interrupt_data, dict) else {}
            logger.info(
                f"触发工具调用中断 | user_id={user_id!r} | "
                f"session_id={session_id!r} | task_id={task_id!r} | "
                f"interrupt_type={interrupt_data.get('interrupt_type')!r} | "
                f"tool={action_request.get('action')!r}"
            )
        # 如果没有中断，返回最终结果
        else:
            response = AgentResponse(
                session_id=session_id,
                task_id=task_id,
                status="completed",
                result=result
            )
            # 最终结果可能包含很长的 messages，使用 truncate 避免单行日志过大
            logger.info(
                f"智能体返回最终结果 | user_id={user_id!r} | "
                f"session_id={session_id!r} | task_id={task_id!r} | "
                f"status=completed | "
                f"result_keys={list(result.keys()) if isinstance(result, dict) else type(result).__name__!r}"
            )

    except Exception as e:
        response = AgentResponse(
            session_id=session_id,
            task_id=task_id,
            status="error",
            message=f"处理智能体结果时出错: {str(e)}"
        )
        # 处理智能体结果失败：使用 log_exception 记录完整 traceback + 上下文，
        # 便于事后定位到 __interrupt__ 解析、AgentResponse 构造等环节的具体堆栈。
        log_exception(
            logger,
            "处理智能体结果时出错",
            e,
            extra_context={
                "user_id": user_id,
                "session_id": session_id,
                "task_id": task_id,
            },
        )
    # 检查指定用户ID的指定session_id是否存在
    exists = await session_manager.session_id_exists(user_id, session_id)
    # 若存在 则更新状态数据
    if exists:
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
    if data['result'] is not None:
        # 获取 messages 列表
        messages = data['result']['messages']

        # 找到最后一个 type 为 human 的消息的索引
        last_human_index = -1
        for i, message in enumerate(messages):
            if message['type'] == 'human':
                last_human_index = i

        # 如果没找到 human 消息，返回原始结构但 messages 为空
        if last_human_index == -1:
            return {
                'session_id': data['session_id'],
                'status': data['status'],
                'timestamp': data['timestamp'],
                'message': data['message'],
                'result': {'messages': []}
            }

        # 筛选最后一个 human 消息及其后续消息
        filtered_messages = messages[last_human_index:]

        # 保留原始结构，只替换 messages
        return {
            'session_id': data['session_id'],
            'status': data['status'],
            'timestamp': data['timestamp'],
            'message': data['message'],
            'result': {'messages': filtered_messages}
        }
    # 若有中断数据
    elif data['interrupt_data'] is not None:
        return {
            'session_id': data['session_id'],
            'status': data['status'],
            'timestamp': data['timestamp'],
            'message': data['message'],
            'result': {'interrupt_data': data['interrupt_data']}
        }
    else:
        return {
            'session_id': data['session_id'],
            'status': data['status'],
            'timestamp': data['timestamp'],
            'message': data['message'],
            'result': {'messages': []}
        }


# 定义Celery任务：异步运行智能体
@celery_app.task
def invoke_agent_task(user_id: str, session_id: str, task_id: str, query: str, system_prompt: str):
    """
    异步运行智能体，处理用户请求并返回结果

    Args:
        user_id: 用户唯一标识
        session_id: 会话唯一标识
        task_id: 任务唯一标识
        query: 用户的问题
        system_prompt: 系统提示词

    Returns:
        dict: 智能体处理结果，转换为字典格式
    """
    # 异步执行智能体调用逻辑
    async def run_invoke():
        try:
            # 初始化Redis会话管理器
            session_manager = get_session_manager()

            # 更新会话状态为运行中
            await session_manager.update_session(
                user_id=user_id,
                session_id=session_id,
                task_id=task_id,
                status="running",
                last_query=query,
                last_updated=time.time(),
                ttl=Config.TTL
            )
            # 记录 Celery 任务开始执行，带完整上下文键，便于跨进程追踪
            logger.info(
                f"Celery 任务 invoke_agent_task 开始执行 | "
                f"user_id={user_id!r} | session_id={session_id!r} | "
                f"task_id={task_id!r} | query={truncate(query, 200)!r} | "
                f"llm_type={Config.LLM_TYPE!r}"
            )

            # 创建数据库连接池
            async with AsyncConnectionPool(
                conninfo=Config.DB_URI,
                min_size=Config.MIN_SIZE,
                max_size=Config.MAX_SIZE,
                kwargs={"autocommit": True, "prepare_threshold": 0}
            ) as pool:
                # 初始化短期记忆检查点
                checkpointer = AsyncPostgresSaver(pool)
                # 初始化长期记忆存储
                store = AsyncPostgresStore(pool)
                # 获取语言模型
                llm_chat, _ = get_llm(Config.LLM_TYPE)
                # 获取工具列表
                tools = await get_tools()
                logger.info(
                    f"Agent 依赖初始化完成 | task_id={task_id!r} | "
                    f"tools_count={len(tools)} | "
                    f"tool_names={[getattr(t, 'name', type(t).__name__) for t in tools]!r}"
                )

                # 创建ReAct智能体（新版 create_agent + middleware）
                agent = create_agent(
                    model=llm_chat,
                    tools=tools,
                    middleware=[trimmed_messages_middleware],
                    checkpointer=checkpointer,
                    store=store
                )

                # 获取长期记忆
                system_message = system_prompt
                result = await read_long_term_info(user_id, store)
                # 检查返回结果是否成功
                if result.get("success", False):
                    long_term_info = result.get("long_term_info")
                    # 若获取到的内容不为空，拼接到系统提示词
                    if long_term_info:
                        system_message = f"{system_prompt}我的附加信息有:{long_term_info}"
                        # 拼接后的 system_message 可能很长，使用 truncate 避免单行日志过大
                        logger.info(
                            f"已拼接长期记忆到 system_message | task_id={task_id!r} | "
                            f"long_term_info_length={len(long_term_info)} chars | "
                            f"system_message_preview={truncate(system_message, 300)!r}"
                        )

                # 构造智能体输入消息体
                messages = [
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": query}
                ]

                # 调用智能体
                result = await agent.ainvoke(
                    {"messages": messages},
                    config={"configurable": {"thread_id": task_id}}
                )

                # 解析返回的消息（DEBUG 级别，生产环境默认不输出）
                await parse_messages(result['messages'])
                # 处理智能体结果
                response = await process_agent_result(session_id, task_id, result, user_id, session_manager)

                # 对response进行处理 筛选出最近一次的完整对话更新到该task中
                # 原写法 `logger.info(f"invoke_response:{response.model_dump()}")` 会直接输出
                # 完整 dict，可能包含用户 query 与很长的 messages。现改为输出结构化摘要。
                response_dict = response.model_dump()
                logger.info(
                    f"invoke_agent_task 响应摘要 | task_id={task_id!r} | "
                    f"status={response.status!r} | "
                    f"has_result={response_dict.get('result') is not None} | "
                    f"has_interrupt={response_dict.get('interrupt_data') is not None} | "
                    f"message={truncate(response_dict.get('message'), 200)!r}"
                )
                filtered_data = filter_last_human_conversation(response_dict)

                # 更新任务状态为完成并绑定用户和会话
                await session_manager.set_task_status(
                    task_id=task_id,
                    status="completed",
                    # result=response.model_dump(),
                    result=filtered_data,
                    user_id=user_id,
                    session_id=session_id
                )
                logger.info(
                    f"Celery 任务 invoke_agent_task 执行完成 | "
                    f"user_id={user_id!r} | session_id={session_id!r} | "
                    f"task_id={task_id!r} | final_status=completed"
                )

                return response_dict

        except Exception as e:
            # 构造错误响应
            error_response = AgentResponse(
                session_id=session_id,
                task_id=task_id,
                status="error",
                message=f"处理请求时出错: {str(e)}"
            )
            # 使用 log_exception 记录完整 traceback + 结构化上下文，
            # 便于事后定位到 agent.ainvoke、PostgreSQL、LLM 调用等环节的具体堆栈。
            log_exception(
                logger,
                "Celery 任务 invoke_agent_task 执行失败",
                e,
                extra_context={
                    "user_id": user_id,
                    "session_id": session_id,
                    "task_id": task_id,
                    "query": truncate(query, 200),
                },
            )
            # 更新会话状态为错误
            await session_manager.update_session(
                user_id=user_id,
                session_id=session_id,
                task_id=task_id,
                status="error",
                last_response=error_response,
                last_updated=time.time(),
                ttl=Config.TTL
            )
            # 更新任务状态为失败并绑定用户和会话
            await session_manager.set_task_status(
                task_id=task_id,
                status="failed",
                error=str(e),
                user_id=user_id,
                session_id=session_id
            )
            raise e
        finally:
            # 关闭Redis连接
            await session_manager.close()

    return asyncio.run(run_invoke())


# 定义Celery任务：异步运行智能体
@celery_app.task
def resume_agent_task(user_id: str, session_id: str, task_id: str, command_data: str):
    """
    异步运行智能体，处理用户请求并返回结果

    Args:
        user_id: 用户唯一标识
        session_id: 会话唯一标识
        task_id: 任务唯一标识
        query: 用户的问题
        system_prompt: 系统提示词

    Returns:
        dict: 智能体处理结果，转换为字典格式
    """
    # 异步执行智能体调用逻辑
    async def resume_invoke():
        try:
            # 初始化Redis会话管理器
            session_manager = get_session_manager()

            # 记录 Celery 任务开始执行，带完整上下文键与响应类型，便于跨进程追踪
            logger.info(
                f"Celery 任务 resume_agent_task 开始执行 | "
                f"user_id={user_id!r} | session_id={session_id!r} | "
                f"task_id={task_id!r} | "
                f"command_type={command_data.get('type') if isinstance(command_data, dict) else type(command_data).__name__!r} | "
                f"llm_type={Config.LLM_TYPE!r}"
            )

            # 创建数据库连接池
            async with AsyncConnectionPool(
                conninfo=Config.DB_URI,
                min_size=Config.MIN_SIZE,
                max_size=Config.MAX_SIZE,
                kwargs={"autocommit": True, "prepare_threshold": 0}
            ) as pool:
                # 初始化短期记忆检查点
                checkpointer = AsyncPostgresSaver(pool)
                # 初始化长期记忆存储
                store = AsyncPostgresStore(pool)
                # 获取语言模型
                llm_chat, _ = get_llm(Config.LLM_TYPE)
                # 获取工具列表
                tools = await get_tools()
                logger.info(
                    f"Agent 依赖初始化完成（resume） | task_id={task_id!r} | "
                    f"tools_count={len(tools)} | "
                    f"tool_names={[getattr(t, 'name', type(t).__name__) for t in tools]!r}"
                )

                # 创建ReAct智能体（新版 create_agent + middleware）
                agent = create_agent(
                    model=llm_chat,
                    tools=tools,
                    middleware=[trimmed_messages_middleware],
                    checkpointer=checkpointer,
                    store=store
                )

                # 调用智能体
                result = await agent.ainvoke(
                    Command(resume=command_data),
                    config={"configurable": {"thread_id": task_id}}
                )

                # 解析返回的消息（DEBUG 级别，生产环境默认不输出）
                await parse_messages(result['messages'])
                # 处理智能体结果
                response = await process_agent_result(session_id, task_id, result, user_id, session_manager)

                # 对response进行处理 筛选出最近一次的完整对话更新到该task中
                # 原写法 `logger.info(f"resume_response:{response.model_dump()}")` 会直接输出
                # 完整 dict，可能包含很长的 messages。现改为输出结构化摘要。
                response_dict = response.model_dump()
                logger.info(
                    f"resume_agent_task 响应摘要 | task_id={task_id!r} | "
                    f"status={response.status!r} | "
                    f"has_result={response_dict.get('result') is not None} | "
                    f"has_interrupt={response_dict.get('interrupt_data') is not None} | "
                    f"message={truncate(response_dict.get('message'), 200)!r}"
                )
                filtered_data = filter_last_human_conversation(response_dict)

                # 更新任务状态为完成并绑定用户和会话
                await session_manager.set_task_status(
                    task_id=task_id,
                    status="completed",
                    # result=response.model_dump(),
                    result=filtered_data,
                    user_id=user_id,
                    session_id=session_id
                )
                logger.info(
                    f"Celery 任务 resume_agent_task 执行完成 | "
                    f"user_id={user_id!r} | session_id={session_id!r} | "
                    f"task_id={task_id!r} | final_status=completed"
                )

                return response_dict

        except Exception as e:
            # 构造错误响应
            error_response = AgentResponse(
                session_id=session_id,
                task_id=task_id,
                status="error",
                message=f"处理请求时出错: {str(e)}"
            )
            # 使用 log_exception 记录完整 traceback + 结构化上下文，
            # 便于事后定位到 Command(resume=...) 执行、checkpointer 加载等环节的具体堆栈。
            log_exception(
                logger,
                "Celery 任务 resume_agent_task 执行失败",
                e,
                extra_context={
                    "user_id": user_id,
                    "session_id": session_id,
                    "task_id": task_id,
                    "command_data": truncate(command_data, 300),
                },
            )
            # 更新会话状态为错误
            await session_manager.update_session(
                user_id=user_id,
                session_id=session_id,
                task_id=task_id,
                status="error",
                last_response=error_response,
                last_updated=time.time(),
                ttl=Config.TTL
            )
            # 更新任务状态为失败并绑定用户和会话
            await session_manager.set_task_status(
                task_id=task_id,
                status="failed",
                error=str(e),
                user_id=user_id,
                session_id=session_id
            )
            raise e
        finally:
            # 关闭Redis连接
            await session_manager.close()

    return asyncio.run(resume_invoke())




