"""主图（Pattern 2: Plan and Solve 的落地）.

参考项目: langchain-ai/langgraph 官方 Plan-and-Execute 范式 ——
Planner(规划) -> Executor(执行) -> Replanner(重规划) 三节点状态图,
并融合 SqueezeAILab/LLMCompiler 的思路: 计划是显式数据结构,
失败步骤会被重新入队修正(Debug and Refine).

对应架构图:
  Task Decomposition = planner_node
  Actions            = executor_node (驱动底部专业 Angineer 总线)
  Decisions          = replanner 的条件边 (继续 / 重规划 / 结束)
  Space Control      = 每步最大迭代数 + 最大重规划次数 (防止失控循环)
  Debug and Refine   = replanner_node + reflection.py
"""
from __future__ import annotations

import json
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from .agents_loader import AgentSpec
from .llm import BaseLLM
from .permissions import GLOBAL_PERMISSIONS, PermissionEngine
from .reflection import ReflectionModule
from .tools import ToolRegistry

MAX_STEP_ITERS = 8   # Space Control: 单步迭代上限(真实模型前期试错会消耗预算, 8 为平衡值)
MAX_REPLANS = 2      # Space Control: 最多重规划 2 次
MAX_CRITIQUES = 2    # Space Control: 单步最多 2 轮自我批判(Self-Refine 实践 1-2 轮即收敛)


class AgentState(TypedDict):
    task: str
    plan: list[dict]
    idx: int
    results: list[dict]
    lessons: list[str]
    replans: int
    done: bool


