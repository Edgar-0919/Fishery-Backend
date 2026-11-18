"""Agent 能力层（单包）

历史说明：改造前项目已有 ``app/agents/``（LLM 调用封装），Agent 化时新增了
``app/agent/``（循环 / 工具 / Guard）。两个包名只差一个 s 且互相依赖，现已
合并为本包。包内按职责分三层：

1. LLM 基础：config.py、rate_limit.py、llm.py、prompt.py
2. 问答：core.py
3. Agent 运行时：proposals.py、tools.py、guard.py、loop.py、pipeline.py、
   decision.py（FALLBACK_ACTIONS 规则兜底）、audit.py、memory.py、gateway.py

设计约束：
- LLM 只有「提议权」，执行器保留「否决权」。
- 本包不直接写 device_commands，所有动作必须经 app.tools.executor.execute_with_check()。
- 紧急规则在 app.services.data_processor 同步路径，永不进入本包。
- 决策链路唯一：app.services.agent_pipeline 提交，app.agent.loop 执行，无模式分流。
"""

from app.agent.proposals import ALLOWED_ACTIONS, ActionProposal, AgentRun, AgentState

__all__ = ["ALLOWED_ACTIONS", "ActionProposal", "AgentState", "AgentRun"]
