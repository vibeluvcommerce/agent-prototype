# 技术选型说明 —— Angineer Agent Prototype

本文档回答两个问题：**原型每一层为什么这么设计**（设计逻辑），
以及**每个技术/架构为什么选它**（选型论证）。
覆盖范围：全部 5 种 Agent 设计模式（P1 Reflection / P2 Plan and Solve /
P3 Tool Use / P4 Multi-Agent Collaboration / P5 Human-in-the-Loop）。

---

## 1. 项目定位与验证状态

原型按 "Angineer Large Model (Open Source)" 架构图实现一个**可运行的完整闭环**：
把图上每个功能框落到真实代码，用工程任务（户外储能电源设计）跑通
"分解 → 执行 → 失败 → 反思 → 协商 → 调参 → 复检 → 批判修订 → 报告 → 高危拦截 → 验收"
全流程，五种设计模式在一次运行中全部真实触发。

**验证状态（2026-07-20 实跑通过）**：

| # | 测试 | 涉及模式 | 结果 |
|---|---|---|---|
| 1 | 户外储能电源示例任务全流程（5 步分派、工具调用留痕、报告落盘） | P2/P3 | 通过 |
| 2 | CAM 失败 → 教训 → 协商 → 调参 → 复检通过的真实闭环 | P1/P3/P4/P5 | 通过 |
| 3 | 总工结论自我批判不通过 → 带意见修订 → 评审通过 | P1 | 通过 |
| 4 | team.ask 代理间协商（PCB CAM 咨询 Safety 温升影响） | P4 | 通过 |
| 5 | 交互式 HITL：批准/拒绝按预期生效 + 验收门禁 | P5 | 通过 |
| 6 | 关键词路由变体与无匹配兜底 | P2 | 通过 |
| 7 | 故障注入：熔断 + 最多 2 次重规划后正常终止，无死循环 | P2/Space Control | 通过 |
| 8 | 权限分层：只读放行、写操作询问、`production.*` 拦截 | P5 | 通过 |
| 9 | 真实模型（Kimi k3）端到端实测五模式 | 全部 | 通过（见附录） |
| 10 | 新任务通用性（智能电表：Mock 路由换组合 + k3 自主规划 5 步全过） | 全部 | 通过（见附录第四轮） |

---

## 2. 总体设计逻辑：为什么分五层

```
用户任务 (CLI)
   │
主循环层   graph.py       LangGraph 状态图: Planner → Executor → Replanner (P2)
   │                       内嵌: generator-critic 批判环(P1) / _consult 协商(P4) / 验收门禁(P5)
代理层     agents/*.md    6 个声明式专业 Angineer（提示词 + 授权工具 + 权限覆盖）(P4)
   │
工具层     tools.py       受控调用层: schema 校验 → 权限 → 执行 → 日志 (P3)
   │        permissions.py  allow/ask/deny 权限引擎 (P5)
环境层     EngineeringWorld  模拟工程环境状态（工具间真实依赖）
   ▼
产物: output/engineering_report.md + output/tool_calls.jsonl
```

分层原则只有一条：**让"决策"与"执行"与"权限"分别可替换**。

- 决策（用哪个模型、怎么规划、怎么评审）集中在 LLM 抽象层和主循环层——换模型不动业务；
- 执行（每个专业会什么）集中在代理层的声明式文件——加一个专业 Angineer
  只需要加一个 md 文件，不改任何代码；
- 权限（谁能做什么）集中在工具层——模型永远碰不到未授权的能力，
  即"权限收敛在工具级，而非放给模型"。

工具之间通过 `EngineeringWorld` 共享状态（如 `set_param` 改线宽会改变
后续 `cam_check` 的结果），因此"检查 → 优化 → 复检"是真实状态依赖驱动的
可复现逻辑，不是写死的表演脚本——这是原型作为"架构证据"的底线。

---

## 3. Pattern 2（Plan and Solve）：为什么主选 LangGraph

