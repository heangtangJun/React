"""HITL 契约自检：验证 interrupt 载荷为纯 dict 后，中断/恢复流程与前端契约保持一致。

运行：python test_hitl_contract.py
（不依赖网络 / 真实 LLM / PostgreSQL，使用假模型与内存 checkpointer）
"""
import asyncio

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from utils.tools import add_human_in_the_loop


class FakeToolChatModel(GenericFakeChatModel):
    """支持 bind_tools 的假聊天模型（直接返回自身）"""

    def bind_tools(self, tools, **kwargs):
        return self


@tool("demo_tool", description="demo")
async def demo_tool(x: int) -> str:
    """demo tool"""
    return f"got {x}"


async def main():
    wrapped = await add_human_in_the_loop(demo_tool)

    # 模型第一步强制调用 demo_tool，第二步给出最终回答
    first = AIMessage(content="", tool_calls=[{"name": "demo_tool", "args": {"x": 42}, "id": "call_1"}])
    second = AIMessage(content="done")
    model = FakeToolChatModel(messages=iter([first, second]))

    agent = create_agent(model=model, tools=[wrapped], checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t1"}}

    # 1) 首次调用应中断，且中断载荷含前端依赖的 action_request.action / .args
    result = await agent.ainvoke({"messages": [{"role": "user", "content": "hi"}]}, config=cfg)
    assert "__interrupt__" in result, f"expected interrupt, got: {result.keys()}"
    payload = result["__interrupt__"][0].value
    assert payload["action_request"]["action"] == "demo_tool", payload
    assert payload["action_request"]["args"] == {"x": 42}, payload
    assert payload["config"]["allow_accept"] and payload["config"]["allow_edit"], payload
    print("1. interrupt payload contract (action_request/config): OK")

    # 2) 以 accept 恢复 → 工具应真正执行并返回结果
    result2 = await agent.ainvoke(Command(resume={"type": "accept"}), config=cfg)
    tool_msgs = [m for m in result2["messages"] if getattr(m, "type", None) == "tool"]
    assert tool_msgs and "got 42" in str(tool_msgs[-1].content), tool_msgs
    print("2. resume with {'type':'accept'} executes tool: OK")

    # 3) reject / response 分支返回固定文案 / 用户反馈
    for rtype, expected in [("reject", "该工具被拒绝使用"), ("response", "自定义反馈")]:
        agent3 = create_agent(
            model=FakeToolChatModel(messages=iter([first, second])),
            tools=[await add_human_in_the_loop(demo_tool)],
            checkpointer=InMemorySaver(),
        )
        cfg3 = {"configurable": {"thread_id": f"t-{rtype}"}}
        await agent3.ainvoke({"messages": [{"role": "user", "content": "hi"}]}, config=cfg3)
        resume_arg = {"type": rtype} if rtype == "reject" else {"type": rtype, "args": expected}
        r = await agent3.ainvoke(Command(resume=resume_arg), config=cfg3)
        tm = [m for m in r["messages"] if getattr(m, "type", None) == "tool"]
        assert tm and expected in str(tm[-1].content), (rtype, tm)
    print("3. reject/response resume branches: OK")

    print("ALL HITL CONTRACT CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
