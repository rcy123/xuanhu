# L2-2 AgentSpec、RunSpec 与 AgentRuntime

## AR-B-010 第 1 轮限定返工

- 新增 `tests/test_l2_2_agent_runtime.py`：全部使用可计数 fake gateway 与 fake recorder，不调用真实模型。覆盖协议边界、调用前后 schema 拒绝、版本、透传、Artifact、重试、预算、deadline/timeout、取消、recorder 脱敏与无权威状态接口。
- `FailurePolicy.retryable_codes` 改为稳定的 `RuntimeErrorCode` 集合。Runtime 仅在固定错误分类可重试、该 code 被 FailurePolicy 列出、仍有 `total_attempt_budget` 且 run deadline 未耗尽时重试；`ModelGatewayUnavailableError.retryable` 只作为网关分类附加条件，不能单独触发重试。
- Runtime 自建的 `ModelGatewayClient(max_retries=0)` 仅供 L2-2 使用，不改变 Legacy BaseAgentImpl。`ModelGatewayClient.chat_structured(max_requests=1)` 是公开的可测试请求上限：它限制 transport retry 且禁用 JSON fallback 的第二个请求。
- recorder 采用最佳努力降级：其同步或异步异常被吞掉，不影响 run，也不向外暴露异常文本。其 event data 仅有 run/session、spec/prompt 版本、模型、尝试数、延迟、trace 与固定错误码；不含 messages、input_payload、prompt、raw output、API key 或患者身份。

## AR-B-011 第 2 轮限定返工

- recorder 现在是有界最佳努力操作。同步 `record` 在工作线程执行，避免阻塞事件循环；异步 record 在受限 task 中等待。异常和超时均静默降级，既不进入 RuntimeError、异常链或 recorder event data，也不改变成功、失败或取消的主结果。
- `RECORDER_TIMEOUT_SECONDS = 0.05`。`started` 与 `succeeded` recorder 的等待上限是 `min(0.05s, remaining_run_deadline)`；若 started recorder 因其较短的剩余 deadline 超时，Runtime 立即以 `RUN_DEADLINE_EXCEEDED` 停止，绝不启动 gateway。
- `RECORDER_FINALIZATION_TIMEOUT_SECONDS = 0.05`。`failed` 与 `cancelled` 使用这一独立且短的上限，不显著延迟主异常或 `CancelledError` 的传播。正常可取消的阻塞 recorder task 会被取消并消费结果，不留下未消费异常。
- `tests/test_l2_2_agent_runtime.py` 新增永久未完成 `asyncio.Event` recorder 测试：started 零 gateway 调用、succeeded 保持成功 Artifact、failed 保持固定 code、cancelled 原样传播；另验证含 API key/prompt/患者身份的 recorder 异常不进入主错误链或事件数据。

## AR-B-012 第 3 轮限定返工

- `RuntimeRunRecorder.record` 已收紧为 async-only 协议。同步 recorder 是配置/编程错误：`AgentRuntime` 构造时直接抛出固定的 `RECORDER_ASYNC_REQUIRED`，不调用 `record`，不读取或传播 recorder 异常文本，也不创建线程、executor work 或 runtime recorder task。
- 已移除 `asyncio.to_thread`。Runtime 不使用 `run_in_executor`、后台线程或 daemon thread 包装同步 recorder，因此同步副作用不能逃逸 run 生命周期。
- 异步 recorder 保留 AR-B-011 的有界最佳努力语义。永久阻塞 task 会被取消、消费结果，并以命名 `agent-runtime-recorder` task 回归断言验证 run 结束后不存在 pending task。
- 专项测试使用 `threading.Event` 证明同步 recorder 函数体从未运行，并保留 started/succeeded/failed/cancelled 四类永久阻塞异步 recorder 覆盖。

## 精确预算定义

- 一个 **Runtime attempt** 是 Runtime 发起的一次 `chat_structured` 操作。
- 一个 **gateway request** 是一次模型 HTTP 请求。
- 对 Runtime 自建的 ModelGatewayClient，每个 Runtime attempt 都传入 `max_requests=1`，所以严格等于一个 gateway request；transport retry 与 structured JSON fallback 都不能增加请求。
- 因而实际模型 HTTP 请求总数不超过 `min(RunSpec.total_attempt_budget, ModelPolicy.max_attempts)`，自然也不超过 `RunSpec.total_attempt_budget`。注入 fake gateway 时同一调用也代表一个可计数请求，并在专项测试中验证该上限。
- deadline 是整次 run 的总截止时间。每次启动 gateway 前检查；单次调用以 `min(timeout_seconds, remaining_deadline)` 包裹。deadline 耗尽后不启动下一次调用。`CancelledError` 原样传播并记录 `cancelled`。

## 测试结果

- `uv run pytest tests/test_l2_2_agent_runtime.py -q -rs`：23 passed
- `uv run pytest tests/test_gateway.py tests/test_base_agent.py -q -rs`：36 passed
- `uv run pytest -q -rs`：1108 passed，1 xfailed（6 个既有 warning）
- `uv run ruff check .`：通过；默认与 Python 3.11 双版本 mypy：各 87 files 无问题；`uv lock --check` 与 `git diff --check`：通过。

本任务未实现 ContextBuilder、Prompt 分层或隐私投影、Verifier/Reducer、Repository/outbox、业务 Agent、MainGraph/API/SSE/前端接入或切流。
