"""用户数据接入（--upload）：把用户上传的数据文件装载进 EngineeringWorld.

支持两类文件（按扩展名识别）：
  .json  设计状态文件 —— 覆盖 EngineeringWorld 初始状态（产品名/PCB/结构/放电/安规/检测）
  .csv   监测数据文件 —— 真实读入并统计行数/空值/数值范围，供 data_transform 使用

设计原则：只初始化"环境状态"，不注入结论——合规与否仍由工具逻辑与代理流程判定。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


class UploadError(Exception):
    """用户上传文件无法解析或不符合约定时抛出（信息面向使用者）。"""


# 设计 JSON 允许的字段（未知字段会提示，防止用户拼错后以为生效）
_DESIGN_SCHEMA = {
    "product_name": str,
    "pcb": {"trace_width_mm": float, "required_width_mm": float},
    "structure": {"beam_stress_mpa": float, "beam_limit_mpa": float},
    "discharge": {"soc": float, "voltage_v": float, "temp_c": float, "status": str},
    "safety": {"thermal_margin_c": float, "thermal_limit_c": float, "ip_rating": str},
    "inspection": {"item": str, "fpy_30d": float},
}


def _coerce(section: dict, spec: dict, where: str) -> dict:
    """按 spec 校验并类型转换一个分节，返回转换后的 dict。"""
    unknown = set(section) - set(spec)
    if unknown:
        raise UploadError(f"{where}: 未知字段 {sorted(unknown)}（可选字段: {sorted(spec)}）")
    out = {}
    for key, typ in spec.items():
        if key not in section:
            continue
        val = section[key]
        try:
            out[key] = typ(val) if typ is not str else str(val)
        except (TypeError, ValueError):
            raise UploadError(f"{where}.{key}: 期望 {typ.__name__} 类型, 实际为 {val!r}") from None
    return out


def load_design_json(path: Path) -> dict:
    """读取设计状态 JSON，返回按 schema 校验后的 dict。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise UploadError(f"{path.name}: JSON 解析失败（第 {e.lineno} 行: {e.msg}）") from None
    if not isinstance(data, dict):
        raise UploadError(f"{path.name}: 顶层必须是 JSON 对象")

    unknown = set(data) - set(_DESIGN_SCHEMA)
    if unknown:
        raise UploadError(
            f"{path.name}: 未知分节 {sorted(unknown)}（可选分节: {sorted(_DESIGN_SCHEMA)}）")

    out: dict[str, Any] = {}
    for key, spec in _DESIGN_SCHEMA.items():
        if key not in data:
            continue
        if spec is str:
            out[key] = str(data[key])
        else:
            if not isinstance(data[key], dict):
                raise UploadError(f"{path.name}: 分节 {key} 必须是对象")
            out[key] = _coerce(data[key], spec, key)
    if not out:
        raise UploadError(f"{path.name}: 未包含任何有效分节")
    return out


def load_sensor_csv(path: Path) -> dict:
    """读取监测数据 CSV，返回真实统计（行数/空值/列名/数值列范围）。"""
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except UnicodeDecodeError:
        raise UploadError(f"{path.name}: 编码不是 UTF-8，请转换后重传") from None
    if not rows:
        raise UploadError(f"{path.name}: 没有数据行（至少需要表头 + 1 行）")

    columns = list(rows[0].keys())
    nulls = sum(1 for r in rows for v in r.values() if v is None or str(v).strip() == "")

    # 数值列的范围统计（如 temp_c 的最大值，供安全审计引用真实数据）
    ranges: dict[str, dict[str, float]] = {}
    for col in columns:
        vals = []
        for r in rows:
            try:
                vals.append(float(r[col]))
            except (TypeError, ValueError):
                continue
        if len(vals) >= len(rows) * 0.8:      # 80% 以上可解析才视为数值列
            ranges[col] = {"min": min(vals), "max": max(vals)}
    return {"source": path.name, "rows": len(rows), "nulls": nulls,
            "columns": columns, "ranges": ranges}


def load_uploads(paths: list[str], world) -> list[str]:
    """把若干上传文件装载进 world，返回逐条加载说明（供 CLI 打印）。"""
    notes: list[str] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            raise UploadError(f"文件不存在: {raw}")
        suffix = path.suffix.lower()
        if suffix == ".json":
            design = load_design_json(path)
            _apply_design(world, design)
            notes.append(f"设计状态已载入 {path.name}: {', '.join(sorted(design))} 分节")
        elif suffix == ".csv":
            sensor = load_sensor_csv(path)
            world.sensor = sensor
            notes.append(
                f"监测数据已载入 {path.name}: 实际 {sensor['rows']} 行, "
                f"空值 {sensor['nulls']} 个, 列 {sensor['columns']}")
        else:
            raise UploadError(f"{path.name}: 不支持的格式（仅支持 .json 设计状态 / .csv 监测数据）")
    return notes


def _apply_design(world, design: dict) -> None:
    """把校验后的设计状态覆盖到 EngineeringWorld（未提供的字段保持默认值）。"""
    if "product_name" in design:
        world.product_name = design["product_name"]
    if "pcb" in design:
        world.trace_width_mm = design["pcb"].get("trace_width_mm", world.trace_width_mm)
        world.required_width_mm = design["pcb"].get("required_width_mm", world.required_width_mm)
    if "structure" in design:
        world.beam_stress_mpa = design["structure"].get("beam_stress_mpa", world.beam_stress_mpa)
        world.beam_limit_mpa = design["structure"].get("beam_limit_mpa", world.beam_limit_mpa)
    if "discharge" in design:
        world.discharge.update(design["discharge"])
    if "safety" in design:
        world.safety.update(design["safety"])
    if "inspection" in design:
        world.inspection.update(design["inspection"])
