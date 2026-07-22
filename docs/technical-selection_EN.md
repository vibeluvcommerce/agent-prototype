# Technical Selection Rationale — Angineer Agent Prototype

[中文文档](technical-selection_CN.md) | English

This document answers two questions: **why each layer of the prototype is designed the way
it is** (design logic), and **why each technology/architecture was chosen** (selection
rationale). Scope: all 5 agentic design patterns (P1 Reflection / P2 Plan and Solve /
P3 Tool Use / P4 Multi-Agent Collaboration / P5 Human-in-the-Loop).

---

## 1. Project positioning and verification status

The prototype implements the "Angineer Large Model (Open Source)" architecture diagram as a
**runnable closed loop**: every functional block on the diagram lands in real code, and an
engineering task (outdoor energy-storage power supply design) runs through the full flow of
"decompose → execute → fail → reflect → consult → tune → re-check → critique-revise →
report → high-risk block → acceptance", with all five design patterns genuinely triggered
in a single run.

**Verification status (all runs executed on 2026-07-20)**:

| # | Test | Patterns | Result |
|---|---|---|---|
| 1 | Energy-storage example task full flow (5-step dispatch, tool-call audit, report persisted) | P2/P3 | Pass |
| 2 | Real closed loop: CAM fail → lesson → consult → tune → re-check pass | P1/P3/P4/P5 | Pass |
| 3 | Chief conclusion rejected by self-critique → revised with feedback → approved | P1 | Pass |
| 4 | team.ask inter-agent consultation (PCB CAM asks Safety about thermal impact) | P4 | Pass |
| 5 | Interactive HITL: approve/reject behave as expected + acceptance gate | P5 | Pass |
| 6 | Keyword-routing variants and no-match fallback | P2 | Pass |
| 7 | Fault injection: circuit breaker + at most 2 replans then clean termination, no infinite loop | P2/Space Control | Pass |
| 8 | Permission tiers: read-only allow, write ask, `production.*` blocked | P5 | Pass |
| 9 | Real model (Kimi k3) end-to-end five-pattern verification | All | Pass (see Appendix) |
| 10 | New-task generality (smart meter: Mock re-routing + k3 autonomous 5-step plan, all pass) | All | Pass (see Appendix Round 4) |

---

## 2. Overall design logic: why five layers

```
User task (CLI)
   │
Main loop     graph.py       LangGraph state graph: Planner → Executor → Replanner (P2)
   │                         embedded: generator-critic loop (P1) / _consult (P4) / gate (P5)
Agent layer   agents/*.md    6 declarative specialist Angineers (prompt + tools + permissions) (P4)
   │
Tool layer    tools.py       controlled invocation: schema check → permission → execute → log (P3)
   │          permissions.py allow/ask/deny permission engine (P5)
Environment   EngineeringWorld  simulated engineering state (real dependencies between tools)
   ▼
Artifacts: output/engineering_report.md + output/tool_calls.jsonl
```

The layering follows a single principle: **keep "decision", "execution", and "permission"
independently replaceable**.

- Decision (which model, how to plan, how to review) lives in the LLM abstraction and main
  loop — swap models without touching business logic;
- Execution (what each specialist can do) lives in declarative agent files — adding a new
  specialist Angineer means adding one md file, no code changes;
- Permission (who may do what) lives in the tool layer — the model can never reach an
  unauthorized capability, i.e. "permissions converge at the tool level, not entrusted to
  the model".

Tools share state through `EngineeringWorld` (e.g. `set_param` changing the trace width
changes the next `cam_check` result), so "check → optimize → re-check" is reproducible
logic driven by real state dependencies, not a hardcoded script — the bottom line for the
prototype to count as "architectural evidence".

---

## 3. Pattern 2 (Plan and Solve): why LangGraph as the primary choice

### 3.1 Selection