### 3.1 选型

**主选：`langchain-ai/langgraph`（37,486 stars）的 Plan-and-Execute 范式**；
思想借鉴 `SqueezeAILab/LLMCompiler`（ICML 2024）；原理引用
`AGI-Edgerunners/Plan-and-Solve-Prompting`（ACL 2023，模式名称出处）。

### 3.2 设计逻辑：主循环如何落地模式定义

| 模式定义 | 原型实现 |
|---|---|
| 规划层产出任务序列 | `planner_node`：`llm.decompose()` 产出显式 `[{agent, instruction}]` 步骤列表 |
| 执行层按序执行 | `executor_node`：每步路由到对应专业代理，驱动 ReAct 小循环 |
| 重规划检查点 | `replanner_node`：每步之后判断——失败且未超限则插入携带教训的修正步骤 |
| Task to Agent（任务分派到代理） | 步骤中的 `agent` 字段就是路由键，对应底部专业 Angineer 总线 |

**计划是显式数据结构**这一点刻意借鉴了 LLMCompiler：计划可以被检查、
被修改、被重新入队，而不是藏在模型上下文里。后续把顺序计划升级为
DAG 并行计划时，只需扩展 planner 的输出结构（LLMCompiler 已被 LangGraph
官方收录，演进路径现成）。

### 3.3 Space Control：模式论文不会告诉你的工程必需

Plan-and-Execute 有两类典型失败模式：单步内空转和计划级死循环。
原型设三道硬约束（故障注入测试已验证）：

- `MAX_STEP_ITERS = 8`：单步工具调用上限（初版为 6；真实模型实测发现前期
  schema 试错会消耗预算，上调为 8——熔断本质保留）；
- `MAX_REPLANS = 2`：全程计划级重规划上限；
- `MAX_CRITIQUES = 2`：单步自我批判轮数上限（见第 5 节）。

### 3.4 为什么不选其他

| 候选 | 排除原因 |
|---|---|
| microsoft/TaskWeaver（6,174 stars） | 执行端是单一代码解释器，无法表达"多专业 Angineer 总线"的分派结构；面向数据分析 |
| OpenBMB/XAgent（8,525 stars） | 端到端自治 agent 产品，双循环写死在内部，难以按自定义架构图改造 |
| 自研规划循环 | 状态持久化、条件路由、中断恢复都要重造，LangGraph 已是业界事实标准 |
| 纯 Plan-and-Solve 提示词 | 只是单 prompt 技巧，无法表达多代理分派与状态机，仅作原理引用 |

---

## 4. Pattern 3（Tool Use）：为什么主选 MCP 风格受控调用层

### 4.1 选型

**主选：按 `modelcontextprotocol/servers`（88,559 stars）的设计自实现
最小受控调用层**；评测配套 `ShishirPatil/gorilla`（BFCL，12,949 stars）与
`OpenBMB/ToolBench`（ICLR'24 spotlight，5,699 stars）。

### 4.2 设计逻辑：调用管线如何落地模式定义

| 工具层五要素 | 原型实现 |
|---|---|
| 工具 schema | 每个工具声明 name / description / parameters（JSON Schema）/ handler；`list_tools()` 等价 MCP 的 `tools/list` |
| 执行逻辑 | handler 与模型隔离，模型只看到 schema |
| 消息处理 | 结构化返回 `{status, summary, data}`，写入代理 scratchpad |
| 错误处理 | 缺参数、未知工具、被拒绝均返回结构化错误供代理恢复（k3 实测中模型多次借此自我纠正） |
| 状态管理 | `EngineeringWorld` 承载跨工具状态；`tool_calls.jsonl` 全量留痕可追溯 |

权限检查嵌在校验与执行之间，形成完整管线：
**schema 校验 → 权限匹配 → 执行 → 日志 → 反思**。

### 4.3 为什么不直接引入 MCP 官方 SDK

