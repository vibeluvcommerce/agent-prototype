"""Angineer Agent Prototype - 工程领域多智能体原型.

架构映射（对照 Angineer Large Model 架构图）:
  Task Decomposition   -> angineer/graph.py  : LangGraph Planner 节点
  Decisions / Actions  -> angineer/graph.py  : Replanner 条件边 + Executor
  Space Control        -> angineer/graph.py  : StateGraph 状态机(迭代上限/重规划次数)
  Engineer Tools       -> angineer/tools.py  : MCP 风格工具注册表 + 权限包裹执行
  Engineer Knowledge   -> tools.kg_query     : 工程知识图谱查询(模拟)
  Data Preparation     -> tools.data_transform
  Debug and Refine     -> angineer/graph.py  : Replanner 节点 + reflection.py
  Optimization         -> tools.optimization_set_param
  Apply Results        -> tools.production_apply (默认 deny, 验证 HITL)
  Writing Report       -> tools.report_write
  底部专业 Angineer 总线 -> agents/*.md       : opencode 风格声明式子代理
"""
__version__ = "0.1.0"
