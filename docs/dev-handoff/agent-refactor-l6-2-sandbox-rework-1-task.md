# L6-2-R1 病历一致性验证限定返工（bytes/str 快照双重序列化）

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 发布日期 | 2026-07-24 |
| 基线 | `d8dfa44`（L6-2 第 1 次交付） |
| 依赖 | L5-PREP-0、L5-1、L5-2、L5-3、L5-4、L6-1 全部 accepted；L6-2 第 1 次交付 `d8dfa44` 被 `ACC-20260724-050R` / `DEC-20260724-045` 撤回 |
| 阻塞 | 无活跃工程阻塞 |
| 交付文件 | `docs/dev-handoff/agent-refactor-l6-2-sandbox-rework-1-task.md`（本文件） |

## 返工根因（来自 DEC-20260724-045）

L6-2 第 1 次交付的 16 项测试、L6-1+L5 回归 `204 passed`、校准全量 `1813 passed, 366 deselected`、Ruff/mypy/lock 全部通过。PM 在原始测试通过后进行 10 项 adversarial 独立探针，发现 2 个 P2 finding。

### P2-1: verifier 对 bytes/str 快照双重序列化（fail-closed 拒绝合法输入）

- 位置：`app/agent_runtime/sandbox_record.py` verifier `_verify` 第 107-108 行
- 缺陷代码模式：
  ```python
  if isinstance(recheck_snapshot, SandboxRecheckSnapshotV1):
      snapshot = recheck_snapshot
  else:
      snapshot = SandboxRecheckSnapshotV1.model_validate_json(
          canonical_review_bytes(recheck_snapshot), strict=True
      )
  ```
- 缺陷机制：`canonical_review_bytes(value)` 对任意对象调用 `json.dumps(_json_ready(value), ...).encode("utf-8")`。当 `value` 已是 `bytes`（例如 `canonical_review_bytes(snapshot)` 的输出）时，`json.dumps` 把 bytes 当作普通字符串序列化，产生 `"7b2261223a317d"` 这种 JSON 字符串字面量（不是原始对象）。`model_validate_json` 收到的是字符串字面量而非对象，报 `Input should be an object [type=model_type]` → 被 `except Exception: return False` 吞掉。
- 关键洞察：`canonical_review_bytes` **不是幂等的**。`canonical_review_bytes(canonical_review_bytes(x))` ≠ `canonical_review_bytes(x)`。
- 方向：fail-closed（拒绝合法输入而非放行非法输入），是功能缺陷而非安全漏洞，但违反 verifier 类型签名 `recheck_snapshot: object` 暗示的多态输入契约。

### P2-2: assembler 和 verifier 共享同一段双序列化代码，缺陷传播

- 位置：assembler `_build_record` 第 252-253 行、verifier `_verify` 第 107-108 行
- 事实：两处使用完全相同的 `isinstance`/`canonical_review_bytes`/`model_validate_json` 模式。assembler 的 L6-1 测试只打了 `SandboxRecheckSnapshotV1` 实例和 `dict` 两条路径，bytes/str 路径从未被测试。
- 在 assembler 中，bytes/str 双序列化失败表现为 `SandboxRecordError`（fail-closed），与 verifier 的 `False` 等价。
- 修复必须同时覆盖两个模块。

### 测试盲区

- L6-2 的 16 个测试全部只传 `SandboxRecheckSnapshotV1` 实例给 `verifier.verify`。
- L6-1 的 12 个测试只打 assembler 的实例和 dict 两条路径。
- 没有输入类型矩阵测试覆盖 bytes / str / dict / garbage 路径。

### 不构成 finding 的项

- A6（formula reorder → reject）：经分析，测试 fixture 的 `formula_items` 只有一个 item，`tuple(reversed(...))` 反转后仍只有一个 item，tuple 内容相同。当 formula_items 有 ≥2 个 item 时，verifier 的 tuple 比较是顺序敏感的，会正确拒绝。**A6 是探针 fixture 约束，不是 verifier 缺陷。**

## 目标

在个人学习、非临床、仅合成数据沙盒范围内，修复 L6-2 第 1 次交付的两个 P2 缺陷，并补输入类型矩阵测试。

具体目标：

1. 修复 verifier 和 assembler 的 bytes/str 快照双重序列化：
   - `SandboxRecheckSnapshotV1` 实例 → 直接使用
   - `bytes` → `model_validate_json(recheck_snapshot, strict=True)`（Pydantic 接受 bytes）
   - `str` → `model_validate_json(recheck_snapshot, strict=True)`（Pydantic 接受 str）
   - `dict` → `model_validate(recheck_snapshot, strict=True)`
   - 其他 → fail-closed（verifier 返回 `False`，assembler 返回 `None` → `SandboxRecordError`）
2. 不破坏 L6-1 已 accepted 的 DTO/Assembler 核心逻辑（assembler 的修复仅限于 else 分支的输入处理，不动 DTO 派生、字段提取、record 构建逻辑）
3. 不破坏 L6-2 已交付的 verifier 字段比较逻辑（仅修复输入解析分支）
4. 补输入类型矩阵测试，覆盖 verifier 和 assembler 的 instance / dict / bytes / str / garbage 五类输入

## 非目标