1. **可读性**：40 行代码能逐行审清"MCP 在协议里到底规定了什么"，
   引 SDK 则这一切藏在框架内部；
2. **替换成本为零**：接口形状与 MCP 一致，工程化阶段把 CAD/EDA/CAM
   各包成独立 MCP server 后，代理层和主循环完全不用改。

### 4.4 开源基座的工具调用能力：评测与增强（后续路线）

- **评测选型**：用 gorilla 的 Berkeley Function Calling Leaderboard
  （已发布面向真实 agentic 场景的 V4）评测候选基座；
- **数据增强**：基座太弱时用 ToolBench 的 16k+ 真实 API 数据做领域微调。

---

## 5. Pattern 1（Reflection）：为什么用"工具落地式"自我批判环

### 5.1 选型

**结构主选：LangGraph 官方 Basic Reflection 教程的 generator-critic 双节点循环**
（与现有技术栈同构）；学术锚点：`noahshinn/reflexion`（NeurIPS 2023，
3,208 stars，模式命名出处）与 `madaan/self-refine`（NeurIPS 2023，812 stars）；
反馈路线锚点：`CRITIC`（ICLR 2024，微软，工具交互式批判）。

### 5.2 设计逻辑：与 Reflexion 三件套逐项对应

| Reflexion 组件 | 原型实现 |
|---|---|
| Actor（执行者） | Executor 的 ReAct 循环 |
| 情景记忆缓冲（反思文本入库） | `ReflectionModule` 的 lessons：失败/schema 错误/权限拦截均沉淀为结构化教训，供重规划使用（k3 实测 7 条生效） |
| Evaluator + 修订环 | **generator-critic 批判环**：代理给出 `final` 后不被直接接受，先由 `llm.critique()` 对照任务目标评审——`revise` 则带意见返回继续执行，`approve` 才放行 |

终止条件沿用教程做法并接入既有机制：单步最多 `MAX_CRITIQUES=2` 轮
（Self-Refine 的实践经验是 1-2 轮即收敛），且批判不计入工具调用预算、
评审解析失败默认 approve 放行——自我批判永远不能让流程失控。

### 5.3 为什么是"工具落地式"而非"空想式"反思

学术界的共识性结论（CRITIC 论文及后续研究）是：**LLM 在没有外部反馈时
并不能可靠自纠**，纯文本自我批判容易"自欺欺人"。因此原型的批判依据
始终锚定真实工具结果：`cam_check` 返回的 fail 是确定性的工程事实，
教训从这里产生，批判围绕它展开——这正是 CRITIC 的 tool-interactive
critiquing 路线，也是汇报时"我们的反思为什么接在工具层之后"的核心论据。

### 5.4 为什么不选其他

- **LATS**（ICML 2024，845 stars）：反思 + 蒙特卡洛树搜索的最强形态，
  但引入搜索树复杂度远超原型阶段所需，列为前沿展望；
- **hermes-agent 内核学习循环**：已部分借鉴（周期自检 + 教训沉淀即来自它），
  其 RL 训练闭环（Atropos）属模型训练范畴，超出架构原型边界。

---

## 6. Pattern 4（Multi-Agent Collaboration）：为什么是"编排分派 + 协商工具"

### 6.1 选型

**结构主选：声明式专业代理（opencode 风格）+ `team.ask` 协商元工具（自实现）**；
参考 `crewAIInc/crewAI`（53,499 stars，角色分工团队）、`microsoft/autogen`
（约 55,000 stars，2026 年 2 月起维护模式，官方继任为 Microsoft Agent
Framework，支持 GroupChat/Handoff/Magentic 多智能体编排）、
`geekan/MetaGPT`（软件公司角色 SOP）、`camel-ai/camel`（学术研究向）。

### 6.2 设计逻辑：架构图决定协作形态

架构图底部是"总线式专业 Angineer + 中央核心"，不是对等网络——
因此协作拆成两层落地：

