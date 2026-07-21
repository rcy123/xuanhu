# AR-B-031 / L4.5-11 范围变更申请（B133 回退前历史记录）

> 版本：v1.0（原批准已随 B133 回退暂停执行）
> 当前处置：2026-07-21 已发布 `L4.5-11-0`，以 exact HEAD `b97c9f9` 重新校准范围。本文只证明当时曾批准改变方向，不授权恢复已回退实现，也不替代待验收的 v2 方案。
> 日期：2026-07-21（Asia/Shanghai）
> 申请人：项目经理侧独立诊断
> 批准人：Kimi Code（项目经理，受用户授权）
> 关联方案：`L4.5-11收敛性架构重写方案-2026-07-21.md`
> 性质：**已批准的范围变更申请，作为 L4.5-11 新架构实施的授权输入。本文件不修改任何生产代码。**

---

## 1. 当前状态

| 项目 | 状态 |
|---|---|
| 任务 | L4.5-11 / AR-B-031 隐私返工 |
| 当前任务状态 | 返工中 / 验收未通过 |
| 当前批次 | B133（第七轮 development-red 已绑定） |
| 当前阻塞 | AR-B-031 处理中 |
| L5 准入 | NO-GO（`L5进入前专业安全预审报告-2026-07-19.md`） |
| 分支 | `codex/l4-5-11-context-privacy-hardening` |
| 基线 HEAD | `af90abe6b007e7013f7306a82c22ebaa92d66be6` |

连续 133 批 red/green 返工后，L4.5-11 仍处于正式复审 FAIL 状态。最新失败 family 为：

- 逐字符 Unicode escape 全角普通数字误脱敏/绕过；
- partial exponent `E/E+` 后接空格/标点导致 PII 泄漏或误脱敏；
- 3～5 个 Context scalar 跨分片重组；
- `not/fake/test/simulation/non-vitals` 等词被误当 proven clinical 豁免。

## 2. 原范围与约束

原 AR-B-031 / L4.5-11 范围限定为：

- 只修改 `app/agent_runtime/context.py` 的投影边界；
- 通过更精确的黑名单/正则/语义扫描，识别已组装、已转义、已跨层混合的自由文本中的 PII；
- 保留临床豁免黑名单（`_CLINICAL_TRUST_CONFLICT_TOKENS`）；
- 不修改 `triage_precheck.py`、临床红旗规则、原始 Domain 事实语义、L5～L9、UI/持久化、默认开关；
- 不修改原始患者消息（byte-for-byte）。

## 3. 变更内容

### 3.1 核心范围变更

| 维度 | 原范围 | 变更后范围 |
|---|---|---|
| 脱敏点 | ContextBuilder 投影边界 | **Intake 入口 + ContextBuilder 投影 + Gateway 拼接边界** 三层 |
| 处理对象 | 已组装、已转义、已跨层混合的自由文本 | 单条原始患者消息（入口）+ 最终 messages 串（Gateway） |
| 临床豁免 | 黑名单排除（`not/fake/test/simulation/...`） | **冻结白名单**（字段名 + 数值范围 + 单位） |
| PII 判定范式 | 黑名单匹配“像不像身份证/手机号” | **白名单拒绝**：未命中临床白名单的数字 token 一律脱敏 |
| Domain 内容 | 原文进入下游 | **入口 pseudonym 化**，Domain 只存假名 |
| 代码结构 | `context.py` 单文件 10,000+ 行 | 拆分为入口、Gateway、白名单、投影、JSON parser、pseudonym 等独立模块 |

### 3.2 新三层脱敏架构

```
患者消息
   │
   ▼
[层 1: 数据入口脱敏]  ← build_intake_context 之前
   │  · 输入：单条原始患者消息（无 JSON 转义、无跨层拼接）
   │  · 动作：识别裸串手机号/身份证号 → HMAC 假名化（长度保持）
   │  · 临床豁免：冻结白名单（体温/血压/心率/呼吸/血糖等 + 精确数值范围）
   │  · Domain State 只存假名，原文不进入任何下游
   ▼
[层 2: ContextBuilder 投影]
   │  · 保留 allowed_fields 白名单投影 + scalar 边界检查
   │  · 删除 semantic number tokenizer、digit corridor、3 个 identity matcher、
   │     clinical measurement verifier、transport artifact mask、boundary witness
   ▼
[层 3: Gateway 拼接边界兜底]
   │  · 在 runtime.run() 前，对最终 messages 串做一次 fail-closed 扫描
   │  · 只识别入口层 1 应该处理但漏掉的裸 PII
   │  · 命中即 ContextBuildFailed，绝不把含裸 PII 的消息发给模型
   ▼
模型 Gateway
```