**Primary: the Plan-and-Execute paradigm of `langchain-ai/langgraph` (37,486 stars)**;
ideas borrowed from `SqueezeAILab/LLMCompiler` (ICML 2024); principle credited to
`AGI-Edgerunners/Plan-and-Solve-Prompting` (ACL 2023, origin of the pattern name).

### 3.2 Design logic: how the main loop lands the pattern definition

| Pattern definition | Prototype implementation |
|---|---|
| Planning layer produces a task sequence | `planner_node`: `llm.decompose()` produces an explicit `[{agent, instruction}]` step list |
| Execution layer runs steps in order | `executor_node`: each step routed to its specialist agent, driving a ReAct mini-loop |
| Replanning checkpoint | `replanner_node`: after each step — if failed and under limits, insert a corrective step carrying the lesson |
| Task to Agent (dispatch) | the `agent` field in each step is the routing key, matching the bottom specialist-Angineer bus |

**The plan is an explicit data structure** — deliberately borrowed from LLMCompiler: plans
can be inspected, modified, and re-queued instead of hiding inside model context. When the
sequential plan is later upgraded to a DAG parallel plan, only the planner's output
structure needs to change (LLMCompiler is officially adopted by LangGraph, so the
evolution path is ready-made).

### 3.3 Space Control: the engineering necessity pattern papers don't mention

Plan-and-Execute has two classic failure modes: spinning inside one step, and plan-level
infinite loops. The prototype sets three hard constraints (verified by fault injection):

- `MAX_STEP_ITERS = 8`: per-step tool-call limit (initially 6; real-model runs showed
  early schema trial-and-error consumes budget, so it was raised to 8 — the circuit
  breaker itself remains);
- `MAX_REPLANS = 2`: plan-level replan limit for the whole run;
- `MAX_CRITIQUES = 2`: per-step self-critique round limit (see Section 5).

### 3.4 Why not the alternatives

| Candidate | Reason for exclusion |
|---|---|
| microsoft/TaskWeaver (6,174 stars) | Execution side is a single code interpreter; cannot express the "multi-specialist Angineer bus" dispatch structure; oriented to data analysis |
| OpenBMB/XAgent (8,525 stars) | End-to-end autonomous agent product; dual loop hardcoded internally, hard to reshape to a custom architecture diagram |
| Self-built planning loop | State persistence, conditional routing, interrupt/resume all rebuilt from scratch; LangGraph is already the de-facto industry standard |
| Pure Plan-and-Solve prompting | Just a single-prompt technique; cannot express multi-agent dispatch and state machines; cited for principle only |

---

## 4. Pattern 3 (Tool Use): why an MCP-style controlled invocation layer

### 4.1 Selection

**Primary: a minimal controlled invocation layer self-implemented after
`modelcontextprotocol/servers` (88,559 stars)**; evaluation companions
`ShishirPatil/gorilla` (BFCL, 12,949 stars) and `OpenBMB/ToolBench` (ICLR'24 spotlight,
5,699 stars).

### 4.2 Design logic: how the invocation pipeline lands the pattern definition

| Five elements of a tool layer | Prototype implementation |
|---|---|
| Tool schema | each tool declares name / description / parameters (JSON Schema) / handler; `list_tools()` is equivalent to MCP `tools/list` |
| Execution logic | handlers are isolated from the model; the model only sees schemas |
| Message handling | structured `{status, summary, data}` results written to the agent scratchpad |
| Error handling | missing args, unknown tools, and denials all return structured errors for agent recovery (the k3 model self-corrected via these many times in real runs) |
| State management | `EngineeringWorld` carries cross-tool state; `tool_calls.jsonl` keeps a full audit trail |

The permission check sits between validation and execution, forming the full pipeline:
**schema validation → permission match → execution → logging → reflection**.

### 4.3 Why not simply adopt the official MCP SDK

1. **Readability**: 40 lines of code let you audit line by line "what MCP actually
   specifies in the protocol"; an SDK would hide all of that inside a framework;