1. **分工层（已有）**：一个 md 文件定义一个专业 Angineer，各有专属
   提示词、授权工具集与权限——对应 CrewAI 的"角色+目标+工具"三元组，
   但用声明式文件而非代码类，颗粒度与架构图总线一致；
2. **协商层（本次新增）**：`team.ask` 元工具——任何代理执行中可点名
   咨询另一个代理（如 PCB CAM 问 Safety"线宽加粗对温升的影响"），
   被咨询方以自己的工具集作答（最多 3 步），结果返回咨询方。
   禁止嵌套协商（防循环），协商权限走统一权限引擎。

这与 AutoGen 的"异步消息+工具闭环"设计原则一致：**重操作必须封装成
可观察、可中断的工具调用**——`team.ask` 在日志中全程留痕（`[Multi-Agent]`），
且被咨询方的工具调用同样穿过受控管线。

### 6.3 为什么不选全面对等协作框架

- CrewAI/AutoGen 是完整框架，引入任一都会替换掉我们按架构图自建的
  LangGraph 主循环（P2 的落点）——模式之间应该叠加而非互相覆盖；
- MetaGPT 的 SOP 角色剧本适合软件开发流水线，与工程设计域不贴合；
- "编排器分派 + 协商工具 + 上下文注入"三件套已覆盖架构图表达的协作语义，
  且每个零件都保持简单、可逐行审计。

---

## 7. Pattern 5（Human-in-the-Loop）：为什么是"权限引擎 + 验收门禁"双闸门

### 7.1 选型

**主选：`sst/opencode` 的权限模型（工具级）+ LangGraph interrupt 式
流程级暂停点（验收门禁，自实现）**；参考 `CopilotKit/CopilotKit`
（36,167 stars，HITL 前端协作框架）与 `ag-ui-protocol/ag-ui`
（14,809 stars，Agent-User Interaction 协议，HITL 为核心特性）。

### 7.2 设计逻辑：两个粒度的人工介入

| 粒度 | 原型实现 | 对应参考 |
|---|---|---|
| 工具级 | `allow/ask/deny` 三态 + 通配符 + 后匹配优先 + 代理级覆盖；只读自动放行、写操作逐条询问、`production.*` 默认 deny | opencode 权限模型；文章"权限收敛在工具级" |
| 流程级 | 全部步骤完成后的**验收门禁**（`final_acceptance`）：暂停流程等人工确认"验收通过/不通过"，对应 LangGraph 的 interrupt 暂停点与 AG-UI 的"共享状态+审批"语义 | LangGraph HITL、AG-UI |

k3 实测中出现的理想行为：模型在明知 `production.apply` 被 deny 后，
以专业变更申请措辞收尾而非反复重试——**模型判断与权限策略形成双重保险**，
这是 HITL 设计有效的直接证据。

### 7.3 权限与代理定义：为什么主参考 opencode

三个热门开源 Agent（OpenClaw / opencode / hermes-agent）中，opencode 的
模块与本架构图映射最干净：plan/build 双 agent ≈ Task Decomposition → 执行；
声明式子代理 ≈ 底部专业 Angineer 总线；allow/ask/deny ≈ 工程安全场景的硬要求。
hermes-agent 只取反思模块（周期自检 + 教训沉淀）；OpenClaw 只借
heartbeat/cron 定时调度（路线图第 5 步）。

---

## 8. LLM 抽象层：为什么 Mock 与真实模型可互换

`llm.py` 定义三个统一接口：`decompose`（规划）、`act`（单步决策）、
`critique`（自我评审），两个实现：

- **MockLLM**：离线规则驱动，输出确定。价值——离线/CI 可复现；
  架构验证不依赖外部服务；"模型能力"与"架构正确性"解耦。
