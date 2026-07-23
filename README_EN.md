# Angineer Agent Prototype

[中文文档](README.md) | English

![Angineer Large Model (Open Source) architecture diagram](docs/angineer-architecture.jpg)

An engineering-domain multi-agent prototype implementing the "Angineer Large Model (Open Source)"
architecture diagram, with **all 5 agentic design patterns fully implemented**:

| Pattern | Where it lands in the prototype |
|---|---|
| P1 Reflection (self-correction) | generator-critic critique loop + periodic self-check + lesson persistence |
| P2 Plan and Solve | LangGraph Planner → Executor → Replanner main loop |
| P3 Tool Use | MCP-style controlled tool layer (schema declaration + permission wrapper + audit log) |
| P4 Multi-Agent Collaboration | 6 declarative specialist Angineers + `team.ask` inter-agent consultation |
| P5 Human-in-the-Loop | allow/ask/deny permission engine + high-risk blocking + final acceptance gate |

> **Verification status** (2026-07-20): offline Mock mode passes all 8 tests (five-pattern
> verification chain, interactive permissions, keyword fallback, no infinite loop under fault
> injection); real-model mode verified end-to-end with **Kimi k3** on **two different tasks**
> (outdoor energy-storage power supply / smart meter).
> See `docs/technical-selection_EN.md` Section 1 and Appendix.

## Quick start

```bash
pip install -r requirements.txt

# `--task` is REQUIRED (no built-in task; every run must state its goal).
# The examples below share one task (matching the sample data in examples/):
TASK="Design a smart meter: run structural strength check and PCB current-carrying CAM check, prepare data and run safety audit, read discharge monitoring data, then generate the engineering report and apply for change approval"

# Offline run (no API Key needed, reproducible results)
python main.py --task "$TASK" --auto-approve

# Interactive mode: ask-permission prompts and acceptance gate ask one by one (HITL)
python main.py --task "$TASK"

# Upload real data (semi-real mode: environment state comes from your files; judging logic unchanged)
python main.py --task "$TASK" --upload examples/design_smart_meter.json examples/thermal_monitoring.csv

# Plug in a real LLM (any OpenAI-compatible endpoint, incl. vLLM/Ollama open-source bases)
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.moonshot.cn/v1   # adjust to your platform
export ANGINEER_MODEL=your-model-name
python main.py --task "$TASK" --real
```

**Windows cmd users**: `TASK=...` / `$TASK` above is Linux bash syntax, which cmd does not support.
Use `set` (no spaces around `=`, value unquoted) or inline the task text directly:

```bat
:: cmd variable style (quote when referencing: %TASK%)
set TASK=Design a smart meter: run structural strength check and PCB current-carrying CAM check, prepare data and run safety audit, read discharge monitoring data, then generate the engineering report and apply for change approval
python main.py --task "%TASK%" --auto-approve

:: Or inline directly (recommended)
python main.py --task "Design a smart meter: run structural strength check and PCB current-carrying CAM check, prepare data and run safety audit, read discharge monitoring data, then generate the engineering report and apply for change approval" --upload examples/design_smart_meter.json examples/thermal_monitoring.csv --auto-approve
```

### Uploading your own data (--upload)

By default the run uses built-in simulated data; `--upload` replaces the environment's initial
state with your real data:

| File type | Contents | Examples |
|---|---|---|
| `.json` design state | product name / PCB trace width & requirement / structural stress / discharge parameters / safety margins / inspection FPY | `examples/design_smart_meter.json` (CAM first-check fails → full closed loop), `examples/design_energy_storage_pass.json` (first check passes → flow shortens naturally) |
| `.csv` monitoring data | sensor time series; real row/null/value-range statistics | `examples/thermal_monitoring.csv` (200 rows, 8 nulls) |

After upload, every tool's input comes from the files: CAM judges by the uploaded trace width,
the safety audit computes remaining thermal headroom from the uploaded margin, and
`data_transform` reports the CSV's real row count. Malformed fields produce precise errors
(unknown section / wrong type / JSON syntax) — never silently ignored.

## Anatomy of one full run (all five patterns in place)

Using the "outdoor energy-storage power supply design" task as an example:

1. **P2 Planning**: the Planner decomposes the task into 5 steps assigned to 5 specialist Angineers;
2. **P3 Tools**: every tool call passes through the controlled pipeline
   "schema validation → permission → execution → logging";
3. **P1 Reflection**: CAM check fails on 0.20mm trace width → a lesson is recorded; the chief
   engineer's conclusion is self-critiqued ("lacks structured summary and risk grading") →
   revised before finalizing;
4. **P4 Collaboration**: before tuning parameters, the PCB CAM agent consults the Safety agent
   via `team.ask` about the thermal impact of widening the trace, and only proceeds after a
   professional answer;
5. **P5 Human-in-the-Loop**: parameter changes / report writes are confirmed one by one,
   `production.apply` is blocked by `deny`, and the run ends only after manual confirmation at
   the **acceptance gate**;
6. Safety net: at most 8 iterations per step, 2 replans, 2 critique rounds per step
   (Space Control) — fault-injection tests confirm no infinite loops.

## Is it limited to the "energy-storage power supply" task? — Task generality

**No.** Task dependence comes in three layers:

1. **Framework layer (task-agnostic)**: the plan-execute-replan loop, critique loop, `team.ask`
   consultation, permission engine and acceptance gate work for any task; `--task` accepts
   arbitrary text;