### 3.3 关键设计决策

1. **D1：临床豁免反转为冻结白名单**
   - 字段名白名单（如 `temperature`、`blood_pressure_systolic`、`heart_rate`、`respiratory_rate`、`blood_glucose`、`spo2`、`weight`、`height`、`bmi`，含 CJK 别名）。
   - 每个字段绑定单位 + 数值范围（如体温 34–43°C、心率 20–250 bpm）。
   - 只有“字段名命中白名单 AND 数值在范围 AND 单位匹配”三者交集，才保留数字。
   - 其余 `user` 层连续数字 token 一律脱敏，包括日期形状合法的 15 位业务号（登记为 P2 可用性影响，但不放宽 fail-closed）。

2. **D2：Pseudonym 化在入口完成**
   - 在 `build_intake_context` 调用 `ContextBuilder.build` 之前，对 `current_messages` 逐条 pseudonym 化。
   - 复用现有 `PseudonymKeyProvider` 和 `_projected_pseudonym` 基础设施。
   - 假名写入 `UserMessageProjection.content`，下游 ContextBuilder 看到的 `user_messages` 已不含原始号码。
   - 可逆性仅在审计侧，Domain 事实语义不变。

3. **D3：Gateway 拼接边界兜底**
   - 新增极简裸串匹配器，在 `runtime.run()` 前扫描最终 messages 串。
   - 只识别裸 11 位手机号 / 18 位身份证，不处理转义/全角/跨分片（这些形态在层 1 已不存在）。
   - 命中即 `ContextBuildFailed`。

4. **D4：拆分 `context.py`**
   - 按职责拆分为独立模块：
     - `app/agent_runtime/context/__init__.py`（公共 API）
     - `app/agent_runtime/context/_json_parser.py`
     - `app/agent_runtime/context/_pseudonym.py`
     - `app/agent_runtime/context/_redaction_entry.py`（层 1，新）
     - `app/agent_runtime/context/_gateway_guard.py`（层 3，新）
     - `app/agent_runtime/context/_clinical_whitelist.py`（D1，新）
     - `app/agent_runtime/context/_projection.py`（层 2 瘦身）

## 4. 根因分析（为何必须变更范围）

1. **脱敏点太晚**：在投影边界扫描已组装文本，必须逆向还原所有 Unicode 规范化、JSON 转义、全角、同形、跨分片重组形态，攻击面为无穷集。
2. **黑名单范式**：判断“这串数字像不像 PII”天然漏报；临床豁免靠黑名单排除假临床词，攻击者用新词即可绕过。
3. **单文件累积复杂度**：`context.py` 10,064 行，多 scanner 共享状态，修一个 family 打坏另一个，形成不可收敛的 red/green 循环。
4. **跨分片重组无统一兜底**：ContextBuilder 内三套机制（local redactions、joined spans、boundary witness）无法覆盖 3～5 分片的组合盲区。

## 5. 影响分析

| 影响域 | 影响 | 控制措施 |
|---|---|---|
| Legacy 路径 | 若 Legacy 路径复用同一入口，则 Domain 内容会改变 | pseudonym 化仅在 LangGraph v2 路径执行；Legacy 路径保持原样 |
| Domain State | 原始患者消息内容变为假名 | 审计侧保留可逆性；原始消息仍可在审计/合规存储中还原 |
| 前端/UI | 展示层可能看到假名而非原文 | 如前端展示原文，需从审计侧还原或明确展示假名；本次范围不改动前端 |
| 测试 | Round10/L2/L3 等回归需迁移或替换 | 分 6 批实施，每批“先红后绿”，不删除既有正向证据 |
| 性能 | 入口、投影、Gateway 三次扫描 | 入口和 Gateway 为极简匹配器；投影瘦身后总体复杂度低于现状 |
| 文件结构 | `context.py` 拆分 | 保留公共 API 不变，必要时保留 re-export shim |
| 临床决策 | 不改动 | 不触碰红旗规则、临床阈值、模型输出事实 |
| L5 准入 | 关闭 G0 工程基线的必要路径 | 完成后更新 `L5进入前专业安全预审报告` |

## 6. 风险与缓解