class AngineerApp:
    def __init__(self, llm: BaseLLM, registry: ToolRegistry,
                 agents: dict[str, AgentSpec],
                 ask_fn: Callable[[str, dict], bool],
                 log_fn: Callable[[str], None] = print):
        self.llm = llm
        self.registry = registry
        self.agents = agents
        self.ask_fn = ask_fn
        self.log = log_fn
        self.global_engine = PermissionEngine.from_mapping(GLOBAL_PERMISSIONS)
        self.reflector = ReflectionModule(every_n=3)
        self.graph = self._build_graph()

    # ---------------- 节点 ----------------
    def planner_node(self, state: AgentState) -> dict:
        self.log(f"\n[Task Decomposition] 规划器(LLM={self.llm.name}) 分解任务...")
        plan = self.llm.decompose(state["task"], self.agents)
        for i, step in enumerate(plan, 1):
            step["status"] = "pending"
            self.log(f"  步骤{i}: {self.agents[step['agent']].display} -> {step['instruction']}")
        return {"plan": plan, "idx": 0, "results": [], "lessons": [], "replans": 0, "done": False}

    def executor_node(self, state: AgentState) -> dict:
        step = state["plan"][state["idx"]]
        agent = self.agents[step["agent"]]
        self.log(f"\n[Actions] 执行步骤{state['idx'] + 1}: {agent.display}")
        self.log(f"  任务: {step['instruction']}  可用工具: {agent.tools}")

        engine = self.global_engine.merged(agent.permission_overrides)
        scratchpad: list[dict] = []
        final, ok = "", False

        # 跨步骤上下文注入: 后续代理(尤其总工)需要看到前置专业结果
        instruction = step["instruction"]
        if state["results"]:
            prior = "\n".join(f"- [{r['agent']}] {r['summary'][:300]}"
                              for r in state["results"][-4:])
            instruction = f"{instruction}\n\n前置步骤结果:\n{prior}"

        critique_rounds = 0
        for _ in range(MAX_STEP_ITERS):
            action = self.llm.act(agent, instruction, scratchpad)
            if "final" in action:
                # 真实模型的 final 可能是结构化 dict, 统一归一为字符串
                raw_final = action["final"]
                final_text = raw_final if isinstance(raw_final, str) else json.dumps(
                    raw_final, ensure_ascii=False)
                # P1 自我批判环(Self-Refine/LangGraph Basic Reflection 结构):
                # 结论接受前先由评审模型对照任务目标批判, 不通过则带意见修订
                review = self.llm.critique(agent, instruction, final_text, scratchpad)
                if review.get("verdict") == "revise" and critique_rounds < MAX_CRITIQUES:
                    critique_rounds += 1
                    self.log(f"  [Reflection] 自我批判(第{critique_rounds}轮): "
                             f"{review.get('critique', '')} -> 要求修订")
                    scratchpad.append({"type": "critique", "result": {
                        "status": "critique", "summary": review.get("critique", "")}})
                    continue
                if critique_rounds or review.get("verdict") == "approve":
                    self.log(f"  [Reflection] 评审通过: {str(review.get('critique', ''))[:60]}")
                final, ok = final_text, True
                break
            tool_name = action.get("tool", "")
            if agent.tools and tool_name not in agent.tools:
                scratchpad.append({"type": "tool", "tool": tool_name, "result": {
                    "status": "error", "summary": f"工具 {tool_name} 不在 {agent.name} 的授权清单内"}})
                continue
            self.log(f"  [Engineer Tools] 调用 {tool_name} {action.get('args', {})}")
            if tool_name == "team.ask":  # P4 代理间协商(元工具, 不走注册表)
                result = self._consult(agent, action.get("args", {}))
            else:
                result = self.registry.invoke(tool_name, action.get("args", {}), engine, self.ask_fn)
            result["tool"] = tool_name
            self.log(f"    -> {result.get('status')}: {result.get('summary')}")
            scratchpad.append({"type": "tool", "tool": tool_name, "result": result})
            note = self.reflector.on_tool_result(tool_name, result, state["lessons"])
            if note:
                self.log(f"    {note}")

        if not ok:
            final = final or f"步骤超过 {MAX_STEP_ITERS} 次迭代仍未收敛(Space Control 强制停止)"
        result_row = {"agent": agent.display, "ok": ok, "summary": final}
        self.log(f"  [Actions] {agent.display} 完成: {final}")
        return {"results": state["results"] + [result_row]}

    def _consult(self, from_agent: AgentSpec, args: dict) -> dict:
        """P4 代理间协商: 一个专业代理执行中向另一个代理提问(禁止嵌套协商)."""
        target = self.agents.get(args.get("agent", ""))
        question = args.get("question", "")
        if target is None or target.name == from_agent.name:
            return {"status": "error",
                    "summary": f"team.ask 目标代理无效: {args.get('agent')}"}
        self.log(f"  [Multi-Agent] {from_agent.display} 咨询 {target.display}: {question[:60]}")
        engine = self.global_engine.merged(target.permission_overrides)
        scratch: list[dict] = []
        answer = ""
        for _ in range(3):
            action = self.llm.act(target, question, scratch)
            if "final" in action:
                raw = action["final"]
                answer = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                break
            tool_name = action.get("tool", "")
            if tool_name == "team.ask":
                scratch.append({"type": "tool", "tool": tool_name, "result": {
                    "status": "error", "summary": "不支持嵌套协商"}})
                continue
            if target.tools and tool_name not in target.tools:
                scratch.append({"type": "tool", "tool": tool_name, "result": {
                    "status": "error", "summary": f"工具 {tool_name} 不在授权清单内"}})
                continue
            r = self.registry.invoke(tool_name, action.get("args", {}), engine, self.ask_fn)
            r["tool"] = tool_name
            self.log(f"    [Multi-Agent] {target.display} 调用 {tool_name} -> {r.get('status')}")
            scratch.append({"type": "tool", "tool": tool_name, "result": r})
        if not answer:
            answer = "被咨询代理未能在 3 步内给出结论"
        self.log(f"  [Multi-Agent] {target.display} 答复: {answer[:80]}")
        self.registry._log("team.ask", args, "ok")  # 协商也留痕, 保证可观察性
        return {"status": "ok", "summary": f"[协商结果] {target.display}: {answer}"}

    def replanner_node(self, state: AgentState) -> dict:
        last = state["results"][-1] if state["results"] else None
        # 计划级修正: 步骤失败 -> 重新入队一个修正步骤 (Debug and Refine)
        if last and not last["ok"] and state["replans"] < MAX_REPLANS:
            fix = dict(state["plan"][state["idx"]])
            fix["instruction"] = ("上次执行失败, 根据教训调整策略重试: "
                                  + (state["lessons"][-1] if state["lessons"] else ""))
            plan = state["plan"][:state["idx"] + 1] + [fix] + state["plan"][state["idx"] + 1:]
            self.log(f"\n[Debug and Refine] 步骤失败, 重规划(第{state['replans'] + 1}次): "
                     f"插入修正步骤后重试")
            return {"plan": plan, "idx": state["idx"] + 1, "replans": state["replans"] + 1}
        # 正常推进 (Decisions)
        if state["idx"] + 1 < len(state["plan"]):
            return {"idx": state["idx"] + 1}
        # P5 验收门禁: 流程结束前的最后人工确认(LangGraph interrupt 式暂停点)
        ok_count = sum(1 for r in state["results"] if r["ok"])
        approved = self.ask_fn("final_acceptance", {
            "通过步骤": f"{ok_count}/{len(state['results'])}",
            "报告": "output/engineering_report.md"})
        self.log("\n[Decisions] 全部步骤完成")
        self.log(f"[HITL] 验收门禁: {'验收通过, 流程结束' if approved else '验收未通过, 结果保留待人工处置'}")
        return {"done": True}

    @staticmethod
    def _route(state: AgentState) -> str:
        return "end" if state["done"] else "execute"

    def _build_graph(self):
        g = StateGraph(AgentState)
        g.add_node("plan", self.planner_node)
        g.add_node("execute", self.executor_node)
        g.add_node("replan", self.replanner_node)
        g.add_edge(START, "plan")
        g.add_edge("plan", "execute")
        g.add_edge("execute", "replan")
        g.add_conditional_edges("replan", self._route, {"execute": "execute", "end": END})
        return g.compile()

    def run(self, task: str) -> AgentState:
        return self.graph.invoke({
            "task": task, "plan": [], "idx": 0, "results": [],
            "lessons": [], "replans": 0, "done": False,
        })