2. **Agent & tool layer (domain-specific but declaratively swappable)**: the 6 agents target the
   engineering design domain. Switching domains = swapping `agents/*.md` (one md file = one
   agent) and tool handlers, with zero changes to the main loop — a deliberate design
   corresponding to the "Etc…" slot on the architecture diagram's bottom bus. Initial tool data
   can be either built-in simulated values or user-uploaded real data via `--upload`
   (see "Uploading your own data");
3. **Mock layer (offline rules)**: the offline script follows a preset chain (keyword routing +
   canned data); it runs with other tasks but the data won't match their semantics. For
   meaningful runs use `--real` with a real model, which plans and executes autonomously on any
   engineering task.

**Verified** (2026-07-20, task "Design a smart meter"): Mock mode automatically routed a new
agent combination (chip simulation → CAM check → inspection records → chief); the k3 real model
autonomously decomposed 5 steps (including a safety-audit step it added on its own initiative),
all passed, and generated change request CR-2024-SM-001.
See `docs/technical-selection_EN.md` Appendix Round 4.

## Architecture diagram module → code mapping

| Diagram module | Code location | Pattern |
|---|---|---|
| Task Decomposition | `angineer/graph.py` planner_node | P2 |
| Actions / bottom specialist-Angineer bus | `angineer/graph.py` executor_node + `agents/*.md` | P2 + P4 |
| Decisions | replanner conditional edges (advance / replan / finish) | P2 |
| Space Control | `MAX_STEP_ITERS=8` / `MAX_REPLANS=2` / `MAX_CRITIQUES=2` | runaway prevention |
| Engineer Tools | `angineer/tools.py` controlled invocation layer | P3 |
| Engineer Knowledge Graph | `kg_query` tool (read-only) | P3 |
| Data Preparation and Transform | `data_transform` tool | P3 |
| Optimization | `optimization.set_param` tool (ask permission) | P3 + P5 |
| Debug and Refine | `reflection.py` + generator-critic loop + replanner_node | P1 |
| (inter-agent collaboration, implicit in the bus) | `team.ask` meta-tool + `graph.py` `_consult()` | P4 |
| Writing Report / Apply Results | `report.write` / `production.apply` (deny) | P3 + P5 |
| (acceptance, runs through the flow) | permission engine + acceptance gate `final_acceptance` | P5 |
| LLM | `angineer/llm.py` (Mock / OpenAI-compatible, interchangeable) | — |

> **Bottom green bus correspondence**: Structure Design / Chip Design / PCB CAM / Safety /
> Discharge Monitoring each have their own agent file; the **Inspection Angineer**'s duty lands
> on the `inspection.query` tool (held by the Discharge Monitoring agent); **Industry Angineer
> and "Etc…"** are not yet implemented — dropping one more md file into `agents/` completes the
> extension without touching the main loop.

## Directory layout

```
angineer_agent_prototype/
├── main.py                  # CLI entry (--task / --auto-approve / --real / --upload)
├── agents/                  # opencode style: one md file = one specialist Angineer
│   ├── structure_design.md  #   YAML frontmatter declares: name/description/tools/permissions
│   ├── chip_design.md       #   every agent carries team.ask (inter-agent consultation)
│   ├── pcb_cam.md
│   ├── safety.md
│   ├── discharge_monitoring.md
│   └── chief.md             # chief engineer: summarize, write report, initiate change request
├── angineer/
│   ├── graph.py             # LangGraph main graph (P2) + critique loop (P1) + _consult (P4) + gate (P5)
│   ├── tools.py             # MCP-style tool registry + controlled invocation layer (P3 core)
│   ├── permissions.py       # allow/ask/deny permission engine, last-match-wins wildcards (P5)
│   ├── reflection.py        # periodic self-check + lesson persistence (P1 runtime introspection)
│   ├── upload.py            # --upload data intake: design JSON / monitoring CSV → EngineeringWorld
│   ├── agents_loader.py     # declarative agent loader
│   └── llm.py               # LLM abstraction: decompose / act / critique
├── examples/                # --upload sample data (2 design JSON + 1 monitoring CSV)
└── docs/technical-selection_CN.md      # five-pattern design logic, selection rationale, evidence, roadmap (中文)
    docs/technical-selection_EN.md   # English version of the above
```

## Run artifacts

| File | Contents |
|---|---|
| `output/engineering_report.md` | automatically aggregated specialist results and lesson records |
| `output/tool_calls.jsonl` | full tool-call audit trail (tool/args/status/timestamp) |

## Referenced open-source projects (by pattern)

- **P1**: Reflexion (NeurIPS'23) · Self-Refine (NeurIPS'23) · CRITIC (ICLR'24) · LangGraph Reflection tutorial
- **P2**: Plan-and-Solve-Prompting (ACL'23) · LangGraph Plan-and-Execute · LLMCompiler (ICML'24)
- **P3**: MCP servers · ToolBench (ICLR'24) · gorilla/BFCL
- **P4**: CrewAI · Microsoft AutoGen → Agent Framework · MetaGPT
- **P5**: LangGraph interrupt/HITL · CopilotKit · AG-UI protocol · sst/opencode (permission model)
- Production-grade references: hermes-agent (reflection kernel) · OpenClaw (scheduled orchestration, roadmap)

Selection rationale and comparison: `docs/technical-selection_EN.md`.