| 编号 | 风险 | 严重度 | 缓解 |
|---|---|---:|---|
| R1 | 范围超出原 AR-B-031 约束 | P0 | 由项目经理批准本变更申请；批准前不修改生产代码 |
| R2 | 入口 pseudonym 化改变 Domain State | P1 | Legacy 路径隔离；审计侧可逆；Domain 事实语义不变；补 Domain 投影回归 |
| R3 | 临床白名单误拒未登记生命体征 | P2 | 登记后续优化，不放宽白名单；可用性影响记 P2 |
| R4 | Gateway 兜底匹配器漏报新形态 | P2 | 层 1 已消除原始号码，层 3 只兜裸串，攻击面已收窄；漏报登记后续 |
| R5 | `context.py` 拆分破坏外部 import | P2 | 保留 re-export shim 或更新引用 |
| R6 | 大改动引入新 bug | P1 | 分批 + 先红后绿 + 三路复审 + full gates |
| R7 | 性能：三次扫描 | P3 | 入口和 Gateway 极简；投影瘦身；低配硬件噪声隔离复跑 |
| R8 | 历史逐批 exact-red 证据缺口 | P2 | 仅依赖已保存红态 + 当前 exact-green + 新三路复审；历史缺口已诚实接受为 P2 |
| R9 | 原始消息可逆性依赖密钥 | P1 | `PseudonymKeyProvider` 缺失时 `PseudonymKeyUnavailable` fail-closed；密钥管理纳入审计 |

## 7. 验收标准

1. 六批实施全部完成（A 临床白名单、B 入口 pseudonym、C 投影瘦身、D Gateway 兜底、E 文件拆分、F 正式复审）。
2. 每批遵循“先红后绿”，未改源码上建立失败回归，修复后绿。
3. 每批结束后至少一轮独立开发复审 P0/P1=0。
4. 最终批次 F：
   - 冻结统一 manifest；
   - 三路独立正式复审 P0/P1=0；
   - full gates 通过（Round10、L2、L3、R9、L2+L3+R9、受影响链、Ruff、mypy、lock、diff check）；
   - 精确暂存 dry-run 验证；
   - 验收、提交、clean exact-HEAD 复验。
5. AR-B-031 状态改为“已关闭”，L0-L4 工程验收重新通过。
6. 更新 `L5进入前专业安全预审报告`，把新架构证据纳入 G0 关闭材料。
7. 不放宽性能阈值；低配硬件噪声用隔离复跑处理。
8. 不执行网络安全、Trusted Access、渗透、`npm audit`、依赖审计。

## 8. 批准决策

本范围变更须经项目经理批准以下决策点：

1. **是否批准范围变更**：允许脱敏点上移 Intake 入口 + Gateway 拼接边界，超出原 AR-B-031 范围。
2. **是否接受 R2（入口 pseudonym 化改变 Domain State 内容）**：原始患者消息在 Domain 中变为假名，可逆性只在审计侧。
3. **是否接受 D1 临床白名单反转**：包括“日期形状合法 15 位业务号被误脱敏”作为 P2 可用性影响，不放宽 fail-closed。
4. **是否批准 `context.py` 拆分**：影响面较大，但消除二次复杂度。

批准方式：在本文件末尾“审批记录”栏签字/确认。批准后进入实施批次 A～F；不批准则执行退化路径。

## 9. 退化路径

若本范围变更**不批准**，则只能在原 AR-B-031 范围内继续逐批收口。基于 133 批的模式，收敛性不保证，预期会继续出现新的 Unicode/转义/跨分片 family。此路径下：

- 把“架构性重写”作为 P1 登记到后续优化台账；
- 当前范围内只做“维持 P0/P1 不恶化”的最小修复，不再追求彻底关闭 AR-B-031；
- L5 保持 NO-GO，G0 保持打开。

## 10. 审批记录

| 决策项 | 批准人 | 日期 | 结论 |
|---|---|---|---|
| 批准范围变更 | Kimi Code（项目经理，受用户授权） | 2026-07-21 | 批准 |
| 接受入口 pseudonym 化改变 Domain State | Kimi Code（项目经理，受用户授权） | 2026-07-21 | 接受 |
| 接受临床白名单反转与 P2 误脱敏 | Kimi Code（项目经理，受用户授权） | 2026-07-21 | 接受 |
| 批准 `context.py` 拆分 | Kimi Code（项目经理，受用户授权） | 2026-07-21 | 批准 |
| 总体结论 | Kimi Code（项目经理，受用户授权） | 2026-07-21 | 批准，进入实施批次 A～F |

## 11. 关联文档

- `docs/01_agent部分优化/L4.5-11收敛性架构重写方案-2026-07-21.md`
- `docs/01_agent部分优化/Agent优化任务进度表.md`
- `docs/01_agent部分优化/L5进入前专业安全预审报告-2026-07-19.md`
- `docs/01_agent部分优化/Agent整体大修实施计划-LangGraph版.md`
- `docs/01_agent部分优化/adr/ADR-001-adopt-langgraph.md`
- `docs/01_agent部分优化/adr/ADR-002-domain-state-and-graph-state-boundary.md`
