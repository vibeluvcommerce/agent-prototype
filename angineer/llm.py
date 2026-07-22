"""LLM 抽象层: 规划与动作决策的统一接口.

- MockLLM  : 离线规则驱动, 无需 API Key, 保证离线/CI 可复现
- OpenAILLM: 任何 OpenAI 兼容接口(含开源基座 vLLM/Ollama), 通过环境变量配置

对应架构图: LLM 框。规划(Pattern 2)与工具选择(Pattern 3)都经由此层,
方便你们把开源基座模型换进来做 BFCL 风格的工具调用评测(参考 gorilla).
"""
from __future__ import annotations

import json
import os
import re

from .agents_loader import AgentSpec


class BaseLLM:
    name = "base"

    def decompose(self, task: str, agents: dict[str, AgentSpec]) -> list[dict]:
        """Pattern 2 的第一步: 把任务分解为 {agent, instruction} 步骤序列."""
        raise NotImplementedError

    def act(self, agent: AgentSpec, instruction: str, scratchpad: list[dict]) -> dict:
        """ReAct 一步: 返回 {"tool": name, "args": {...}} 或 {"final": "..."}"""
        raise NotImplementedError

    def critique(self, agent: AgentSpec, instruction: str, final: str,
                 scratchpad: list[dict]) -> dict:
        """Pattern 1 自我批判环的评审一步 (Self-Refine 式 generator-critic).

        返回 {"verdict": "approve"|"revise", "critique": "..."}"""
        raise NotImplementedError


