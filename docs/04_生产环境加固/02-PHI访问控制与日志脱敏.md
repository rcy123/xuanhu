# 阶段 2 · PHI 访问控制 + 日志脱敏加固

> 状态：已实施（待安全评审签字后上线）
> 上线阻断级别：**must**
> 前置依赖：阶段 1（医师身份需由可信 JWT 提供）
> 关联缺陷：H4 / H7 / M6

---

## 1. 目标

中医四诊（问诊对话、舌脉、症状描述）在《个人信息保护法》与卫健委口径下属于**敏感个人信息（含健康信息）**。阶段 2 确保两点：

1. **纵向隔离**：医师只能访问自己负责的会话；跨医师访问需显式授权并留审计。
2. **不出域**：日志、错误响应、健康检查、模型网关请求体中不出现可还原的患者 PHI。

**完成定义**：

1. 任意路由泄露他人会话数据 → 返回 `403 FORBIDDEN`，写 `audit.access_denied` 事件。
2. 全 `logger.*` 调用经审计不存在明文患者姓名、症状原文、舌脉描述。
3. 错误响应（`detail` 字段）不再暴露内部结构化细节（如 `session_id=... payload_digest_mismatch`）。
4. SSE `session_id` 强制 UUID 格式校验，非法格式 → `400 VALIDATION_ERROR`。

---

## 2. 访问控制：会话所有权模型

### 2.1 数据模型扩展

复用阶段 1 新增的 `doctors` 表。在 `consult_sessions` 上增加字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `doctor_id` | UUID FK→doctors.id NOT NULL | 会话负责医师，创建时由 JWT claim 写入，非客户端可改 |

Alembic 迁移为存量会话回填 `doctor_id`（取 `state_snapshot.doctor_id` 或统一回填到迁移操作者）。

### 2.2 所有权校验中间件

新建 `app/core/access.py`：

```python
async def require_session_owner(
    session_id: str,
    doctor: DoctorPrincipal = Depends(get_current_doctor),
    db: AsyncSession = Depends(get_db),
) -> None:
    """校验当前 token 医师是该会话的负责医师。否则 403。"""
```

注入点：所有以 `sessions/{session_id}` 为路径前缀的写接口（advance、review、record、recovery、safety_confirmations、messages）以及 SSE stream。

读接口（`GET /sessions`、`GET /sessions/{id}`、`GET /sessions/{id}/messages`）的过滤：

- 列表查询自动加 `WHERE doctor_id = :current_doctor_id`，不靠客户端不传就越权。
- 单会话详情查询命中他人会话 → `404 SESSION_NOT_FOUND`（**不返回 403**——避免枚举型信息泄露：不暴露"该会话存在但不属于你"）。

### 2.3 跨医师访问（MVP 不实现，仅留设计）

院内"会诊"场景需要他人查看某会话。MVP 不开放，预留扩展点：

- 新增 `session_access_grants` 表（`session_id`, `granted_doctor_id`, `granted_by`, `expires_at`）。
- `require_session_owner` 改为 `require_session_access`，校验"负责医师 OR 有效 grant"。
- 授予动作单独路由 `POST /sessions/{id}/grants`，写 `audit.access_granted`。

本阶段只实现"负责医师"分支，grant 表不建。设计预留给后续阶段，避免阶段 2 范围膨胀。

### 2.4 审计事件

新增审计事件类型（写入现有 `audit_events` 表，复用 `AuditEvent` 模型）：

| event_type | 触发时机 | 关键字段 |
|---|---|---|
| `access.denied` | 所有权校验失败 | `session_id`, `attempted_doctor_id`, `path`, `reason` |
| `session.accessed` | 成功访问他人会话（含 grant 场景，本期不触发） | `session_id`, `accessor_id`, `via` |

`access.denied` 含敏感度低但仍需脱敏：`path` 只记路径模板（`/sessions/{id}/advance`），不记真实 session_id；session_id 单独成列。

---

## 3. 日志脱敏加固

### 3.1 现状盘点

审计发现以下日志热点（不含 PHI 的纯技术日志已豁免）：

| 文件 | 行 | 风险 |
|---|---|---|
| `app/api/advance.py:905-910` | 异常日志含 `session_id`、`str(exc)` | 异常消息可能由下游拼接患者信息 |
| `app/services/recovery.py` | 恢复日志 | 可能含会话快照字段 |
| `app/services/review.py` | 审核日志 | 可能含处方药味 |
| `app/agent_runtime/repository.py` | 仓储异常 | SQL 约束冲突可能含字段值 |

### 3.2 脱敏策略

**三层防护**：

1. **过滤器层（首选）**：在 logging 配置中挂一个 `PHIRedactingFilter`，对每条日志 message 做正则替换：
   - 匹配 `session_id=...`、`patient.*=...`、症状关键词 → 替换为 `[REDACTED]`。
   - 维护一份 `app/core/log_filter.py` 的 PHI 关键词/模式表，覆盖已知字段名。
2. **调用点层（兜底）**：review 所有 `logger.*` 调用，凡是把 `model_dump()` / 业务对象 str 化入参的，改为只记摘要（`type(exc).__name__` + `id`）。
3. **结构化字段层**：审计日志一律走 `AuditEvent` 表的 JSON payload，不进 logger；logger 只记"audit written: type=X trace=Y"。

