"""声明式子代理加载器（Pattern 4 的落地 + "Task to Agent" 路由的依据）.

参考项目: sst/opencode 的 .opencode/agents/*.md 机制 ——
一个 markdown 文件定义一个专业代理: YAML frontmatter 声明名称/可用工具/
权限覆盖, 正文是该代理的系统提示词.

对应架构图: 底部一排专业 Angineer (Structure Design / Chip Design /
PCB CAM / Safety / Inspection / Discharge Monitoring) —— 每个代理
一个文件, 工具与权限按专业收敛.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class AgentSpec:
    name: str
    display: str
    description: str
    tools: list[str]
    permission_overrides: dict = field(default_factory=dict)
    prompt: str = ""


_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def load_agent_file(path: Path) -> AgentSpec:
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER.match(text)
    if not m:
        raise ValueError(f"{path} 缺少 YAML frontmatter")
    meta, body = yaml.safe_load(m.group(1)), m.group(2).strip()
    return AgentSpec(
        name=meta["name"],
        display=meta.get("display", meta["name"]),
        description=meta.get("description", ""),
        tools=list(meta.get("tools", [])),
        permission_overrides=dict(meta.get("permission", {})),
        prompt=body,
    )


def load_agents(directory: str | Path) -> dict[str, AgentSpec]:
    agents: dict[str, AgentSpec] = {}
    for p in sorted(Path(directory).glob("*.md")):
        spec = load_agent_file(p)
        agents[spec.name] = spec
    return agents