- **OpenAILLM**：任何 OpenAI 兼容端点（`OPENAI_API_KEY` / `OPENAI_BASE_URL` /
  `ANGINEER_MODEL`），可接 vLLM/Ollama 开源基座；默认不传 temperature
  （部分模型如 kimi k3 不允许自定义），可用 `ANGINEER_TEMPERATURE` 指定。

这一层同时是未来的**评测插点**：同一套任务跑不同基座模型，统计计划合理率、
工具调用成功率、批判环收敛率，即是 BFCL 思路的领域化评测。

---

## 9. 已知边界（诚实声明）

1. MockLLM 的代理策略是脚本化的——它证明架构与流程正确，
   不代表真实模型的规划/工具选择/自评能力；
2. 工具是模拟实现（无真实 CAD/EDA/CAM 对接），但工具间状态依赖是真实的；
   已支持 `--upload` 用用户上传的设计 JSON / 监测 CSV 初始化环境状态
   （半真实模式：输入数据真实、判定逻辑真实、执行引擎仍为模拟），
   把 handler 替换为真实工程软件调用即为全真实（工程化路线第 2 步）；
3. 计划是顺序执行，DAG 并行（LLMCompiler 思想）在路线图中；
4. 知识图谱为内置字典模拟，工程化应换 Neo4j 等真实图库；
5. 模拟工具对相同/相似调用总返回静态成功结果，真实模型缺少"已完成"
   信号时可能过度迭代——Space Control 按设计兜底，接真实工具后自然消失；
6. 代理间协商目前是一对一咨询（team.ask），多人会议式协作
   （GroupChat/Magentic 形态）在路线图中；
7. 架构图底部总线的 **Inspection Angineer 未独立建代理**——其职责落在
   `inspection.query` 工具上，由 Discharge Monitoring 代理持有调用；
   **Industry Angineer 与 "Etc…" 未实现**。这是刻意留白：声明式扩展
   （往 `agents/` 加一个 md 文件）即可补齐，无需改动主循环代码；
8. 框架层（主循环/批判环/协商/权限/验收门禁）任务无关，但 Mock 层的
   离线脚本按预设链路运行（关键词路由 + 罐装数据）——换任务验证请用
   `--real` 接真实模型（已用智能电表任务实测，见附录第四轮）。

## 10. 工程化路线

| 阶段 | 动作 |
|---|---|
| 1 | `--real` 接开源基座（vLLM/Ollama），用 BFCL 评测工具调用能力 |
| 2 | 工具层替换为 MCP 官方 SDK，CAD/EDA/CAM 各包成 server |
| 3 | 顺序计划升级为 DAG 并行计划（LLMCompiler 思想） |
| 4 | kg_query 换真实工程知识图谱（Neo4j） |
| 5 | 引入 cron/heartbeat，放电监测定时巡检（借 OpenClaw） |
| 6 | team.ask 升级为多代理会议（参考 Microsoft Agent Framework 的 GroupChat/Magentic） |
| 7 | 基座工具调用不足时，用 ToolBench 数据微调领域模型 |

---

## 11. 参考项目清单

stars 抓取于 2026-07（部分为公开报道值）。

