#!/usr/bin/env python3
"""Angineer Agent Prototype — CLI 入口.

用法(--task 必填, 无内置任务):
  python main.py --task "..."                    # 离线运行, ask 权限逐条人工确认
  python main.py --task "..." --auto-approve     # 离线运行, 自动同意所有 ask
  python main.py --task "..." --real             # 使用真实大模型(需 OPENAI_API_KEY)
  python main.py --task "..." --upload 设计.json 监测.csv   # 上传真实数据初始化工程环境
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.chdir(Path(__file__).parent)          # 保证 output/ 始终落在项目根目录
sys.path.insert(0, str(Path(__file__).parent))

from angineer.agents_loader import load_agents
from angineer.graph import AngineerApp
from angineer.llm import get_llm
from angineer.tools import EngineeringWorld, ToolRegistry

C = {"cyan": "\033[96m", "green": "\033[92m", "yellow": "\033[93m",
     "red": "\033[91m", "magenta": "\033[95m", "dim": "\033[2m", "reset": "\033[0m"}


def colored_log(msg: str) -> None:
    for tag, color in [("[Task Decomposition]", "cyan"), ("[Actions]", "green"),
                       ("[Engineer Tools]", "yellow"), ("[Debug and Refine]", "red"),
                       ("[Reflection]", "red"), ("[Multi-Agent]", "magenta"),
                       ("[Decisions]", "cyan"), ("[HITL]", "magenta")]:
        if tag in msg:
            print(f"{C.get(color, '')}{msg}{C['reset']}")
            return
    print(f"{C['dim']}{msg}{C['reset']}" if msg.startswith(" ") else msg)


def main() -> None:
    ap = argparse.ArgumentParser(description="Angineer Agent Prototype")
    ap.add_argument("--task", required=True,
                    help="任务描述(必填): 规划器据此分解执行步骤, 如 '设计智能电表: "
                         "完成结构校核与 PCB 载流检查, 生成工程报告'")
    ap.add_argument("--auto-approve", action="store_true", help="自动同意所有 ask 权限请求")
    ap.add_argument("--real", action="store_true", help="强制使用真实大模型")
    ap.add_argument("--agents-dir", default=str(Path(__file__).parent / "agents"))
    ap.add_argument("--upload", nargs="+", metavar="文件",
                    help="上传数据文件初始化工程环境: .json=设计状态, .csv=监测数据(可多个)")
    args = ap.parse_args()

    def ask_fn(tool_name: str, tool_args: dict) -> bool:
        if args.auto_approve:
            colored_log(f"    [HITL] {tool_name} 需要确认 -> 自动批准(--auto-approve)")
            return True
        ans = input(f"    [HITL] 代理请求执行 {tool_name} {tool_args}, 是否批准? [y/N] ")
        return ans.strip().lower() == "y"

    llm = get_llm(force="real" if args.real else None)
    agents = load_agents(args.agents_dir)

    world = EngineeringWorld()
    upload_notes: list[str] = []
    if args.upload:                      # 用户上传真实数据 -> 覆盖内置初始状态
        from angineer.upload import UploadError, load_uploads
        try:
            upload_notes = load_uploads(args.upload, world)
        except UploadError as e:
            print(f"[upload] 加载失败: {e}")
            sys.exit(2)
    registry = ToolRegistry(world)
    app = AngineerApp(llm, registry, agents, ask_fn, log_fn=colored_log)

    print("=" * 72)
    print(" Angineer Agent Prototype | Plan-and-Execute + Tool Use + Reflection")
    print(f" LLM: {llm.name} | 专业代理: {len(agents)} 个 | 注册工具: {len(registry.list_tools())} 个")
    print("=" * 72)
    for note in upload_notes:
        colored_log(f"[upload] {note}")
    print(f"产品/项目: {world.product_name}" + ("（用户上传数据）" if upload_notes else "（内置模拟数据）"))
    print(f"任务: {args.task}")

    state = app.run(args.task)

    print("\n" + "=" * 72)
    print(" 执行汇总")
    print("=" * 72)
    for i, r in enumerate(state["results"], 1):
        mark = "✓" if r["ok"] else "✗"
        print(f" {mark} 步骤{i} [{r['agent']}] {r['summary']}")
    if state["lessons"]:
        print("\n 反思沉淀的教训 (Debug and Refine):")
        for l in state["lessons"]:
            print(f"  - {l}")
    # 把执行结果回填进报告, 让 Writing Report 的产物是真实汇总
    report_path = Path("output/engineering_report.md")
    if report_path.exists():
        with report_path.open("a", encoding="utf-8") as f:
            f.write("\n## 各专业 Angineer 执行结果\n\n")
            for i, r in enumerate(state["results"], 1):
                f.write(f"{i}. **{r['agent']}** — {'通过' if r['ok'] else '未通过'}: {r['summary']}\n")
            if state["lessons"]:
                f.write("\n## Debug and Refine 教训记录\n\n")
                for l in state["lessons"]:
                    f.write(f"- {l}\n")

    registry.dump_log_jsonl("output/tool_calls.jsonl")
    print(f"\n 工具调用日志: output/tool_calls.jsonl ({len(registry.call_log)} 条)")
    print(f" 工程报告:     output/engineering_report.md")


if __name__ == "__main__":
    main()
