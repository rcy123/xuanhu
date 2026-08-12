# ADR-007：不引入额外 CSRF Token 机制（显式接受风险）

## 状态

已采纳（2026-08-13，阶段 4 运行态安全加固 M3）。

## 背景

CSRF（Cross-Site Request Forgery）攻击依赖浏览器自动携带目标站点凭据（通常是
Cookie）随第三方请求发送。评审了以下现状：

1. **认证形态**：登录后签发的 JWT 存放在前端 `sessionStorage`，请求时由前端
   代码显式放入 `Authorization: Bearer <token>` header（见 [client.ts](../../frontend/src/api/client.ts)
   与 [auth.ts](../../frontend/src/api/auth.ts)），**不使用任何 Cookie**。
2. **部署形态**：内网部署，前端与 API 同源（或经 Caddy/Nginx 反代同域），
   CORS 白名单只放行受控来源（M2）。
3. **既有纵深**：`Authorization` header 是天然 CSRF 防护——浏览器**不会**
   自动把该 header 附加到跨站请求，攻击者无法从第三方页面构造携带有效
   `Authorization` 的请求，CSRF 在 token-in-header 模型下基本免疫。
4. **SSE 例外**：`/sessions/{id}/stream` 因浏览器 EventSource 无法自定义
   header，token 走 query string（仅此一处例外，见 `require_stream_session_reader`）。
   query-string token 不会自动随跨站请求携带，不构成 CSRF 载体。

## 决策

**MVP 不实现额外的 CSRF token 机制。**

- token 仅走 `Authorization` header（与 SSE query-string 例外），不依赖
  Cookie 传递凭据 → CSRF 攻击面在传输层即被关闭。
- 不引入 double-submit cookie / synchronizer token / SameSite 等额外机制。
- **硬约束**：禁止未来把 token 切换到 Cookie 承载。若后续引入 SSO 单点登录
  Cookie 联动（`Set-Cookie` 传递会话），必须同时启用 CSRF token 机制并重新
  评估 CORS 与同源策略，本 ADR 随之更新或推翻。

## 决策依据

1. **威胁模型不成立**：无 Cookie 即无自动携带的凭据，CSRF 的触发前提消失。
2. **header 传输是行业标准**：Bearer token in Authorization header 是
   OAuth2/OIDC 的标准形态，天然免 CSRF（RFC 6749 §10.13 明确
   "the client must use the Authorization header to transmit credentials
   rather than cookies"）。
3. **实现成本**：额外 CSRF token 机制（生成、校验、轮换、失败处理）引入
   维护面，在当前威胁模型下无对应收益。

## 明确边界

### 允许

- 保留 `Authorization` header 与 SSE query-string token 两个承载点。
- CORS 白名单放行受控来源（M2），`allow_credentials=True`（前端 header
  携带凭据）时**禁止** `*` 通配来源。

### 不允许 / 未来禁止

- **不得**把 token 放入 Cookie（`Set-Cookie` / `document.cookie`）。
- **不得**在 CORS 中启用 `Access-Control-Allow-Origin: *` 与
  `allow_credentials` 的组合（浏览器规范直接拒绝，且会破坏同源边界）。
- **不得**在未引入 CSRF token 机制的情况下增加任何「浏览器自动携带」的
  凭据通道（Cookie、HTTP Basic、TLS client cert 等）。

### 与 M2（CORS）的关系

- CORSMiddleware 已按 `cors_allowed_origins` 白名单装配（[main.py](../../app/main.py)），
  通配符与 credentials 组合在装配期即 fail-fast（starlette 抛
  ValueError），从根上杜绝「放开 origin 后 token-in-header 被跨域读取」。

## 正面影响

- 免维护一套 CSRF 机制及其测试、轮换、审计。
- 前端请求路径保持单一（header 注入），`401 → 重新登录` 处理集中在
  `client.ts`。

## 风险与代价

1. **SSE query-string token 泄漏面**：token 可能出现在访问日志/代理日志。
   缓解：Nginx `log_format no_query` 已剥离 query string（阶段 3），token
   TTL 短，SSE 重建连接会重新签 token。
2. **未来形态漂移**：若后续引入 Cookie 会话，必须推翻本 ADR。缓解：本
   决策写入 ADR 并列为硬约束，代码评审中拦截任何新增 Cookie 写入路径。

## 验证方式

- 阶段 4 验收：CORS 白名单装配测试（`test_cors.py`）验证同源/白名单来源
  放行、非白名单来源无 CORS 头。
- 回归：`test_auth_protected_routes.py` 验证所有写接口在 `on` 模式要求
  `Authorization` header，缺失即 401——不存在可被浏览器自动携带的凭据通道。