| 模式 | 项目 | 用途 | Stars |
|---|---|---|---|
| P1 | [noahshinn/reflexion](https://github.com/noahshinn/reflexion) | 模式学术出处（NeurIPS'23） | 3,208 |
| P1 | [madaan/self-refine](https://github.com/madaan/self-refine) | 自我批判环参考实现（NeurIPS'23） | 812 |
| P1 | [CRITIC](https://github.com/microsoft/ProphetNet/tree/master/CRITIC) | 工具交互式批判（ICLR'24） | — |
| P1 | LangGraph Reflection/Reflexion 官方教程 | 批判环实现结构（同栈） | — |
| P1 | [lapisrocks/LanguageAgentTreeSearch](https://github.com/lapisrocks/LanguageAgentTreeSearch) | 前沿：反思+MCTS（ICML'24） | 845 |
| P2 | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | 主循环框架（主选） | 37,486 |
| P2 | [SqueezeAILab/LLMCompiler](https://github.com/SqueezeAILab/LLMCompiler) | DAG 并行计划思想 | 1,861 |
| P2 | [AGI-Edgerunners/Plan-and-Solve-Prompting](https://github.com/AGI-Edgerunners/Plan-and-Solve-Prompting) | 模式学术出处（ACL'23） | 733 |
| P3 | [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers) | 工具层协议设计（主选） | 88,559 |
| P3 | [ShishirPatil/gorilla](https://github.com/ShishirPatil/gorilla) | 工具调用评测 BFCL | 12,949 |
| P3 | [OpenBMB/ToolBench](https://github.com/OpenBMB/ToolBench) | 工具学习数据与基准 | 5,699 |
| P4 | [crewAIInc/crewAI](https://github.com/crewAIInc/crewAI) | 角色分工团队参考 | 53,499 |
| P4 | [microsoft/autogen](https://github.com/microsoft/autogen) | 多智能体编排（维护模式，继任为 Agent Framework） | 约 55,000 |
| P4 | [geekan/MetaGPT](https://github.com/geekan/MetaGPT) | 角色 SOP 参考（未选） | — |
| P5 | [CopilotKit/CopilotKit](https://github.com/CopilotKit/CopilotKit) | HITL 协作框架参考 | 36,167 |
| P5 | [ag-ui-protocol/ag-ui](https://github.com/ag-ui-protocol/ag-ui) | 代理-用户交互协议 | 14,809 |
| P5 | [sst/opencode](https://github.com/sst/opencode) | 权限模型 + 声明式代理（主参考） | — |
| 综合 | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | 反思内核（辅参考） | — |

---

## 附录：真实模型（Kimi k3）实测记录（2026-07-20）

端点 `https://api.kimi.com/coding/v1`，模型 `k3`（该模型仅允许 temperature=1，
代码已改为默认不传 temperature，可用 `ANGINEER_TEMPERATURE` 显式指定）。

### 第一轮：户外储能电源示例任务全量跑（P2/P3 链路）

- k3 自主规划 6 个专业步骤，全程 30 次工具调用；多次 schema 参数错误
  经结构化错误回写全部自我纠正；
- PCB CAM 真实复现"检查失败 → 调参 → 复检通过"；
- Space Control 真实触发 2 次，Replanner 两次插入修正步骤均自救成功；
- `production.apply` 被权限层 deny，模型以专业变更申请措辞收尾。

### 第一轮暴露的问题与修复

| 发现 | 修复 |
|---|---|
| `MAX_STEP_ITERS=6` 对真实模型偏紧 | 上调为 8 |
| 各步骤上下文隔离，总工看不到前置结果 | 跨步骤上下文注入（每步附最近 4 条前置结果摘要） |
| schema 错误不沉淀教训 | `reflection.py` 增加 error 教训分支 |
| `kg_query` 摘要与数据不一致（0 命中却称"命中 0 条"） | 修正摘要为"返回全部 N 条参考条目" |

### 第二轮：定向回归

- 上下文注入生效：CAM 代理引用前置监测边界（51.2V/SOC 62%/41.5°C/裕量 1.5°C）；
- 7 条错误教训进入重规划指令；
- 总工产出含 P0/P1 风险分级的报告，并主动决定不调用 `production.apply`
  （"P0 项未闭环"）——模型判断与权限策略双重保险。

### 第三轮：五模式实测（批判环 + team.ask + 验收门禁）

**第三轮 A（real5）**——五模式机制首次真实驱动：

- P1 批判环抓到实质问题：结构代理结论中"安全系数 1.37 与 1.52 前后矛盾"被要求修订；
  Safety 代理结论被批判"证据不足，暂不予通过"；总工首版报告被批"缺少各专业结果的
  结构化汇总、风险分级与变更去向"，修订后通过；
- P4 协商容错：模型自发 `team.ask` 咨询**不存在的** `certification_engineer`，
  结构化错误回写 + 教训沉淀 + 重规划三件套接住，随后改询真实存在的 Safety 代理；
- P5 验收门禁正常触发，"验收通过，流程结束"；
- 暴露并修复一个收尾缺陷：总工把报告写到 `reports/` 导致 `output/` 未创建，
  `dump_log_jsonl` 抛 `FileNotFoundError` → 已加 `os.makedirs` 修复；
- 暴露并修复两类鲁棒性问题：k3 的 final 可能是 dict 而非 str（三处加归一化）；
  注入的前置上下文误触发 Mock 被咨询分支（条件排除"前置步骤结果"）。

**第三轮 B（real6 复跑，41 次工具调用）**——验证修复后的完整跑通，并意外验证了
"上游全部失败时系统是否仍然安全"：

- 尾声完整跑通：`output/tool_calls.jsonl` 41 条日志正常落盘，无崩溃；
  验收门禁触发，"验收通过，流程结束"；
- k3 在 6 个专业步骤中出现"反复调工具不定稿"的失效模式，Space Control
  6 次熔断均正常终止、无死循环——三道硬约束（8 次迭代 / 2 次重规划 / 2 轮批判）
  正是为真实模型的这类行为设计；
- 总工在四个上游步骤均被强制停止的情况下，**诚实披露数据质量问题**：
  报告明确标注"无可信执行结果可用于生产变更"，变更项全部标记【待验证】，
  决策"冻结生产变更；不调用 production.apply；退回各专业重新收敛"，
  其 final 经批判环评审"结论完整回应了任务目标……未伪造"后通过；
- 6 条沉淀教训中 5 条为 schema error 类（error 教训分支持续生效）。

> 第三轮结论：五模式机制在真实模型下全部生效；更重要的是，当上游步骤整体失败时，
> 权限策略（deny）、批判环（不放行无证据结论）、验收门禁（人工最终确认）与模型的
> 安全判断形成四重保险，系统以"安全失败"方式收尾，而非带病交付。

### 第四轮：新任务通用性实测（智能电表，real7）

为验证"原型并非只能跑单一示例任务"，改用 **"设计一款智能电表：完成电源管理芯片
仿真与 PCB 载流 CAM 检查，读取质量检测记录，生成工程报告"** 任务实测：

- **Mock 模式**：关键词路由自动匹配出 chip_design → pcb_cam →
  discharge_monitoring → chief 的新代理组合（芯片代理首次登场），
  五模式闭环完整复现——任务分派不是写死的；
- **k3 真实模型**（13 次工具调用，5 步全部通过，验收门禁正常收尾）：
  - 规划：模型自主分解出 5 步，含任务文本未明说、**自行补充**的 Safety
    安规审计步；
  - P1 批判环在芯片步骤抓到实质问题："缺 PVT 工艺角/温压条件/功耗预算对标/
    DRC 规则集与版本/报告路径"，结论从"通过"修订为**"有条件通过/待复核"**；
  - CAM 闭环在新任务真实重演：初检 0.20mm fail → `set_param` 0.30mm →
    复检通过；
  - 跨步骤上下文注入生效：Safety 与总工的结论均引用全部前置步骤的风险项
    （如"温升裕量 1.5°C 与 41.5°C 监测温度叠加"）；
  - 总工生成变更申请单 CR-2024-SM-001，综合判定"有条件通过，不建议直接
    生产放行"，并主动不调用 `production.apply`；
  - 容错：模型幻觉出的 `team.ask` 目标代理 `signoff_review` 被结构化错误
    回写 + 教训沉淀接住。

> 第四轮结论：框架层的五模式机制对新任务无需任何改动即全部生效；
> 任务相关性只存在于代理定义与工具模拟数据（声明式可替换），与第 9 节
> 边界声明一致。
