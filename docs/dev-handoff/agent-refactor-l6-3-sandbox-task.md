# L6-3 病历持久化（Sandbox）

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 发布日期 | 2026-07-24 |
| 基线 | `d8dfa44`（L6-2 验收提交） |
| 依赖 | L5-PREP-0、L5-1、L5-2、L5-3、L5-4、L6-1、L6-2 全部 accepted |
| 阻塞 | 无活跃工程阻塞 |
| 交付文件 | `docs/dev-handoff/agent-refactor-l6-3-sandbox-task.md`（本文件） |

## 目标

在个人学习、非临床、仅合成数据沙盒范围内，实现病历子图的第三层：**确定性 in-memory 持久化与幂等落盘边界**。

具体目标：

1. 实现 `SandboxRecordStore`（in-memory、确定性、幂等）：
   - `put(record)` 对同一 `record_id` 幂等：相同 record 重复写入不报错、不增加版本
   - 相同 `record_id` 但字段不同 → 固定拒绝（`SandboxRecordError`）
   - `get(record_id)` 返回已存 record 或固定失败（无 None 渗透、无异常穿透）
   - 不持有 clock、random、network、DB、文件句柄；`__slots__ == ()`
2. 实现确定性落盘边界（**不接触真实磁盘/DB**）：
   - 提供一个纯函数化的 `serialize_record(record) -> bytes`（canonical JSON，字节稳定）
   - 同一 record 多次序列化 → 字节级相同
   - 不写入真实文件、不调用 `open`、不连接 DB、不发起网络调用
3. 验证边界：
   - 幂等 put（同 record 重复）→ 不报错、版本不变
   - 篡改后同 record_id put → 固定拒绝
   - get 不存在 record_id → 固定失败（chainless、payload-free）
   - 序列化确定性 → 同输入字节级相同
4. 建立 L6-3 专项测试：
   - 幂等 put → 通过
   - 篡改后同 id → 固定拒绝
   - get miss → 固定失败
   - 序列化字节稳定

## 非目标

- 不实现 narration / 文本润色（属于 L6-4）
- 不实现最终组合 / 跨层集成（属于 L6-4）
- 不修改 L6-1 已 accepted 的 DTO/Assembler 核心逻辑
- 不修改 L6-2 已 accepted 的 Verifier 核心逻辑（只允许在 `sandbox_record.py` 内新增 store/serialize 类）
- 不接入真实 LangGraph `Command`、Runtime、HTTP、容器、部署、DB、RAG、Gateway 或外部服务
- 不连接真实患者数据、真实病历、真实知识库或生产模型日志
- 不生成真实临床诊断、治疗建议、处方或医疗决策
- 不修改 accepted L5-1/L5-2/L5-3/L5-4、L6-1/L6-2 生产代码、handoff 或验收记录
- 不修改 Legacy engine/review/record、配置、依赖、前端、UI 或部署
- 不声称临床有效、医疗安全、法规合规或获得专业批准

## 允许修改范围

只允许修改/新增以下文件，全部 tracked：

1. `sandbox_record.py` — 在现有文件内新增 `SandboxRecordStore` 与 `serialize_record` 纯函数（不修改 DTO/Assembler/Verifier 部分）
2. `tests/test_sandbox_record_l6_3.py` — L6-3 唯一专项测试
3. `docs/dev-handoff/agent-refactor-l6-3-sandbox.md` — 交付 handoff

允许从 `sandbox_record.py` 自身、`sandbox_review.py`、`sandbox_recheck.py` 读取已 accepted 的类型和常量（只读引用，不修改）。

## 禁止修改范围

- 禁止修改 `sandbox_record.py` 中 L6-1 已验收的 `SandboxMedicalRecordData`、`SandboxRecordAssembler` 与 L6-2 已验收的 `SandboxRecordConsistencyVerifier` 的任何代码
- 禁止修改 `sandbox_safety.py`（L5-1）、`sandbox_explanation.py`（L5-2）、`sandbox_review.py`（L5-3）、`sandbox_recheck.py`（L5-4）的任何代码、测试或 handoff
- 禁止修改 `pyproject.toml`、`README.md`、配置、依赖、前端、Legacy、Runtime、DB、Gateway、PM 台账
- 禁止修改 L0～L5 任何已验收的管理文档、验收记录、决策记录
- 禁止读取 `.env`、ignored `data/` 或任何外部存储
- 禁止网络调用、子进程、真实文件写入（专项测试的临时 in-memory store 除外；`serialize_record` 只返回 bytes，不写盘）

## 先红后绿要求

1. 在未修改生产代码时，以真实 RED 证明以下缺口：
   - 无 store 时同 record_id 篡改后写入被接受（或无 store 类无法测）
   - 无 serialize 函数时序列化不确定（或无函数可调用）
2. 修复后 GREEN 必须覆盖：
   - 幂等 put（同 record 重复）→ 不报错、版本/计数不变
   - 篡改后同 record_id put → 固定拒绝（`SandboxRecordError`）
   - get 命中 → 返回原 record
   - get miss → 固定失败（chainless、payload-free）
   - `serialize_record` 同输入 → 字节级相同
   - `serialize_record` 不同输入 → 字节不同
   - store `__slots__ == ()`
   - AST 边界：无 `open/print/breakpoint/exec/eval/compile`、无 network/socket/http 调用

## 验收标准

### 独立 Review
- P0/P1/P2/P3 全为 0
- 不修改 accepted L5/L6-1/L6-2 代码的前提下，L6-3 模块独立可测

### 独立 CI
- L6-3 专项测试全部通过
- L6-1/L6-2 专项 `12 + 16 passed`
- L5-1/2/3/4 回归专项全部通过（`14/18/84/60`）
- Safety `71/3 deselected`、privacy `76`、L0 `131`、Ruff/mypy/lock 全通过
- 校准全量 `1813 passed, 366 deselected`（或当前基线等价）
- scope/tracked/diff/exact/clean 全通过

### PM 探针
- 五项定向探针：
  1. 幂等 put（同 record 重复）→ 不报错、版本不变
  2. 篡改后同 record_id put → 固定拒绝
  3. get 命中 → 返回原 record
  4. get miss → 固定失败（chainless、payload-free）
  5. `serialize_record` 同输入 → 字节级相同

## 停止条件

- 任何修改超出允许文件范围 → 停止，重新发布
- 任何真实患者/临床数据进入测试 → 立即停止
- 需要修改 L5/L6-1/L6-2 代码才能通过 → 停止，发布对应 rework 而非在当前任务中修复
- 发现 P0/P1 → 停止交付，发布 bounded rework

## 记录要求

1. 开发交付时更新 `agent-refactor-l6-3-sandbox.md` handoff
2. 不得由开发交付声明替代 PM 验收
3. 验收通过后，PM 追加 `ACC-YYYYMMDD-NNN` 验收记录、更新任务台账和当前状态

## 状态边界

- 本任务发布不等于 L6 完成，也不等于 L6-4/L7 授权
- L6-3 完成后由 PM 另行发布 L6-4
- 真实临床、患者服务、公开生产继续 NO-GO
- G1～G6、EXT-001、EXT-002 继续 `deferred_for_clinical_use`
