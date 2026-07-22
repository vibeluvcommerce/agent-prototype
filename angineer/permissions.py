"""权限引擎（Pattern 5: Human-in-the-Loop 的落地）.

参考项目: sst/opencode 的权限模型 —— allow / ask / deny 三态,
按工具名通配符逐条匹配, "后匹配优先", agent 级规则覆盖全局规则.

对应架构图: Engineer Tools 框的"受控接口层" —— 权限收敛在工具级,
而不是把信任整体放给模型（文章 Pattern 3/5 的核心论点）.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass

ALLOW, ASK, DENY = "allow", "ask", "deny"


@dataclass
class Rule:
    pattern: str
    action: str


class PermissionEngine:
    """opencode 风格: 规则列表顺序扫描, 后匹配覆盖先匹配."""

    def __init__(self, rules: list[Rule] | None = None, default: str = ASK):
        self.rules = rules or []
        self.default = default

    @classmethod
    def from_mapping(cls, mapping: dict | None, default: str = ASK) -> "PermissionEngine":
        return cls([Rule(p, a) for p, a in (mapping or {}).items()], default)

    def merged(self, overrides: dict | None) -> "PermissionEngine":
        """agent 声明的权限追加在末尾 -> 优先级高于全局规则."""
        extra = [Rule(p, a) for p, a in (overrides or {}).items()]
        return PermissionEngine(self.rules + extra, self.default)

    def check(self, tool_name: str) -> str:
        action = self.default
        for r in self.rules:
            if fnmatch.fnmatchcase(tool_name, r.pattern):
                action = r.action
        return action


# 全局默认权限(对应架构图 Safety / 生产环境的受控要求):
GLOBAL_PERMISSIONS = {
    "team.ask": ALLOW,                 # 代理间协商(P4): 内部通信, 放行
    "kg_query": ALLOW,                 # 知识图谱查询: 只读, 放行
    "data_transform": ALLOW,           # 数据预处理: 内部操作, 放行
    "inspection.query": ALLOW,         # 检测数据查询: 只读, 放行
    "discharge_sensor.*": ALLOW,       # 放电监测: 只读传感器, 放行
    "structure_calc.*": ALLOW,         # 结构校核: 纯计算, 放行
    "cam_check.*": ALLOW,              # CAM 检查: 纯计算, 放行
    "optimization.*": ASK,             # 修改设计参数: 需工程师确认
    "safety_audit.*": ASK,             # 安全审计: 需工程师确认
    "report.write": ASK,               # 落盘报告: 需确认
    "production.*": DENY,              # 应用结果到生产: 原型默认禁止(只能人工)
}