2. **Zero replacement cost**: the interface shape matches MCP, so in the engineering phase,
   when CAD/EDA/CAM are each packaged as standalone MCP servers, neither the agent layer
   nor the main loop needs to change.

### 4.4 Tool-calling capability of open-source bases: evaluation and enhancement (roadmap)

- **Evaluation selection**: use gorilla's Berkeley Function Calling Leaderboard (V4,
  released for real agentic scenarios) to evaluate candidate base models;
- **Data augmentation**: when a base model is too weak, fine-tune for the domain with
  ToolBench's 16k+ real API data.

---

## 5. Pattern 1 (Reflection): why a "tool-grounded" self-critique loop

### 5.1 Selection

**Structural primary: the generator-critic two-node loop from LangGraph's official Basic
Reflection tutorial** (isomorphic with the existing stack); academic anchors:
`noahshinn/reflexion` (NeurIPS 2023, 3,208 stars, origin of the pattern name) and
`madaan/self-refine` (NeurIPS 2023, 812 stars); feedback-route anchor: `CRITIC`
(ICLR 2024, Microsoft, tool-interactive critiquing).

### 5.2 Design logic: mapped item-by-item to the Reflexion trio

| Reflexion component | Prototype implementation |
|---|---|
| Actor | the Executor's ReAct loop |
| Episodic memory buffer (reflection text persisted) | `ReflectionModule` lessons: failures / schema errors / permission denials all precipitate into structured lessons, used by replanning (7 took effect in k3 real runs) |
| Evaluator + revision loop | **generator-critic loop**: an agent's `final` is not accepted directly; `llm.critique()` first reviews it against the task goal — `revise` returns it with feedback for another pass, only `approve` releases it |

Termination follows the tutorial and ties into existing mechanisms: at most
`MAX_CRITIQUES=2` rounds per step (Self-Refine's practical experience is convergence
within 1-2 rounds); critiques don't consume the tool-call budget; a failed review parse
defaults to approve — self-critique must never run away with the flow.

### 5.3 Why "tool-grounded" rather than "armchair" reflection

The academic consensus (the CRITIC paper and follow-ups) is: **an LLM cannot reliably
self-correct without external feedback** — pure-text self-critique tends toward
self-deception. The prototype therefore anchors all criticism in real tool results:
a `fail` returned by `cam_check` is a deterministic engineering fact; lessons originate
from it, and critique revolves around it — exactly CRITIC's tool-interactive critiquing
route, and the core argument for "why our reflection sits after the tool layer".

### 5.4 Why not the alternatives

- **LATS** (ICML 2024, 845 stars): the strongest form (reflection + Monte-Carlo tree
  search), but the search-tree complexity far exceeds what the prototype stage needs;
  listed as a frontier outlook;
- **hermes-agent kernel learning loop**: partially borrowed (periodic self-check + lesson
  persistence comes from it); its RL training loop (Atropos) belongs to model training,
  beyond the scope of an architecture prototype.

---

## 6. Pattern 4 (Multi-Agent Collaboration): why "orchestrated dispatch + consultation tool"

### 6.1 Selection

**Structural primary: declarative specialist agents (opencode style) + the `team.ask`
consultation meta-tool (self-implemented)**; references: `crewAIInc/crewAI` (53,499 stars,
role-divided teams), `microsoft/autogen` (~55,000 stars, in maintenance mode since Feb
2026, officially succeeded by Microsoft Agent Framework, supporting GroupChat / Handoff /
Magentic multi-agent orchestration), `geekan/MetaGPT` (software-company role SOPs),
`camel-ai/camel` (academic research oriented).

### 6.2 Design logic: the architecture diagram determines the collaboration shape

The bottom of the diagram is a "bus of specialist Angineers + central core", not a
peer-to-peer network — so collaboration lands in two layers:

