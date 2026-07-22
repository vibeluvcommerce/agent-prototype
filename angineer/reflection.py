"""反思模块（Pattern 1: Reflection 的落地, 主参考 hermes-agent 的辅助模块）.

参考项目: NousResearch/hermes-agent 的内核自检循环 ——
每执行 N 次工具调用暂停一次, 评估执行轨迹; 失败结果沉淀为结构化"教训"
(lessons), 供 Replanner 修正后续计划使用.

对应架构图: Debug and Refine 框的左半边(执行期自省);
右半边(计划级修正)在 graph.py 的 Replanner 节点.
"""
from __future__ import annotations


class ReflectionModule:
    def __init__(self, every_n: int = 3):
        self.every_n = every_n
        self.tool_calls = 0

    def on_tool_result(self, tool_name: str, result: dict, lessons: list[str]) -> str | None:
        """每次工具调用后触发; 返回需要打印的自检信息(没有则返回 None)."""
        self.tool_calls += 1
        status = result.get("status")

        if status == "fail":
            lesson = f"工具 {tool_name} 失败: {result.get('summary', '')} -> 调整策略后重试"
            lessons.append(lesson)
            return f"[Reflection] 记录教训#{len(lessons)}: {lesson}"
        if status == "error":
            lesson = f"工具 {tool_name} 调用错误: {result.get('summary', '')} -> 按工具 schema 修正参数"
            lessons.append(lesson)
            return f"[Reflection] 记录教训#{len(lessons)}: {lesson}"
        if status in ("denied", "rejected"):
            lesson = f"工具 {tool_name} 被人工/策略拦截: 此类操作需走人工审批"
            lessons.append(lesson)
            return f"[Reflection] 记录教训#{len(lessons)}: {lesson}"
        if self.tool_calls % self.every_n == 0:
            return (f"[Reflection] 周期性自检(第 {self.tool_calls} 次工具调用): "
                    "轨迹正常, 继续执行")
        return None