- 不实现 persistence / 幂等落盘（属于 L6-3）
- 不实现 narration / 文本润色（属于 L6-4）
- 不修改 L6-1 已 accepted 的 `SandboxMedicalRecordData` DTO 定义、`_record_id`、`_revision_id`、`_digest`、`SandboxRecordAssembler.assemble` 的公共签名和字段提取逻辑
- 不修改 L6-2 已 accepted 的 verifier 字段比较逻辑（review_confirm_ref、session_id、reviewed_formula、safety_result、revision_id、record_id 比较）
- 不接入真实 LangGraph `Command`、Runtime、HTTP、容器、部署、DB、RAG、Gateway 或外部服务
- 不连接真实患者数据、真实病历、真实知识库或生产模型日志
- 不声称临床有效、医疗安全、法规合规或获得专业批准

## 允许修改范围

只允许修改/新增以下文件，全部 tracked：

1. `sandbox_record.py` — 仅修改 verifier `_verify` 和 assembler `_build_record` 的 else 分支输入处理（不修改 DTO、`_record_id`、`_revision_id`、`_digest`、`SandboxRecordAssembler.assemble` 的公共签名和字段提取逻辑、verifier 的字段比较逻辑）
2. `tests/test_sandbox_record_l6_2.py` — 补输入类型矩阵测试；保留原有 16 项测试
3. `docs/dev-handoff/agent-refactor-l6-2-sandbox-rework-1.md` — R1 交付 handoff

允许从 `sandbox_review.py`、`sandbox_recheck.py` 读取已 accepted 的类型和常量（只读引用，不修改）。

## 禁止修改范围

- 禁止修改 `sandbox_record.py` 中 L6-1 已验收的 `SandboxMedicalRecordData`、`_record_id`、`_revision_id`、`_digest`、`SandboxRecordAssembler` 的公共签名和字段提取逻辑
- 禁止修改 L6-2 已交付的 verifier 字段比较逻辑（仅允许修改输入解析分支）
- 禁止修改 `sandbox_safety.py`（L5-1）、`sandbox_explanation.py`（L5-2）、`sandbox_review.py`（L5-3）、`sandbox_recheck.py`（L5-4）的任何代码、测试或 handoff
- 禁止修改 `pyproject.toml`、`README.md`、配置、依赖、前端、Legacy、Runtime、DB、Gateway、PM 台账
- 禁止修改 L0～L5 任何已验收的管理文档、验收记录、决策记录
- 禁止读取 `.env`、ignored `data/` 或任何外部存储
- 禁止网络调用、子进程、文件写入

## 先红后绿要求

1. 在未修复生产代码时，以真实 RED 证明以下缺口：
   - verifier 接受 bytes 快照时返回 `False`（应为 `True`）
   - verifier 接受 str 快照时返回 `False`（应为 `True`）
   - assembler 接受 bytes 快照时 raise `SandboxRecordError`（应成功）
   - assembler 接受 str 快照时 raise `SandboxRecordError`（应成功）
2. 修复后 GREEN 必须覆盖输入类型矩阵：
   - verifier: instance → True（合法）、dict → True、bytes → True、str → True、garbage → False、None → False
   - assembler: instance → record、dict → record、bytes → record、str → record、garbage → `SandboxRecordError`、None → `SandboxRecordError`
   - 保留原有 16 项 GREEN 测试全部通过
   - 保留原有字段篡改拒绝测试全部通过（formula/confirm_ref/safety_result/decision/injected field）

## 验收标准

### 独立 Review
- P0/P1/P2/P3 全为 0
- P2-1（bytes/str 双序列化）和 P2-2（assembler 同名缺陷）已关闭
- 不修改 accepted L5/L6-1 代码的前提下，L6-2-R1 模块独立可测

### 独立 CI
- L6-2 专项测试全部通过（原 16 项 + 新增输入类型矩阵）
- L6-1 专项 `12 passed`（assembler 修复不得破坏 L6-1 测试）
- L5-1/2/3/4 回归专项全部通过（`14/18/84/60`）
- Safety `71/3 deselected`、privacy `76`、L0 `131`、Ruff/mypy/lock 全通过
- 校准全量 `1813 passed, 366 deselected`（或当前基线等价）
- scope/tracked/diff/exact/clean 全通过

### PM 探针
- 输入类型矩阵定向探针（verifier 和 assembler 各 6 项）：
  1. verifier instance → True
  2. verifier dict → True
  3. verifier bytes → True
  4. verifier str → True
  5. verifier garbage → False
  6. verifier None → False
  7. assembler instance → record
  8. assembler dict → record
  9. assembler bytes → record
  10. assembler str → record
  11. assembler garbage → `SandboxRecordError`
  12. assembler None → `SandboxRecordError`
- 原始 5 项字段篡改探针仍全部通过

## 停止条件

- 任何修改超出允许文件范围 → 停止，重新发布
- 任何真实患者/临床数据进入测试 → 立即停止
- 需要修改 L5/L6-1 代码或 L6-2 字段比较逻辑才能通过 → 停止，发布对应 rework 而非在当前任务中修复
- 发现 P0/P1 → 停止交付，发布 bounded rework

## 记录要求

1. 开发交付时更新 `agent-refactor-l6-2-sandbox-rework-1.md` handoff
2. 不得由开发交付声明替代 PM 验收
3. 验收通过后，PM 追加 `ACC-YYYYMMDD-NNN` 验收记录、更新任务台账和当前状态

## 状态边界

- 本任务发布不等于 L6 完成，也不等于 L6-3/L6-4/L7 授权
- L6-2-R1 完成后由 PM 另行发布 L6-3
- 真实临床、患者服务、公开生产继续 NO-GO
- G1～G6、EXT-001、EXT-002 继续 `deferred_for_clinical_use`
