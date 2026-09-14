import os
from typing import Callable
from langchain_core.tools import BaseTool, tool as create_tool
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt.interrupt import HumanInterruptConfig, HumanInterrupt
from langgraph.types import interrupt, Command
from langchain_core.tools import tool
from .config import Config
from .logger import setup_logger, log_exception, truncate
from langchain_mcp_adapters.client import MultiServerMCPClient


# 统一通过 utils.logger.setup_logger 初始化 logger，
# 避免此前在本模块中复制粘贴的 20 行样板代码（handler / formatter / level 等）。
# 日志级别、格式、轮转策略均由 Config 与 utils.logger 集中管理。
logger = setup_logger(__name__)


# 为工具添加人工审查（human-in-the-loop）功能
async def add_human_in_the_loop(
        tool: Callable | BaseTool,
        *,
        interrupt_config: HumanInterruptConfig = None,
) -> BaseTool:
    """
    为工具添加人工审查（human-in-the-loop）

    Args:
        tool: 可调用对象或 BaseTool 对象
        interrupt_config: 可选的人工中断配置

    Returns:
        BaseTool: 一个带有人工审查功能的 BaseTool 对象
    """
    # 检查传入的工具是否为 BaseTool 的实例
    if not isinstance(tool, BaseTool):
        # 如果不是 BaseTool，则将可调用对象转换为 BaseTool 对象
        tool = create_tool(tool)

    # 使用 create_tool 装饰器定义一个新的工具函数，继承原工具的名称、描述和参数模式
    @create_tool(
        tool.name,
        description=tool.description,
        args_schema=tool.args_schema
    )
    # 定义内部函数，用于处理带有中断逻辑的工具调用
    async def call_tool_with_interrupt(config: RunnableConfig, **tool_input):
        # 创建一个人为中断请求，包含工具名称、输入参数和配置
        request: HumanInterrupt = {
            "action_request": {
                "action": tool.name,
                "args": tool_input
            },
            "config": interrupt_config,
            "description": f"准备调用 {tool.name} 工具：\n- 参数为: {tool_input}\n\n是否允许继续？\n输入 'yes' 接受工具调用\n输入 'no' 拒绝工具调用\n输入 'edit' 修改工具参数后调用工具\n输入 'response' 不调用工具直接反馈信息",
        }
        # 调用 interrupt 函数，获取人工审查的响应（取第一个响应）
        response = interrupt(request)
        # 日志中输出人工审查响应概要，带工具名与响应类型，便于追踪审批流
        logger.info(
            f"收到人工审查响应 | tool={tool.name!r} | "
            f"response_type={response.get('type')!r} | "
            f"has_args={'args' in response}"
        )

        # 检查响应类型是否为“接受”（accept）
        if response["type"] == "accept":
            logger.info(
                f"工具调用已批准，开始执行 | tool={tool.name!r} | "
                f"args={truncate(tool_input, 300)!r}"
            )
            try:
                # 如果接受，直接调用原始工具并传入输入参数
                tool_response = await tool.ainvoke(input=tool_input)
                # 工具返回结果可能很长，使用 truncate 避免单行日志过大
                logger.info(
                    f"工具执行成功 | tool={tool.name!r} | "
                    f"response={truncate(tool_response, 500)!r}"
                )
            except Exception as e:
                # 工具调用失败：使用 log_exception 记录完整 traceback + 上下文，
                # 便于事后定位到具体是哪一个工具、哪一组参数触发了异常。
                log_exception(
                    logger,
                    "工具调用失败",
                    e,
                    extra_context={
                        "tool": tool.name,
                        "args": truncate(tool_input, 300),
                    },
                )

        # 检查响应类型是否为“编辑”（edit）
        elif response["type"] == "edit":
            # 如果是编辑，更新工具输入参数为响应中提供的参数
            tool_input = response["args"]["args"]
            logger.info(
                f"用户编辑了工具参数，使用新参数执行 | tool={tool.name!r} | "
                f"new_args={truncate(tool_input, 300)!r}"
            )
            try:
                # 使用更新后的参数调用原始工具
                tool_response = await tool.ainvoke(input=tool_input)
                logger.info(
                    f"工具执行成功（edit 分支） | tool={tool.name!r} | "
                    f"response={truncate(tool_response, 500)!r}"
                )
            except Exception as e:
                log_exception(
                    logger,
                    "工具调用失败（edit 分支）",
                    e,
                    extra_context={
                        "tool": tool.name,
                        "args": truncate(tool_input, 300),
                    },
                )

        # 检查响应类型是否为“拒绝”（reject）
        elif response["type"] == "reject":
            logger.info(
                f"工具调用被拒绝 | tool={tool.name!r} | "
                f"等待用户输入替代方案"
            )
            # 直接将用户反馈作为工具的响应
            tool_response = '该工具被拒绝使用，请尝试其他方法或拒绝回答问题。'

        # 检查响应类型是否为“响应”（response）
        elif response["type"] == "response":
            # 如果是响应，直接将用户反馈作为工具的响应
            user_feedback = response["args"]
            logger.info(
                f"用户选择直接反馈不调用工具 | tool={tool.name!r} | "
                f"feedback={truncate(user_feedback, 300)!r}"
            )
            tool_response = user_feedback

        else:
            # 未知响应类型：记录 ERROR 并带完整 response 上下文，便于排查
            logger.error(
                f"不支持的人工审查响应类型 | tool={tool.name!r} | "
                f"response_type={response.get('type')!r} | "
                f"supported_types=['accept','edit','reject','response']"
            )
            raise ValueError(f"Unsupported interrupt response type: {response['type']}")

        return tool_response

    return call_tool_with_interrupt


# 获取工具列表 提供给第三方调用
async def get_tools():
    # 自定义工具 模拟酒店预定工具
    @tool("book_hotel", description="酒店预定工具")
    async def book_hotel(hotel_name: str):
        """
       支持酒店预定的工具

        Args:
            hotel_name: 酒店名称

        Returns:
            工具的调用结果
        """
        return f"成功预定了在{hotel_name}的住宿。"

    # 自定义工具 计算两个数的乘积的工具
    @tool("multiply", description="计算两个数的乘积的工具")
    async def multiply(a: float, b: float) -> float:
        """
       支持计算两个数的乘积的工具

        Args:
            a: 参数1
            b: 参数2

        Returns:
            工具的调用结果
        """
        result = a * b
        return f"{a}乘以{b}等于{result}。"

    # MCP Server工具 高德地图
    client = MultiServerMCPClient({
        # 高德地图MCP Server
        # "amap-amap-sse": {
        #     "url": "https://mcp.amap.com/sse?key=" + os.getenv("AMAP_MAPS_API_KEY"),
        #     "transport": "sse",
        # },
        "amap-maps-streamableHTTP": {
            "url": "https://mcp.amap.com/mcp?key=" + os.getenv("AMAP_MAPS_API_KEY"),
            "transport": "streamable_http"
        }
    })
    # 从MCP Server中获取可提供使用的全部工具
    amap_tools = await client.get_tools()
    # 为工具添加人工审查
    tools = [await add_human_in_the_loop(index) for index in amap_tools]

    # 追加自定义工具并添加人工审查
    tools.append(await add_human_in_the_loop(book_hotel))
    tools.append(multiply)

    # 返回工具列表
    return tools