> 优先级：第 2 层是真正可靠的（不依赖正则覆盖），第 1 层是纵深防御（防止新增日志漏改）。两层都做。

### 3.3 异常 detail 收敛

当前 `app/core/exceptions.py` 的自定义异常大量用 `detail=f"session_id={x} ..."` 向客户端回显。改为：

- 客户端响应只回 `code` + `message` + `retryable` + `trace_id`（已有）。
- `detail` 字段从**响应体移除**，仅用于服务端日志与审计表。
- 仅保留 `trace_id` 给客户端用于排查（trace_id 不含 PHI）。

这一改动需逐个文件改 `*_exception_handler` 把 `detail` 从 JSON 移除（共 7 个路由文件、约 30 个 handler）。回归风险：前端目前可能依赖 `detail` 显示给医师——review 前端代码确认 `detail` 不用于业务展示（仅作错误诊断），即可安全移除。

### 3.4 不入日志的白名单 + 强校验

新增 `tests/test_no_phi_in_logs.py`：对一组模拟患者数据跑完整问诊流程，捕获日志输出，断言不含患者姓名、症状原文 token。该测试作为阶段 2 验收的硬门禁，防止后续回归。

---

## 4. SSE session_id 输入校验（H7）

`app/api/stream.py` 当前直接把 `session_id` 透传给 `ensure_session_exists`，没做格式校验。改造：

```python
def _validate_session_id(session_id: str) -> None:
    try:
        uuid.UUID(session_id)
    except ValueError:
        raise ValidationError(message="session_id 格式非法", ...)
```

所有以 `{session_id}` 路径参数的接口统一走该校验（提取到 `app/api/request_context.py` 复用）。这样既堵了 SSE 的潜在 SSRF/路径遍历输入，也统一了全项目 session_id 校验口径。

---

## 5. 实现拆解

| ID | 任务 | 文件 | 估时 |
|---|---|---|---|
| T2.1 | `doctors` 表关联 + `consult_sessions.doctor_id` 迁移 + 存量回填 | `alembic/`, `app/models/` | 0.5d |
| T2.2 | `app/core/access.py` 所有权校验中间件 | `app/core/access.py` | 0.5d |
| T2.3 | 全路由注入所有权校验 + 列表查询加 doctor 过滤 | `app/api/*.py` | 1d |
| T2.4 | `access.denied` / `session.accessed` 审计事件 | `app/audit/`, `app/api/` | 0.5d |
| T2.5 | `PHIRedactingFilter` 日志过滤器 + 关键词表 | `app/core/log_filter.py` | 0.5d |
| T2.6 | 全 `logger.*` 调用点 review 改摘要 | 16 个文件 | 1d |
| T2.7 | 异常 detail 从响应体移除，仅留服务端 | 7 路由文件 ~30 handler | 0.5d |
| T2.8 | session_id UUID 校验提取复用 | `app/api/request_context.py` | 0.25d |
| T2.9 | `test_no_phi_in_logs.py` 端到端测试 | `tests/` | 0.5d |

---

## 6. 前端联动

阶段 2 前端改动较小，但要点：

1. 前端不再持久化任何患者 PHI 到 localStorage（当前业务里前端不应存）；review `useSessionDetail` / `useMessages` 的缓存策略，确认仅在内存。
2. 错误响应 schema 变化（`detail` 移除）——前端 `ErrorBanner` 组件改为只展示 `message`，不依赖 `detail`。
3. 403 时前端友好提示"无权访问该会话"，不暴露会话存在与否。

---

## 7. 验收

- [ ] A 医师带自己的 token 访问 B 医师会话的 advance/stream/messages → `404`（列表）/ `403`（写）。
- [ ] `audit.access_denied` 表记录被拦截的访问，含 attempted_doctor_id、path 模板。
- [ ] 跑端到端问诊流程，全量日志 grep 不出患者姓名与任一症状关键词。
- [ ] 错误响应体 JSON 不含 `detail` 字段（仅服务端日志有）。
- [ ] SSE 传 `session_id=../../etc` → `400 VALIDATION_ERROR`。
- [ ] `test_no_phi_in_logs.py` 通过。

---

## 8. 风险与回退

| 风险 | 缓解 |
|---|---|
| 所有权校验导致历史会话无法访问（doctor_id 回填错） | 迁移前导出 `consult_sessions` 备份；回填脚本 dry-run；灰度期允许"无 owner 会话任何登录医师可访问"过渡态 24h |
| 日志过滤器误伤正常技术字段（记丢诊断信息） | 灰度期过滤器先"标记不替换"（加 `[PHI?]` 前缀而非 `[REDACTED]`），人工 review 一周后切换为替换模式 |
| 移除 detail 影响前端展示 | 阶段 1 联调时同步改前端；先在预发布验证 |
| 审计表写入失败导致访问被拒（审计不可用就 fail-open 还是 fail-closed？） | **fail-open**：审计写入失败时记一条 logger.error 但放行请求，避免审计基础设施抖动连累临床业务。这是安全与可靠的权衡，需安全评审人认可 |

回退：所有权校验通过 `XUANHU_ACCESS_ENABLED` 开关灰度，与阶段 1 的 `XUANHU_AUTH_ENABLED` 独立。