# ---------------------------------------------------------------- MockLLM
class MockLLM(BaseLLM):
    """规则驱动, 确定性输出, 离线规则实现."""

    name = "mock(离线规则)"

    KEYWORD_ROUTING = [
        (r"结构|强度|应力", "structure_design", "完成结构强度校核并给出安全系数"),
        (r"芯片|chip|SoC", "chip_design", "完成芯片功耗/时序/DRC 仿真"),
        (r"PCB|CAM|载流|线宽", "pcb_cam", "完成 PCB 载流 CAM 检查, 不通过则调参复检"),
        (r"安全|审计|安规", "safety", "准备监测数据并执行安全审计"),
        (r"放电|监测|传感器", "discharge_monitoring", "读取放电监测数据并汇总运行状态"),
        (r"检测|检验|质量", "discharge_monitoring", "查询质量检测记录"),
    ]

    def decompose(self, task: str, agents: dict[str, AgentSpec]) -> list[dict]:
        steps, seen = [], set()
        for pattern, agent_name, instruction in self.KEYWORD_ROUTING:
            if re.search(pattern, task, re.IGNORECASE) and agent_name not in seen \
                    and agent_name in agents:
                steps.append({"agent": agent_name, "instruction": instruction})
                seen.add(agent_name)
        if not steps:  # 兜底: 未命中关键词时交给总工
            steps.append({"agent": "chief", "instruction": f"分析任务并直接给出结论: {task}"})
        # 总工程师收尾: 汇总 + 写报告 (+ 尝试应用变更, 验证权限拦截)
        if "chief" in agents and "chief" not in seen:
            steps.append({"agent": "chief",
                          "instruction": "汇总各专业结果, 生成工程报告, 并评估是否申请应用变更"})
        return steps

    # ---- 各专业代理的脚本化 ReAct 策略(真实场景由模型按工具 schema 自主选择) ----
    def act(self, agent: AgentSpec, instruction: str, scratchpad: list[dict]) -> dict:
        calls = [s for s in scratchpad if s.get("type") == "tool"]
        last = calls[-1]["result"] if calls else None
        n = len(calls)

        if agent.name == "structure_design":
            if n == 0:
                return {"tool": "kg_query", "args": {"query": "铝合金6061 屈服强度"}}
            if n == 1:
                return {"tool": "structure_calc.run", "args": {"model": "电池仓主梁"}}
            # 结论引用工具真实返回(与 --upload 载入的数据保持一致)
            return {"final": f"结构校核结论: {last['summary']}"}

        if agent.name == "chip_design":
            if n == 0:
                return {"tool": "chip_sim.run", "args": {"design": "电源管理 SoC"}}
            return {"final": "芯片仿真通过: 功耗 3.8W, 时序裕量 +0.12ns, DRC 0 违例。"}

        if agent.name == "pcb_cam":
            cam_calls = [c for c in calls if c["tool"] == "cam_check.run"]
            consults = [c for c in calls if c["tool"] == "team.ask"]
            opt_done = any(c["tool"] == "optimization.set_param"
                           and c["result"].get("status") == "ok" for c in calls)
            cam_passed = any(c["tool"] == "cam_check.run"
                             and c["result"].get("status") == "ok" for c in calls)
            if not cam_calls:
                return {"tool": "cam_check.run", "args": {"board": "功率主板"}}
            # P4 协商: 调参前先咨询安全工程师对温升/安规的影响
            if cam_calls[-1]["result"].get("status") == "fail" and not consults:
                return {"tool": "team.ask",
                        "args": {"agent": "safety",
                                 "question": "功率主板线宽拟加粗以满足载流要求, "
                                             "请评估对整机温升与安规的影响"}}
            if cam_calls[-1]["result"].get("status") == "fail" and not opt_done:
                return {"tool": "optimization.set_param",
                        "args": {"param": "trace_width_mm", "value": 0.35}}
            if opt_done and not cam_passed:
                return {"tool": "cam_check.run",
                        "args": {"board": "功率主板", "trace_width_mm": 0.35}}
            # 结论引用 CAM 工具真实返回, 并如实说明是否经过协商/调参
            final = f"CAM 检查结论: {cam_calls[-1]['result']['summary']}"
            if consults:
                final += " 调整前经 team.ask 咨询 Safety Angineer 确认温升与安规影响。"
            if opt_done:
                final += " 已经 optimization.set_param 调参并复检。"
            return {"final": final}

        if agent.name == "safety":
            # P4 被咨询分支: 其他代理通过 team.ask 发来的问题
            # (注入的前置上下文也含"线宽", 用"前置步骤结果"排除本步指令)
            if "线宽" in instruction and "前置步骤结果" not in instruction:
                return {"final": "温升评估: 线宽加粗后铜截面积增大, 焦耳热下降, "
                                 "对整机温升为正向影响, 与安规无冲突; "
                                 "建议调参后复检 CAM 并复核温升数据后再放行。"}
            if n == 0:
                return {"tool": "data_transform", "args": {"dataset": "温升监测数据"}}
            if n == 1:
                return {"tool": "safety_audit.run", "args": {"scope": "整机安规"}}
            # 结论引用审计工具真实返回(数值来自 EngineeringWorld/上传数据)
            return {"final": f"安全审计结论: {last['summary']}; "
                             f"明细: {'; '.join(last.get('data', []))}"}

        if agent.name == "discharge_monitoring":
            if n == 0:
                return {"tool": "discharge_sensor.read", "args": {"channel": "pack-01"}}
            if n == 1:
                return {"tool": "inspection.query", "args": {}}   # item 缺省取 World 中产品名
            sensor_sum = calls[0]["result"]["summary"]
            insp_sum = calls[1]["result"]["summary"]
            return {"final": f"监测汇总: {sensor_sum}; {insp_sum}。"}

        if agent.name == "chief":
            rep = [c for c in calls if c["tool"] == "report.write"]
            applied = [c for c in calls if c["tool"] == "production.apply"]
            critiques = [s for s in scratchpad if s.get("type") == "critique"]
            if not rep:
                return {"tool": "report.write",
                        "args": {"path": "output/engineering_report.md",
                                 "content": "# 工程报告\n\n(由 Chief Angineer 汇总生成)\n"}}
            if not applied:
                return {"tool": "production.apply", "args": {"change": "设计参数变更(详见工程报告)"}}
            rejected = rep[-1]["result"].get("status") != "ok"
            # P1 自我批判后: 输出结构化的修订版结论(明细见报告, 不编造数值)
            if critiques:
                return {"final": "修订版工程结论: 各专业步骤已完成, 明细数据以工程报告为准; "
                                 "风险分级: 审计建议项列入整改, 无 P0 阻断项; 变更去向: "
                                 "生产变更因权限 deny 已转人工审批, 验收门禁待操作员确认。"}
            if rejected:
                return {"final": "报告写入被操作员拒绝; 生产变更被权限策略拦截, 已全部转人工处理。"}
            return {"final": "报告已生成; 生产变更被权限策略拦截, 已转人工审批流程。"}

        return {"final": f"{agent.display} 已完成: {instruction}"}

    # ---- P1 自我批判环的 Mock 评审: 验证一次"评审不通过 -> 修订"循环 ----
    def critique(self, agent: AgentSpec, instruction: str, final: str,
                 scratchpad: list[dict]) -> dict:
        already = any(s.get("type") == "critique" for s in scratchpad)
        if agent.name == "chief" and not already:
            return {"verdict": "revise",
                    "critique": "结论缺少各专业结果的结构化汇总、风险分级与变更去向, "
                                "需修订补充后再定稿"}
        return {"verdict": "approve", "critique": "结论完整回应了任务目标"}


