import sys
import asyncio
import time
from fastapi import FastAPI, HTTPException
import uuid
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore
import uvicorn
from contextlib import asynccontextmanager
from psycopg_pool import AsyncConnectionPool
from utils.config import Config
from utils.tasks import celery_app, invoke_agent_task, resume_agent_task
from utils.models import AgentRequest, InterruptResponse, SystemInfoResponse, LongMemRequest
from utils.models import SessionInfoResponse, TaskInfoResponse, ActiveSessionInfoResponse, SessionStatusResponse
from utils.redis import get_session_manager
from utils.logger import setup_logger, log_exception, truncate


logger = setup_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期：初始化 Redis 会话管理器、PG 连接池、checkpointer 与 store"""
    try:
        app.state.session_manager = get_session_manager()
        logger.info("FastAPI 生命周期 | Redis 会话管理器初始化成功")

        app.state.pool = AsyncConnectionPool(
            conninfo=Config.DB_URI,
            min_size=Config.MIN_SIZE,
            max_size=Config.MAX_SIZE,
            kwargs={"autocommit": True, "prepare_threshold": 0}
        )
        await app.state.pool.open()
        logger.info(
            f"FastAPI 生命周期 | 数据库连接池初始化成功 | "
            f"min_size={Config.MIN_SIZE} | max_size={Config.MAX_SIZE}"
        )
        app.state.checkpointer = AsyncPostgresSaver(app.state.pool)
        app.state.store = AsyncPostgresStore(app.state.pool)
        logger.info("FastAPI 生命周期 | Checkpointer 与 Store 初始化成功")
        yield
    except Exception as e:
        log_exception(logger, "FastAPI 生命周期初始化失败", e)
        raise RuntimeError(f"服务初始化失败: {str(e)}")
    finally:
        await app.state.session_manager.close()
        if getattr(app.state, "pool", None):
            await app.state.pool.close()
            logger.info("FastAPI 生命周期 | 数据库连接池已关闭")
        logger.info("FastAPI 生命周期 | 关闭服务并完成资源清理")


app = FastAPI(
    title="Agent智能体后端API接口服务(异步任务调度)",
    description="基于LangGraph提供AI Agent服务",
    lifespan=lifespan
)

# API接口:异步运行智能体并返回任务ID
@app.post("/agent/invoke", response_model=dict)
async def invoke_agent(request: AgentRequest):
    logger.info(
        f"POST /agent/invoke | user_id={request.user_id!r} | session_id={request.session_id!r} | "
        f"task_id={request.task_id!r} | query={truncate(request.query, 200)!r}"
    )
    # 检查会话是否存在，不存在则创建
    if not await app.state.session_manager.session_task_id_exists(request.user_id, request.session_id, request.task_id):
        await app.state.session_manager.create_session(
            user_id=request.user_id,
            session_id=request.session_id,
            task_id=request.task_id,
            status="idle",
            last_updated=time.time(),
            ttl=Config.TTL
        )

    # 使用delay()调用任务 将任务发送到消息队列以便由Worker进程异步执行
    task = invoke_agent_task.delay(
        user_id=request.user_id,
        session_id=request.session_id,
        task_id=request.task_id,
        query=request.query,
        system_prompt=request.system_message or "你会使用工具来帮助用户。如果工具使用被拒绝，请提示用户。"
    )
    # 设置任务状态为等待并绑定用户和会话
    await app.state.session_manager.set_task_status(
        task_id=request.task_id, status="pending",
        user_id=request.user_id, session_id=request.session_id
    )
    return {"user_id": request.user_id, "session_id": request.session_id, "task_id": request.task_id}

# API接口:提交异步恢复被中断的智能体运行返回任务ID
@app.post("/agent/resume", response_model=dict)
async def resume_agent(response: InterruptResponse):
    user_id, session_id, task_id = response.user_id, response.session_id, response.task_id
    logger.info(
        f"POST /agent/resume | user_id={user_id!r} | session_id={session_id!r} | "
        f"task_id={task_id!r} | response_type={response.response_type!r}"
    )
    if not await app.state.session_manager.session_task_id_exists(user_id, session_id, task_id):
        raise HTTPException(status_code=404, detail=f"用户会话任务 {user_id}:{session_id}:{task_id} 不存在")

    session = await app.state.session_manager.get_session_by_task(user_id, session_id, task_id)
    status = session.get("status")
    if status != "interrupted":
        raise HTTPException(status_code=400, detail=f"用户会话任务 {user_id}:{session_id}:{task_id} 当前状态为 {status}，无法恢复非中断状态的会话")

    await app.state.session_manager.update_session(
        user_id=user_id, session_id=session_id, task_id=task_id,
        status="running", last_query=None, last_response=None, last_updated=time.time(), ttl=Config.TTL
    )

    # 构造恢复指令，如提供参数则一并携带
    command_data = {"type": response.response_type}
    if response.args:
        command_data["args"] = response.args

    task = resume_agent_task.delay(
        user_id=user_id, session_id=session_id, task_id=task_id, command_data=command_data
    )
    await app.state.session_manager.set_task_status(
        task_id=task_id, status="pending", user_id=user_id, session_id=session_id
    )
    return {"user_id": user_id, "session_id": session_id, "task_id": task_id}

# API接口:获取当前系统内全部的会话状态信息
@app.get("/system/info", response_model=SystemInfoResponse)
async def get_system_info():
    return SystemInfoResponse(
        sessions_count=await app.state.session_manager.get_session_count(),
        active_users=await app.state.session_manager.get_all_users_session_ids()
    )

# API接口:获取指定用户ID的最近一次修改的会话ID（不存在时返回空，客户端会新建会话）
@app.get("/agent/active/sessionid/{user_id}", response_model=ActiveSessionInfoResponse)
async def get_agent_active_sessionid(user_id: str):
    if not await app.state.session_manager.user_id_exists(user_id):
        return ActiveSessionInfoResponse(active_session_id="")
    return ActiveSessionInfoResponse(
        active_session_id=await app.state.session_manager.get_user_active_session_id(user_id)
    )

# API接口:获取指定用户ID的所有session_id
@app.get("/agent/sessionids/{user_id}", response_model=SessionInfoResponse)
async def get_agent_sessionids(user_id: str):
    if not await app.state.session_manager.user_id_exists(user_id):
        return SessionInfoResponse(session_ids=[])
    return SessionInfoResponse(
        session_ids=await app.state.session_manager.get_all_session_ids(user_id)
    )

# API接口:获取指定用户ID和会话ID下所有任务的ID和状态值
@app.get("/agent/tasks/{user_id}/{session_id}", response_model=TaskInfoResponse)
async def get_agent_task_ids(user_id: str, session_id: str):
    if not await app.state.session_manager.session_id_exists(user_id, session_id):
        return TaskInfoResponse(task_ids=[])
    return TaskInfoResponse(
        task_ids=await app.state.session_manager.get_task_status(user_id, session_id)
    )

# API接口:获取指定用户当前会话和任务ID的对应的状态数据
@app.get("/agent/status/{user_id}/{session_id}/{task_id}", response_model=SessionStatusResponse)
async def get_agent_status(user_id: str, session_id: str, task_id: str):
    if not await app.state.session_manager.session_task_id_exists(user_id, session_id, task_id):
        return SessionStatusResponse(
            user_id=user_id, session_id=session_id, task_id=task_id, status="not_found",
            message=f"用户 {user_id}:{session_id}:{task_id} 的会话不存在"
        )
    session = await app.state.session_manager.get_session_by_task(user_id, session_id, task_id)
    return SessionStatusResponse(
        user_id=user_id, session_id=session_id, task_id=task_id,
        status=session.get("status"),
        last_query=session.get("last_query"),
        last_updated=session.get("last_updated"),
        last_response=session.get("last_response")
    )

# API接口:写入指定用户的长期记忆
@app.post("/agent/write/longterm")
async def write_long_term(request: LongMemRequest):
    logger.info(
        f"POST /agent/write/longterm | user_id={request.user_id!r} | "
        f"memory_info_length={len(request.memory_info)} chars"
    )
    if not await app.state.session_manager.user_id_exists(request.user_id):
        raise HTTPException(status_code=404, detail=f"用户会话 {request.user_id} 不存在")

    try:
        memory_id = str(uuid.uuid4())
        await app.state.store.aput(
            namespace=("memories", request.user_id),
            key=memory_id,
            value={"data": request.memory_info}
        )
        logger.info(
            f"成功写入长期记忆 | user_id={request.user_id!r} | memory_id={memory_id!r} | "
            f"memory_info_length={len(request.memory_info)} chars"
        )
        return {"status": "success", "memory_id": memory_id, "message": "记忆存储成功"}
    except Exception as e:
        log_exception(logger, "存储长期记忆时发生意外错误", e,
                      extra_context={"user_id": request.user_id,
                                     "memory_info_preview": truncate(request.memory_info, 200)})
        raise HTTPException(status_code=500, detail=f"存储记忆失败: {str(e)}")

# API接口:删除指定用户指定会话
@app.delete("/agent/session/{user_id}/{session_id}")
async def delete_agent_session(user_id: str, session_id: str):
    if not await app.state.session_manager.session_id_exists(user_id, session_id):
        raise HTTPException(status_code=404, detail=f"用户 {user_id}:{session_id} 的会话不存在")
    await app.state.session_manager.delete_session(user_id, session_id)
    return {"status": "success", "message": f"用户 {user_id}:{session_id} 的会话已删除"}

# API接口:删除指定用户指定会话的指定任务
@app.delete("/agent/task/{user_id}/{session_id}/{task_id}")
async def delete_agent_task(user_id: str, session_id: str, task_id: str):
    if not await app.state.session_manager.session_task_id_exists(user_id, session_id, task_id):
        raise HTTPException(status_code=404, detail=f"用户 {user_id}:{session_id}:{task_id} 的任务不存在")
    await app.state.session_manager.delete_session(user_id, session_id, task_id)
    return {"status": "success", "message": f"用户 {user_id}:{session_id}:{task_id} 的任务已删除"}


# 启动服务器
if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    uvicorn.run(app, host=Config.HOST, port=Config.PORT)
