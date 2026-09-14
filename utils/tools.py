import os
from typing import Callable
from langchain_core.tools import BaseTool, tool as create_tool
from langgraph.types import interrupt
from langchain_core.tools import tool
from .logger import setup_logger, log_exception, truncate
from langchain_mcp_adapters.client import MultiServerMCPClient


logger = setup_logger(__name__)


# 为工具添加人工审查（human-in-the-loop）功能
async def add_human_in_the_loop(tool: Callable | BaseTool) -> BaseTool:
    """
    为工具添加人工审查（human-in-the-loop）

    中断负载为纯 dict（langgraph 1.x 已弃用 langgraph.prebuilt.interrupt 下的
    HumanInterrupt/HumanInterruptConfig TypedDict，interrupt() 本身接受任意可序列化值），
    前端契约保持不变：action_request.action / action_request.args。

    Args:
        tool: 可调用对象或 BaseTool 对象

    Returns:
        BaseTool: 一个带有人工审查功能的 BaseTool 对象
    """
    if not isinstance(tool, BaseTool):
        tool = create_tool(tool)

    @create_tool(
        tool.name,
        description=tool.description,
        args_schema=tool.args_schema
    )
    async def call_tool_with_interrupt(**tool_input):
        request = {
            "action_request": {"action": tool.name, "args": tool_input},
            "config": {"allow_accept": True, "allow_edit": True, "allow_respond": True, "allow_ignore": False},
            "description": f"准备调用 {tool.name} 工具：\n- 参数为: {tool_input}\n\n是否允许继续？\n输入 'yes' 接受工具调用\n输入 'no' 拒绝工具调用\n输入 'edit' 修改工具参数后调用工具\n输入 'response' 不调用工具直接反馈信息",
        }
        response = interrupt(request)
        logger.info(
            f"收到人工审查响应 | tool={tool.name!r} | "
            f"response_type={response.get('type')!r} | has_args={'args' in response}"
        )

        try:
            match response["type"]:
                case "accept":
                    tool_response = await tool.ainvoke(input=tool_input)
                case "edit":
                    tool_input = response["args"]["args"]
                    logger.info(f"用户编辑了工具参数 | tool={tool.name!r} | new_args={truncate(tool_input, 300)!r}")
                    tool_response = await tool.ainvoke(input=tool_input)
                case "reject":
                    tool_response = '该工具被拒绝使用，请尝试其他方法或拒绝回答问题。'
                case "response":
                    tool_response = response["args"]
                case _:
                    raise ValueError(f"Unsupported interrupt response type: {response['type']}")
        except Exception as e:
            log_exception(logger, "工具调用失败", e, extra_context={"tool": tool.name, "args": truncate(tool_input, 300)})
            raise
        logger.info(f"工具处理完成 | tool={tool.name!r} | response={truncate(tool_response, 500)!r}")
        return tool_response

    return call_tool_with_interrupt


# 获取工具列表 提供给第三方调用
async def get_tools():
    # 自定义工具 模拟酒店预定工具
    @tool("book_hotel", description="酒店预定工具")
    async def book_hotel(hotel_name: str):
        """支持酒店预定的工具

        Args:
            hotel_name: 酒店名称

        Returns:
            工具的调用结果
        """
        return f"成功预定了在{hotel_name}的住宿。"

    # 自定义工具 计算两个数的乘积的工具
    @tool("multiply", description="计算两个数的乘积的工具")
    async def multiply(a: float, b: float) -> float:
        """支持计算两个数的乘积的工具

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
        "amap-maps-streamableHTTP": {
            "url": "https://mcp.amap.com/mcp?key=" + os.getenv("AMAP_MAPS_API_KEY"),
            "transport": "streamable_http"
        }
    })
    # 从MCP Server中获取可提供使用的全部工具并添加人工审查
    tools = [await add_human_in_the_loop(t) for t in await client.get_tools()]

    # 追加自定义工具并添加人工审查
    tools.append(await add_human_in_the_loop(book_hotel))
    tools.append(multiply)

    return tools