# ---------------------------------------------------------------- OpenAILLM
class OpenAILLM(BaseLLM):
    """OpenAI 兼容接口(开源基座可用 vLLM/Ollama 的兼容端点).

    环境变量: OPENAI_API_KEY / OPENAI_BASE_URL / ANGINEER_MODEL
    """

    name = "openai-compatible"

    def __init__(self) -> None:
        from openai import OpenAI  # 延迟导入, 未安装时 MockLLM 不受影响
        self.client = OpenAI(timeout=60)
        self.model = os.environ.get("ANGINEER_MODEL", "gpt-4o-mini")

    def _chat(self, system: str, user: str) -> str:
        kwargs = dict(model=self.model,
                      messages=[{"role": "system", "content": system},
                                {"role": "user", "content": user}])
        # 部分模型(如 kimi k3)不允许自定义 temperature, 默认不传;
        # 需要时通过 ANGINEER_TEMPERATURE 显式指定
        if os.environ.get("ANGINEER_TEMPERATURE"):
            kwargs["temperature"] = float(os.environ["ANGINEER_TEMPERATURE"])
        resp = self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    @staticmethod
    def _extract_json(text: str):
        m = re.search(r"[\[{].*[\]}]", text, re.DOTALL)
        return json.loads(m.group(0)) if m else None

    def decompose(self, task: str, agents: dict[str, AgentSpec]) -> list[dict]:
        roster = "\n".join(f"- {a.name}: {a.description}" for a in agents.values())
        system = ("你是任务规划器(Pattern 2: Plan and Solve)。把用户任务分解为有序步骤, "
                  "每步指派给最合适的专业代理。只输出 JSON 数组: "
                  '[{"agent": "代理名", "instruction": "该步要做什么"}]')
        user = f"可用代理:\n{roster}\n\n任务: {task}"
        steps: list[dict] = []
        try:
            data = self._extract_json(self._chat(system, user)) or []
            steps = [s for s in data if isinstance(s, dict)
                     and s.get("agent") in agents and s.get("instruction")]
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 真实模型规划调用失败({e})")
        if not steps:
            print("[warn] 真实模型未产出有效计划, 回退 MockLLM 规则分解兜底")
            steps = MockLLM().decompose(task, agents)
        return steps

    def act(self, agent: AgentSpec, instruction: str, scratchpad: list[dict]) -> dict:
        history = json.dumps(scratchpad[-6:], ensure_ascii=False)
        system = (f"{agent.prompt}\n你只能使用这些工具: {agent.tools}。"
                  '其中 team.ask 用于咨询其他专业代理, 参数为 {"agent": "目标代理名", '
                  '"question": "问题"}。'
                  "每一步只输出一个 JSON: 调用工具 "
                  '{"tool": "工具名", "args": {...}} 或给出结论 {"final": "..."}')
        user = f"当前步骤: {instruction}\n最近执行记录: {history}"
        for _ in range(2):  # 解析失败重试一次
            try:
                data = self._extract_json(self._chat(system, user))
            except Exception:  # noqa: BLE001
                data = None
            if isinstance(data, dict) and ("final" in data or "tool" in data):
                return data
        return {"final": "模型多次未产出有效决策, 本步骤转人工处理。"}

    def critique(self, agent: AgentSpec, instruction: str, final: str,
                 scratchpad: list[dict]) -> dict:
        if not isinstance(final, str):  # 模型可能返回结构化结论
            final = json.dumps(final, ensure_ascii=False)
        system = ("你是严格的工程评审员(Pattern 1: Reflection)。评审以下专业代理的"
                  "结论是否完整回应了任务目标(结论有据/风险说明/可操作性)。"
                  '只输出 JSON: {"verdict": "approve" 或 "revise", "critique": "具体意见"}')
        user = f"任务: {instruction}\n\n结论:\n{final[:2000]}"
        for _ in range(2):
            try:
                data = self._extract_json(self._chat(system, user))
            except Exception:  # noqa: BLE001
                data = None
            if isinstance(data, dict) and data.get("verdict") in ("approve", "revise"):
                return data
        # 解析失败默认通过(闭环由 MAX_CRITIQUES 上限保护)
        return {"verdict": "approve", "critique": "评审输出解析失败, 默认通过"}


def get_llm(force: str | None = None) -> BaseLLM:
    if force == "mock":
        return MockLLM()
    if force == "real" or os.environ.get("OPENAI_API_KEY"):
        try:
            return OpenAILLM()
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 真实 LLM 初始化失败({e}), 回退 MockLLM")
    return MockLLM()