1. **Division layer (pre-existing)**: one md file defines one specialist Angineer, each
   with its own prompt, authorized tool set, and permissions — corresponding to CrewAI's
   "role + goal + tools" triple, but as declarative files rather than code classes, with
   granularity matching the diagram's bus;
2. **Consultation layer (newly added)**: the `team.ask` meta-tool — any agent mid-task can
   consult another agent by name (e.g. PCB CAM asks Safety "thermal impact of widening the
   trace"); the consulted party answers using its own tool set (at most 3 steps) and the
   result returns to the asker. Nested consultation is forbidden (loop prevention), and
   consultation permissions go through the unified permission engine.

This aligns with AutoGen's "async messages + tool-closure" design principle: **heavy
operations must be wrapped as observable, interruptible tool calls** — `team.ask` is fully
traced in logs (`[Multi-Agent]`), and the consulted party's tool calls pass through the
same controlled pipeline.

### 6.3 Why not adopt a full peer-collaboration framework

- CrewAI/AutoGen are complete frameworks; adopting either would replace the LangGraph main
  loop we built per the architecture diagram (P2's landing point) — patterns should stack,
  not overwrite each other;
- MetaGPT's SOP role scripts fit software development pipelines, not the engineering
  design domain;
- The trio "orchestrator dispatch + consultation tool + context injection" already covers
  the collaboration semantics expressed by the diagram, and every part stays simple and
  line-by-line auditable.

---

## 7. Pattern 5 (Human-in-the-Loop): why the twin gates of "permission engine + acceptance gate"

### 7.1 Selection

**Primary: `sst/opencode`'s (tool-level) permission model + a LangGraph-interrupt-style
flow-level pause point (acceptance gate, self-implemented)**; references:
`CopilotKit/CopilotKit` (36,167 stars, HITL frontend collaboration framework) and
`ag-ui-protocol/ag-ui` (14,809 stars, Agent-User Interaction protocol with HITL as a core
feature).

### 7.2 Design logic: human intervention at two granularities

| Granularity | Prototype implementation | Reference |
|---|---|---|
| Tool level | `allow/ask/deny` tri-state + wildcards + last-match-wins + agent-level overrides; read-only auto-allowed, writes confirmed one by one, `production.*` denied by default | opencode permission model; the article's "permissions converge at the tool level" |
| Flow level | **acceptance gate** after all steps complete (`final_acceptance`): pauses the flow for manual "accept / reject" confirmation, matching LangGraph's interrupt and AG-UI's "shared state + approval" semantics | LangGraph HITL, AG-UI |

Ideal behavior observed in k3 real runs: knowing `production.apply` was denied, the model
wrapped up with professional change-request wording instead of retrying blindly —
**model judgment and permission policy forming a double safeguard**, direct evidence that
the HITL design works.

### 7.3 Permissions and agent definitions: why opencode is the primary reference

Among three popular open-source agents (OpenClaw / opencode / hermes-agent), opencode maps
cleanest onto this architecture diagram: plan/build dual agents ≈ Task Decomposition →
execution; declarative sub-agents ≈ the bottom specialist-Angineer bus; allow/ask/deny ≈
a hard requirement for engineering safety scenarios. From hermes-agent we take only the
reflection module (periodic self-check + lesson persistence); from OpenClaw only the
heartbeat/cron scheduling idea (roadmap step 5).

---

## 8. LLM abstraction layer: why Mock and real models are interchangeable

`llm.py` defines three unified interfaces: `decompose` (planning), `act` (per-step
decision), `critique` (self-review), with two implementations:

- **MockLLM**: offline rule-driven, deterministic output. Value — offline/CI
  reproducibility; architecture verification independent of external services;
  "model capability" decoupled from "architecture correctness".
- **OpenAILLM**: any OpenAI-compatible endpoint (`OPENAI_API_KEY` / `OPENAI_BASE_URL` /
  `ANGINEER_MODEL`), including vLLM/Ollama open-source bases; temperature is not sent by
  default (some models such as kimi k3 reject custom values), overridable via
  `ANGINEER_TEMPERATURE`.

This layer doubles as the future **evaluation socket**: run the same task set on different
base models and score plan reasonableness, tool-call success rate, and critique-loop
convergence — a domain adaptation of the BFCL methodology.

---

## 9. Known boundaries (honest disclosure)

1. MockLLM's agent policies are scripted — they prove the architecture and flow correct,
   but say nothing about a real model's planning / tool selection / self-evaluation
   capability;
2. Tools are simulated implementations (no real CAD/EDA/CAM integration), but inter-tool
   state dependencies are real; `--upload` is supported for initializing environment
   state from user-uploaded design JSON / monitoring CSV (semi-real mode: real input
   data, real judging logic, simulated execution engines) — swapping handlers for real
   engineering software calls makes it fully real (roadmap step 2);
3. Plans execute sequentially; DAG parallelism (LLMCompiler idea) is on the roadmap;
4. The knowledge graph is a built-in dictionary simulation; engineering should switch to
   a real graph store such as Neo4j;
5. Simulated tools always return static success for identical/similar calls — a real
   model lacking a "done" signal may over-iterate; Space Control catches this by design
   and it disappears naturally once real tools are connected;
6. Inter-agent collaboration is currently one-to-one consultation (team.ask);
   meeting-style collaboration (GroupChat/Magentic shapes) is on the roadmap;
7. The diagram's **Inspection Angineer has no standalone agent** — its duty lands on the
   `inspection.query` tool, held by the Discharge Monitoring agent; **Industry Angineer
   and "Etc…" are not implemented**. This is deliberate whitespace: declarative
   extension (one more md file in `agents/`) fills them without touching the main loop;
8. The framework layer (main loop / critique loop / consultation / permissions /
   acceptance gate) is task-agnostic, but the Mock layer's offline script follows a
   preset chain (keyword routing + canned data) — for new-task verification use `--real`
   with a real model (verified on the smart-meter task, see Appendix Round 4).

## 10. Engineering roadmap

| Stage | Action |
|---|---|
| 1 | Connect open-source bases via `--real` (vLLM/Ollama), evaluate tool-calling with BFCL |
| 2 | Replace the tool layer with the official MCP SDK; package CAD/EDA/CAM as servers |
| 3 | Upgrade sequential plans to DAG parallel plans (LLMCompiler idea) |
| 4 | Swap kg_query for a real engineering knowledge graph (Neo4j) |
| 5 | Introduce cron/heartbeat for scheduled discharge-monitoring patrols (borrow OpenClaw) |
| 6 | Upgrade team.ask to multi-agent meetings (cf. Microsoft Agent Framework GroupChat/Magentic) |
| 7 | Fine-tune a domain model with ToolBench data when base tool-calling falls short |

---

## 11. Reference project list

Stars captured 2026-07 (some from public reports).

| Pattern | Project | Purpose | Stars |
|---|---|---|---|
| P1 | [noahshinn/reflexion](https://github.com/noahshinn/reflexion) | academic origin of the pattern (NeurIPS'23) | 3,208 |
| P1 | [madaan/self-refine](https://github.com/madaan/self-refine) | self-critique loop reference implementation (NeurIPS'23) | 812 |
| P1 | [CRITIC](https://github.com/microsoft/ProphetNet/tree/master/CRITIC) | tool-interactive critiquing (ICLR'24) | — |
| P1 | LangGraph Reflection/Reflexion official tutorial | critique-loop implementation structure (same stack) | — |
| P1 | [lapisrocks/LanguageAgentTreeSearch](https://github.com/lapisrocks/LanguageAgentTreeSearch) | frontier: reflection + MCTS (ICML'24) | 845 |
| P2 | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | main-loop framework (primary) | 37,486 |
| P2 | [SqueezeAILab/LLMCompiler](https://github.com/SqueezeAILab/LLMCompiler) | DAG parallel planning idea | 1,861 |
| P2 | [AGI-Edgerunners/Plan-and-Solve-Prompting](https://github.com/AGI-Edgerunners/Plan-and-Solve-Prompting) | academic origin of the pattern (ACL'23) | 733 |
| P3 | [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers) | tool-layer protocol design (primary) | 88,559 |
| P3 | [ShishirPatil/gorilla](https://github.com/ShishirPatil/gorilla) | tool-calling benchmark BFCL | 12,949 |
| P3 | [OpenBMB/ToolBench](https://github.com/OpenBMB/ToolBench) | tool-learning data and benchmark | 5,699 |
| P4 | [crewAIInc/crewAI](https://github.com/crewAIInc/crewAI) | role-divided team reference | 53,499 |
| P4 | [microsoft/autogen](https://github.com/microsoft/autogen) | multi-agent orchestration (maintenance mode; succeeded by Agent Framework) | ~55,000 |
| P4 | [geekan/MetaGPT](https://github.com/geekan/MetaGPT) | role-SOP reference (not chosen) | — |
| P5 | [CopilotKit/CopilotKit](https://github.com/CopilotKit/CopilotKit) | HITL collaboration framework reference | 36,167 |
| P5 | [ag-ui-protocol/ag-ui](https://github.com/ag-ui-protocol/ag-ui) | agent-user interaction protocol | 14,809 |
| P5 | [sst/opencode](https://github.com/sst/opencode) | permission model + declarative agents (primary reference) | — |
| General | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | reflection kernel (secondary reference) | — |

---

## Appendix: real-model (Kimi k3) verification log (2026-07-20)

Endpoint `https://api.kimi.com/coding/v1`, model `k3` (this model only allows
temperature=1; the code no longer sends temperature by default, overridable via
`ANGINEER_TEMPERATURE`).

### Round 1: full run of the energy-storage example task (P2/P3 chain)

- k3 autonomously planned 6 specialist steps with 30 tool calls in total; repeated schema
  parameter errors were all self-corrected via structured error feedback;
- PCB CAM genuinely reproduced "check fail → tune → re-check pass";
- Space Control genuinely fired twice, and the Replanner's two corrective steps both
  self-rescued successfully;
- `production.apply` was denied at the permission layer; the model wrapped up with
  professional change-request wording.

### Issues found in Round 1 and their fixes

| Finding | Fix |
|---|---|
| `MAX_STEP_ITERS=6` too tight for a real model | raised to 8 |
| Steps were context-isolated; the chief couldn't see prior results | cross-step context injection (each step carries summaries of the last 4 prior results) |
| Schema errors precipitated no lessons | added an error-lesson branch in `reflection.py` |
| `kg_query` summary inconsistent with data ("0 hits" yet phrased as "hit 0 entries") | summary corrected to "returned all N reference entries" |

### Round 2: targeted regression

- Context injection took effect: the CAM agent cited upstream monitoring boundaries
  (51.2V / SOC 62% / 41.5°C / 1.5°C margin);
- 7 error lessons entered replanning instructions;
- The chief produced a report with P0/P1 risk grading and proactively decided not to call
  `production.apply` ("P0 items not closed") — model judgment and permission policy as
  double safeguards.

### Round 3: five-pattern verification (critique loop + team.ask + acceptance gate)

**Round 3A (real5)** — first real drive of the five-pattern mechanisms:

- P1 critique loop caught substantive issues: the structure agent's "safety factor 1.37
  vs 1.52 contradiction" was sent back for revision; the Safety agent's conclusion was
  criticized as "insufficient evidence, do not approve for now"; the chief's first report
  draft was criticized for "lacking a structured summary of specialist results, risk
  grading, and change disposition" — approved after revision;
- P4 consultation fault tolerance: the model spontaneously consulted a **nonexistent**
  `certification_engineer` via `team.ask`; the trio of structured error feedback + lesson
  persistence + replanning caught it, and it then consulted the real Safety agent;
- P5 acceptance gate fired normally: "acceptance passed, flow ends";
- A finishing defect was exposed and fixed: the chief wrote the report to `reports/`,
  leaving `output/` uncreated, so `dump_log_jsonl` threw `FileNotFoundError` → fixed with
  `os.makedirs`;
- Two robustness issues were exposed and fixed: k3's `final` can be a dict rather than a
  string (normalization added in three places); injected prior context falsely triggered
  Mock's consulted branch (condition now excludes "前置步骤结果").

**Round 3B (real6 re-run, 41 tool calls)** — verified the fixed pipeline end-to-end, and
incidentally tested "whether the system stays safe when all upstream steps fail":

- Clean finish: 41 log entries written to `output/tool_calls.jsonl`, no crash; acceptance
  gate fired, "acceptance passed, flow ends";
- k3 showed a "repeatedly calling tools without finalizing" failure mode across 6
  specialist steps; Space Control's 6 circuit-breaks all terminated cleanly with no
  infinite loop — the three hard constraints (8 iterations / 2 replans / 2 critique
  rounds) are designed exactly for this kind of real-model behavior;
- With all four upstream steps forcibly stopped, the chief **honestly disclosed data
  quality problems**: the report explicitly stated "no trustworthy execution results
  available for production change", marked every change item [pending verification], and
  decided to "freeze production changes; do not call production.apply; send back to
  specialists for re-convergence"; its final passed the critique loop with the review
  "conclusion fully addresses the task goal ... nothing fabricated";
- 5 of the 6 persisted lessons are schema-error type (the error-lesson branch keeps
  working).

> Round 3 conclusion: the five-pattern mechanisms all work under a real model; more
> importantly, when upstream steps fail wholesale, the permission policy (deny), the
> critique loop (no release for unsupported conclusions), the acceptance gate (final
> human confirmation), and the model's own safety judgment form a four-layer safeguard —
> the system ends in a "safe failure" rather than shipping with defects.

### Round 4: new-task generality verification (smart meter, real7)

To verify "the prototype is not limited to a single example task", we ran the task
**"Design a smart meter: complete power-management chip simulation and PCB
current-carrying CAM check, read quality inspection records, generate the engineering
report"**:

- **Mock mode**: keyword routing automatically matched a new agent combination of
  chip_design → pcb_cam → discharge_monitoring → chief (the chip agent's first
  appearance), and the five-pattern closed loop fully reproduced — task dispatch is not
  hardcoded;
- **k3 real model** (13 tool calls, all 5 steps passed, acceptance gate closed normally):
  - Planning: the model autonomously decomposed 5 steps, including a Safety compliance
    audit step it **added on its own initiative** (not stated in the task text);
  - P1 critique loop caught substantive issues in the chip step: "missing PVT process
    corners / temperature-voltage conditions / power-budget benchmarking / DRC rule set
    and version / report path" — the conclusion was revised from "pass" to **"conditional
    pass / pending review"**;
  - The CAM closed loop genuinely replayed on the new task: first check 0.20mm fail →
    `set_param` 0.30mm → re-check pass;
  - Cross-step context injection worked: both Safety's and the chief's conclusions cited
    risk items from all prior steps (e.g. "1.5°C thermal margin stacking with the 41.5°C
    monitored temperature");
  - The chief generated change request CR-2024-SM-001, reached an overall verdict of
    "conditional pass, direct production release not recommended", and proactively did
    not call `production.apply`;
  - Fault tolerance: the model's hallucinated `team.ask` target `signoff_review` was
    caught by structured error feedback + lesson persistence.

> Round 4 conclusion: the framework's five-pattern mechanisms work on a new task with zero
> changes; task dependence exists only in agent definitions and tool simulation data
> (declaratively replaceable), consistent with the boundary disclosures in Section 9.
