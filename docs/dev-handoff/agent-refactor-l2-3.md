# L2-3 ContextBuilder、Prompt 分层与隐私投影

## 变更文件

- `app/agent_runtime/context.py`：不可变 Pydantic 合约、白名单投影、统一递归隐私投影、运行时密钥伪名化、模板严格校验、token 预算。
- `app/agent_runtime/context_builder.py`：兼容导入入口。
- `app/agent_runtime/__init__.py`：公开 L2-3 合约。
- `tests/test_l2_3_context_builder.py`：fake/local 专项测试。

## 边界与策略

ContextBuilder 是纯函数式边界，不读取或修改 Domain State、Graph State、Safety 结果，不调用模型、数据库、API 或日志。消息层固定为 `system -> developer -> context -> user`，用户内容只能进入 user 层。

仅允许构造时给出的顶层字段进入投影；Mapping、嵌套 Mapping、列表和裸字符串 context 都经同一个递归投影边界。手机号/证件号模式在任意自由文本中替换为 `[REDACTED]`。身份字段替换为稳定、不可逆的 HMAC 伪名；伪名密钥只能由运行时 `pseudonym_key` 注入或由 `PseudonymKeyProvider` 提供，缺失或空密钥会以 `PseudonymKeyUnavailable` 确定性拒绝，源码中没有固定 HMAC key、普通 hash 或明文回退。普通事件不接收 ContextPacket、完整 Prompt 或模型输出。

模板只接受简单变量名，缺失、未知、未授权变量均确定性拒绝；不允许属性/索引访问。token 预算使用稳定的保守字符估算，默认超限拒绝，也支持显式的确定性截断策略。

## 测试

专项：`uv run pytest tests/test_l2_3_context_builder.py -q -rs`

AR-B-013 验收结果：专项 9 passed；L2-2 23 passed；`uv run ruff check .` 通过；`uv run mypy app` 通过（89 files）；`uv lock --check` 通过；`git diff --check` 通过（仅 Git 的 LF/CRLF 提示）。此前 L2-3 全量结果为 1112 passed、1 xfailed、6 warnings；本次限定返工未运行全量测试。

## 未完成项与风险

尚未将 Legacy Agent 的现有 Prompt 生成路径切换到该边界，避免违反兼容性要求；也未实现 Verifier、Reducer、Repository、Outbox 或真实模型调用。生产接入时必须由密钥管理系统实现 `PseudonymKeyProvider`，不得把密钥写入配置文件、日志或源码。token 估算不是具体模型 tokenizer，接入模型时应以目标模型 tokenizer 校准预算。
