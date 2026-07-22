"""工具层（Pattern 3: Tool Use 的落地）.

参考项目: modelcontextprotocol (MCP) —— 每个工具以声明式 schema 暴露
(name / description / parameters JSON Schema / handler), agent 不直接接触
外部系统, 所有调用穿过这个"受控调用层"(bounded invocation layer):
  schema 校验 -> 权限检查(permissions.py) -> 执行 -> 结构化返回.

对应架构图: Engineer Tools 框; kg_query=Engineer Knowledge Graph;
data_transform=Data Preparation and Transform; optimization=Optimization;
production_apply=Apply Results; report_write=Writing Report.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .permissions import ALLOW, ASK, DENY, PermissionEngine


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                      # JSON Schema (MCP inputSchema 等价物)
    handler: Callable[[dict], dict]
    readonly: bool = False


class EngineeringWorld:
    """工程环境状态: 工具之间通过它产生真实依赖(改参数会影响后续检查结果).

    默认值是内置模拟数据; 通过 --upload 上传设计 JSON / 监测 CSV 后,
    以下状态会被真实数据覆盖(见 upload.py), 工具的判定逻辑不变.
    """

    def __init__(self) -> None:
        self.product_name = "户外储能电源"
        # PCB 设计状态: 当前线宽不满足载流要求 -> 第一次 CAM 检查必然 FAIL,
        # 用于验证 "检查失败 -> Optimization 调参 -> 复检通过" 的 Debug and Refine 闭环.
        self.trace_width_mm = 0.20
        self.required_width_mm = 0.30
        # 结构状态
        self.beam_stress_mpa = 182.0
        self.beam_limit_mpa = 250.0
        # 放电监测(只读)
        self.discharge = {"soc": 0.62, "voltage_v": 51.2, "temp_c": 41.5, "status": "normal"}
        # 安规状态(温升裕量/防护等级)
        self.safety = {"thermal_margin_c": 18.5, "thermal_limit_c": 20.0, "ip_rating": "IP54"}
        # 质量检测记录
        self.inspection = {"item": "户外储能电源", "fpy_30d": 0.986}
        # 用户上传的监测数据(--upload .csv): None 表示使用内置模拟值
        self.sensor: dict | None = None
        self.report_sections: list[str] = []


class ToolRegistry:
    def __init__(self, world: EngineeringWorld | None = None):
        self.world = world or EngineeringWorld()
        self._tools: dict[str, Tool] = {}
        self.call_log: list[dict] = []
        self._register_engineering_tools()

    # ---------- MCP 风格接口 ----------
    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def list_tools(self) -> list[dict]:
        """等价于 MCP 的 tools/list: 给规划器/模型的工具 schema 清单."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "readonly": t.readonly,
            }
            for t in self._tools.values()
        ]

    def invoke(self, name: str, args: dict, engine: PermissionEngine,
               ask_fn: Callable[[str, dict], bool]) -> dict:
        """受控调用: schema 校验 -> 权限 -> 执行 -> 日志."""
        tool = self._tools.get(name)
        if tool is None:
            return {"status": "error", "summary": f"未知工具 {name}"}

        missing = [p for p in tool.parameters.get("required", []) if p not in args]
        if missing:
            return {"status": "error", "summary": f"{name} 缺少参数: {missing}"}

        decision = engine.check(name)
        if decision == DENY:
            self._log(name, args, "denied")
            return {"status": "denied",
                    "summary": f"[HITL] {name} 被权限策略禁止(deny), 需人工在权限文件中显式放行"}
        if decision == ASK and not tool.readonly:
            if not ask_fn(name, args):
                self._log(name, args, "rejected")
                return {"status": "rejected",
                        "summary": f"[HITL] 操作员拒绝了 {name} 的执行请求"}

        result = tool.handler(args)
        self._log(name, args, result.get("status", "ok"))
        return result

    def _log(self, name: str, args: dict, status: str) -> None:
        self.call_log.append({"tool": name, "args": args, "status": status, "ts": time.time()})

    # ---------- 工程工具集(模拟真实工程软件) ----------
    def _register_engineering_tools(self) -> None:
        w = self.world

        def kg_query(args: dict) -> dict:
            facts = {
                "铝合金6061": {"屈服强度_mpa": 276, "密度_g_cm3": 2.70},
                "FR-4": {"玻璃化温度_c": 135, "铜厚_oz": 1},
                "磷酸铁锂电芯": {"标称电压_v": 3.2, "最大放电_c": 3},
            }
            hit = {k: v for k, v in facts.items() if k in args["query"]}
            if hit:
                return {"status": "ok", "summary": f"知识图谱命中 {len(hit)} 条", "data": hit}
            return {"status": "ok",
                    "summary": f"未精确命中, 返回全部 {len(facts)} 条参考条目(内置知识子集)",
                    "data": facts}

        def data_transform(args: dict) -> dict:
            if w.sensor:  # 用户上传的真实监测数据(--upload .csv)
                s = w.sensor
                return {"status": "ok",
                        "summary": (f"已清洗 {s['source']}: 实际 {s['rows']} 行, "
                                    f"剔除空值 {s['nulls']} 个, 去噪+对齐采样率"),
                        "data": {"rows": s["rows"], "nulls_dropped": s["nulls"],
                                 "columns": s["columns"], "ranges": s["ranges"]}}
            return {"status": "ok",
                    "summary": f"已清洗 {args.get('dataset', '传感器数据')}: 12480 行, 去噪+对齐采样率",
                    "data": {"rows": 12480, "nulls_dropped": 37}}

        def structure_calc(args: dict) -> dict:
            ok = w.beam_stress_mpa < w.beam_limit_mpa
            return {"status": "ok" if ok else "fail",
                    "summary": (f"结构校核: 最大应力 {w.beam_stress_mpa:.0f}MPa "
                                f"< 许用 {w.beam_limit_mpa:.0f}MPa, 安全系数 "
                                f"{w.beam_limit_mpa / w.beam_stress_mpa:.2f} -> "
                                + ("通过" if ok else "不通过")),
                    "data": {"stress_mpa": w.beam_stress_mpa, "pass": ok}}

        def cam_check(args: dict) -> dict:
            width = args.get("trace_width_mm", w.trace_width_mm)
            if width >= w.required_width_mm:
                return {"status": "ok",
                        "summary": f"CAM 检查: 线宽 {width:.2f}mm 满足载流要求 -> 通过",
                        "data": {"pass": True, "trace_width_mm": width}}
            return {"status": "fail",
                    "summary": (f"CAM 检查: 线宽 {width:.2f}mm < 要求 {w.required_width_mm:.2f}mm, "
                                "载流密度超限约 27% -> 不通过"),
                    "data": {"pass": False, "trace_width_mm": width,
                             "required_mm": w.required_width_mm}}

        def optimization_set_param(args: dict) -> dict:
            param, value = args["param"], float(args["value"])
            if param == "trace_width_mm":
                w.trace_width_mm = value
            return {"status": "ok",
                    "summary": f"[Optimization] 设计参数 {param} 已更新为 {value}",
                    "data": {param: value}}

        def chip_sim(args: dict) -> dict:
            return {"status": "ok",
                    "summary": (f"芯片仿真({args.get('design', 'SoC')}): 功耗 3.8W, "
                                "时序裕量 +0.12ns, DRC 0 违例 -> 通过"),
                    "data": {"power_w": 3.8, "timing_slack_ns": 0.12, "drc_violations": 0}}

        def safety_audit(args: dict) -> dict:
            s = w.safety
            margin = s["thermal_margin_c"]
            limit = s["thermal_limit_c"]
            headroom = limit - margin          # 距限值的剩余裕量
            ok = headroom > 0
            findings = [f"电池仓温升裕量 {margin}C (限值 {limit}C, 剩余 {headroom:.1f}C)",
                        f"外壳防护等级 {s['ip_rating']}" + ("满足户外要求" if ok else "需结合温升复核"),
                        "建议: 增加泄压阀标识"]
            if 0 < headroom < 2:
                findings.append(f"关注: 温升剩余裕量仅 {headroom:.1f}C, 建议热设计复核")
            verdict = "通过" if ok else "不通过"
            return {"status": "ok" if ok else "fail",
                    "summary": f"安全审计{verdict}, {len(findings)} 项结论(1 项建议)",
                    "data": findings}

        def inspection_query(args: dict) -> dict:
            rec = w.inspection
            return {"status": "ok",
                    "summary": (f"检测记录: {args.get('item', rec['item'])} "
                                f"近 30 天一次合格率 {rec['fpy_30d']*100:.1f}%"),
                    "data": {"fpy": rec["fpy_30d"]}}

        def discharge_sensor_read(args: dict) -> dict:
            d = w.discharge
            return {"status": "ok",
                    "summary": (f"放电监测(只读): SOC {d['soc']*100:.0f}%, "
                                f"{d['voltage_v']}V, {d['temp_c']}C, 状态 {d['status']}"),
                    "data": dict(d)}

        def report_write(args: dict) -> dict:
            path = args.get("path", "output/engineering_report.md")
            import os
            dir_name = os.path.dirname(path)
            if dir_name:                     # 裸文件名(如"报告.md")无需建目录
                os.makedirs(dir_name, exist_ok=True)
            body = args.get("content", "")
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            return {"status": "ok", "summary": f"[Writing Report] 报告已写入 {path}", "data": {"path": path}}

        def production_apply(args: dict) -> dict:
            # 默认在权限层被 deny, 正常不会执行到这里
            return {"status": "ok", "summary": "[Apply Results] 变更已应用到生产系统"}

        self.register(Tool("kg_query", "查询工程知识图谱(材料/工艺/器件参数)",
                           {"type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"]}, kg_query, readonly=True))
        self.register(Tool("data_transform", "数据清洗与变换(传感器/CAD 数据预处理)",
                           {"type": "object",
                            "properties": {"dataset": {"type": "string"}}}, data_transform))
        self.register(Tool("structure_calc.run", "结构强度校核(应力/安全系数计算)",
                           {"type": "object",
                            "properties": {"model": {"type": "string"}},
                            "required": ["model"]}, structure_calc))
        self.register(Tool("cam_check.run", "PCB CAM 检查(线宽/载流/间距规则)",
                           {"type": "object",
                            "properties": {"board": {"type": "string"},
                                           "trace_width_mm": {"type": "number"}},
                            "required": ["board"]}, cam_check))
        self.register(Tool("optimization.set_param", "优化设计参数(修改设计变量)",
                           {"type": "object",
                            "properties": {"param": {"type": "string"},
                                           "value": {"type": "number"}},
                            "required": ["param", "value"]}, optimization_set_param))
        self.register(Tool("chip_sim.run", "芯片设计仿真(功耗/时序/DRC)",
                           {"type": "object",
                            "properties": {"design": {"type": "string"}},
                            "required": ["design"]}, chip_sim))
        self.register(Tool("safety_audit.run", "安全审计(安规/温升/防护等级核查)",
                           {"type": "object",
                            "properties": {"scope": {"type": "string"}},
                            "required": ["scope"]}, safety_audit))
        self.register(Tool("inspection.query", "质量检测记录查询(只读)",
                           {"type": "object",
                            "properties": {"item": {"type": "string"}}}, inspection_query, readonly=True))
        self.register(Tool("discharge_sensor.read", "放电监测数据读取(只读)",
                           {"type": "object",
                            "properties": {"channel": {"type": "string"}}}, discharge_sensor_read, readonly=True))
        self.register(Tool("report.write", "生成工程报告(写文件)",
                           {"type": "object",
                            "properties": {"path": {"type": "string"},
                                           "content": {"type": "string"}},
                            "required": ["content"]}, report_write))
        self.register(Tool("production.apply", "将设计变更应用到生产系统(高危)",
                           {"type": "object",
                            "properties": {"change": {"type": "string"}},
                            "required": ["change"]}, production_apply))

    def dump_log_jsonl(self, path: str) -> None:
        import os
        dir_name = os.path.dirname(path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for row in self.call_log:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
