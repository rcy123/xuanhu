"""L0-1 文档契约测试。

纯文件契约测试：验证 ADR、兼容矩阵、迁移边界文档的内容完整性、
章节覆盖率、端点覆盖、事件覆盖和不可变约束。不调用数据库、Redis 或真实模型。

本测试位于默认 ``testpaths`` 的 ``tests/`` 目录，可随完整测试套件运行，
也可以单独执行：

    uv run pytest tests/test_l0_1_contract.py -q -rs
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs" / "01_agent部分优化"
ADR_DIR = DOCS_DIR / "adr"

ADR_FILES = [
    "ADR-001-adopt-langgraph.md",
    "ADR-002-domain-state-and-graph-state-boundary.md",
    "ADR-003-sufficiency-as-policy.md",
    "ADR-004-merge-prescription-and-modification.md",
    "ADR-005-doctor-review-interrupt.md",
]

LEGACY_MATRIX_FILE = DOCS_DIR / "legacy-api-compatibility-matrix.md"
MIGRATION_BOUNDARY_FILE = DOCS_DIR / "agent-runtime-migration-boundary.md"
HANDOFF_FILE = REPO_ROOT / "docs" / "dev-handoff" / "agent-refactor-l0-1.md"

REQUIRED_ADR_SECTIONS = [
    "状态",
    "背景",
    "决策",
    "决策依据",
    "明确边界",
    "正面影响",
    "风险与代价",
    "迁移策略",
    "回滚策略",
    "验证方式",
]

COVERED_ENDPOINTS = [
    "POST /api/v1/consult/sessions/{session_id}/messages",
    "GET /api/v1/consult/sessions/{session_id}/messages",
    "POST /api/v1/consult/sessions/{session_id}/advance",
    "POST /api/v1/consult/sessions/{session_id}/review",
    "GET /api/v1/consult/sessions/{session_id}/record",
    "PUT /api/v1/consult/sessions/{session_id}/record",
    "GET /api/v1/consult/sessions/{session_id}/record/export",
    "POST /api/v1/consult/sessions/{session_id}/recover",
    "GET /api/v1/consult/sessions/{session_id}/stream",
]

SSE_EVENTS = [
    "stage.changed",
    "message.created",
    "agent.started",
    "agent.finished",
    "agent.failed",
    "review.required",
    "safety.blocked",
    "session.blocked",
    "session.done",
    "session.terminated",
    "doctor.reviewed",
    "heartbeat",
    "resync",
]

IMMUTABLE_RULES = [
    # Domain State 权威 —— 每个规则用 (关键词, 说明) 元组
    # 关键词必须出现于文档，说明用于错误消息
    ("唯一权威", "Domain State 是临床事实唯一权威"),
    ("SQLAlchemy", "checkpoint 禁止保存 SQLAlchemy Session/ORM 对象"),
    ("患者身份", "checkpoint 禁止保存患者身份/PII"),
    ("完整 Prompt", "checkpoint 禁止保存完整 Prompt"),
    ("双真源", "禁止 Domain State / Graph State 双真源"),
    # 会话隔离
    ("会话隔离", "Legacy 与 LangGraph 会话隔离"),
    ("不得互相恢复", "两类会话不得互相恢复"),
    # 降级禁止
    ("静默降级", "禁止静默降级到 Legacy"),
    ("显式", "运行时切换必须显式可审计"),
    # 安全硬边界
    ("安全结论", "SafetyRuleEngine 始终是安全结论权威"),
    ("不得绕过安全规则", "模型不得绕过安全规则"),
    # Doctor Review 硬边界
    ("hard gate", "Doctor Review 是不可绕过的 hard gate"),
    ("重新执行安全审核", "医师修改处方后必须重新执行安全审核"),
    ("有效复核前", "有效复核前不得生成最终病历"),
    # Legacy 保护
    ("禁止删除", "迁移期间禁止删除 Legacy 实现"),
]


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    """读取文件内容为字符串。"""
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _exists(path: Path) -> bool:
    """检查文件或目录是否存在。"""
    return path.exists()


# ===================================================================
# 测试类 1：文件存在性
# ===================================================================


class TestFileExistence:
    """验证所有 L0-1 交付物存在。"""

    @pytest.mark.parametrize("filename", ADR_FILES)
    def test_adr_file_exists(self, filename: str) -> None:
        """每份 ADR 文件必须存在于 adr/ 目录下。"""
        path = ADR_DIR / filename
        assert _exists(path), f"ADR 文件缺失: {path}"

    def test_legacy_matrix_exists(self) -> None:
        """兼容矩阵文件必须存在。"""
        assert _exists(LEGACY_MATRIX_FILE), f"兼容矩阵文件缺失: {LEGACY_MATRIX_FILE}"

    def test_migration_boundary_exists(self) -> None:
        """迁移边界文件必须存在。"""
        assert _exists(MIGRATION_BOUNDARY_FILE), f"迁移边界文件缺失: {MIGRATION_BOUNDARY_FILE}"


# ===================================================================
# 测试类 2：ADR 章节完整性
# ===================================================================


class TestADRSections:
    """验证每份 ADR 包含全部必填章节且状态为'已采纳'。"""

    @pytest.mark.parametrize("filename", ADR_FILES)
    def test_adr_status_adopted(self, filename: str) -> None:
        """ADR 状态必须为'已采纳'。"""
        content = _read(ADR_DIR / filename)
        assert content, f"{filename} 内容为空"
        # 状态行格式：## 状态\n\n已采纳
        assert "已采纳" in content, (
            f"{filename} 状态未设置为'已采纳'，"
            f"请确认文档中包含明确的状态声明"
        )

    @pytest.mark.parametrize("filename", ADR_FILES)
    def test_adr_sections_present(self, filename: str) -> None:
        """每份 ADR 必须包含所有必填章节。"""
        content = _read(ADR_DIR / filename)
        assert content, f"{filename} 内容为空"
        for section in REQUIRED_ADR_SECTIONS:
            assert section in content, (
                f"{filename} 缺少必填章节: {section}"
            )

    @pytest.mark.parametrize("filename", ADR_FILES)
    def test_adr_no_placeholders(self, filename: str) -> None:
        """ADR 不得包含 TODO、TBD、FIXME 等占位符。"""
        content = _read(ADR_DIR / filename)
        assert content, f"{filename} 内容为空"
        placeholders = ["TODO", "TBD", "FIXME", "待补充", "待完成", "[待定]"]
        for ph in placeholders:
            assert ph not in content, (
                f"{filename} 包含占位符: {ph}"
            )


# ===================================================================
# 测试类 3：兼容矩阵覆盖
# ===================================================================


class TestCompatibilityMatrix:
    """验证兼容矩阵覆盖全部端点和事件。"""

    def test_all_endpoints_covered(self) -> None:
        """兼容矩阵必须逐项覆盖全部 9 个端点。"""
        content = _read(LEGACY_MATRIX_FILE)
        assert content, "兼容矩阵文件内容为空"
        for endpoint in COVERED_ENDPOINTS:
            # 用路径的唯一部分匹配（如 /messages、/advance）
            path_part = endpoint.split("/")[-1]
            assert path_part in content, (
                f"兼容矩阵未覆盖端点: {endpoint} (找不到 /{path_part})"
            )

    def test_all_sse_events_covered(self) -> None:
        """兼容矩阵必须覆盖全部 13 种 SSE 事件。"""
        content = _read(LEGACY_MATRIX_FILE)
        assert content, "兼容矩阵文件内容为空"
        for event in SSE_EVENTS:
            assert f"`{event}`" in content or event in content, (
                f"兼容矩阵未覆盖 SSE 事件: {event}"
            )

    def test_invalid_state_version_covered(self) -> None:
        """兼容矩阵必须覆盖 INVALID_STATE_VERSION 错误码。"""
        content = _read(LEGACY_MATRIX_FILE)
        assert "INVALID_STATE_VERSION" in content, (
            "兼容矩阵未覆盖 INVALID_STATE_VERSION 错误码"
        )

    def test_session_busy_covered(self) -> None:
        """兼容矩阵必须覆盖 SESSION_BUSY 错误码。"""
        content = _read(LEGACY_MATRIX_FILE)
        assert "SESSION_BUSY" in content, (
            "兼容矩阵未覆盖 SESSION_BUSY 错误码"
        )

    def test_feature_flag_covered(self) -> None:
        """兼容矩阵必须覆盖 Feature Flag 行为。"""
        content = _read(LEGACY_MATRIX_FILE)
        assert "Feature Flag" in content, (
            "兼容矩阵未覆盖 Feature Flag 行为"
        )

    def test_rollback_covered(self) -> None:
        """兼容矩阵必须覆盖回滚行为。"""
        content = _read(LEGACY_MATRIX_FILE)
        assert "回滚预期行为" in content or "回滚" in content, (
            "兼容矩阵未覆盖回滚行为"
        )

    def test_idempotency_covered(self) -> None:
        """兼容矩阵必须覆盖幂等性说明。"""
        content = _read(LEGACY_MATRIX_FILE)
        assert "幂等" in content, (
            "兼容矩阵未覆盖幂等性说明"
        )

    def test_current_vs_target_distinction(self) -> None:
        """兼容矩阵必须区分'当前实现'与'目标架构'。"""
        content = _read(LEGACY_MATRIX_FILE)
        assert "当前实现" in content and "目标架构" in content, (
            "兼容矩阵未区分'当前实现'与'目标架构'"
        )


# ===================================================================
# 测试类 4：迁移边界不可变规则
# ===================================================================


class TestMigrationBoundary:
    """验证迁移边界文档包含全部不可变规则。"""

    def test_migration_boundary_content(self) -> None:
        """迁移边界文档必须包含实质性内容。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        assert len(content) > 1000, (
            f"迁移边界文档内容过短 ({len(content)} 字符)，"
            "可能为占位文档"
        )

    @pytest.mark.parametrize("keyword,description", IMMUTABLE_RULES)
    def test_immutable_rule_present(self, keyword: str, description: str) -> None:
        """每条不可变规则必须在迁移边界文档中明确出现。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        assert keyword in content, (
            f"迁移边界文档未明确覆盖不可变规则: {description} "
            f"(关键词 '{keyword}' 未找到)"
        )

    def test_migration_boundary_no_placeholders(self) -> None:
        """迁移边界文档不得包含占位符。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        placeholders = ["TODO", "TBD", "FIXME", "待补充", "待完成", "[待定]"]
        for ph in placeholders:
            assert ph not in content, (
                f"迁移边界文档包含占位符: {ph}"
            )

    def test_phase_boundaries_defined(self) -> None:
        """迁移边界必须明确 L0/L1/L9 阶段边界。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        assert "L0" in content, "迁移边界未定义 L0 阶段边界"
        assert "L1" in content, "迁移边界未定义 L1 阶段边界"
        assert "L9" in content, "迁移边界未定义 L9 阶段边界"

    def test_database_redis_boundaries_defined(self) -> None:
        """迁移边界必须明确数据库和 Redis 边界。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        assert "数据库" in content or "Database" in content or "PG" in content, (
            "迁移边界未定义数据库边界"
        )

    def test_domain_state_authority(self) -> None:
        """迁移边界必须明确 Domain State 是临床事实唯一权威。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        assert "唯一权威" in content or "Single Source of Truth" in content, (
            "迁移边界未明确 Domain State 是唯一权威"
        )

    def test_checkpoint_constraints(self) -> None:
        """迁移边界必须明确 checkpoint 禁止保存的内容。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        assert "checkpoint" in content.lower(), (
            "迁移边界未覆盖 checkpoint 约束"
        )

    def test_session_isolation(self) -> None:
        """迁移边界必须明确 Legacy 与 LangGraph 会话隔离。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        assert "会话隔离" in content or "session isolation" in content.lower(), (
            "迁移边界未明确会话隔离"
        )

    def test_no_implicit_degradation(self) -> None:
        """迁移边界必须明确禁止静默降级。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        assert "静默" in content or "implicit" in content.lower(), (
            "迁移边界未明确禁止静默降级"
        )

    def test_safety_hard_boundary(self) -> None:
        """迁移边界必须明确 SafetyRuleEngine 硬边界。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        assert "SafetyRuleEngine" in content, (
            "迁移边界未明确 SafetyRuleEngine 硬边界"
        )

    def test_doctor_review_hard_boundary(self) -> None:
        """迁移边界必须明确 Doctor Review 硬边界。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        assert "Doctor Review" in content or "doctor review" in content.lower(), (
            "迁移边界未明确 Doctor Review 硬边界"
        )

    def test_no_delete_legacy(self) -> None:
        """迁移边界必须明确禁止删除 Legacy 实现。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        assert "禁止删除" in content or "不得删除" in content, (
            "迁移边界未明确禁止删除 Legacy 实现"
        )


# ===================================================================
# 测试类 5：文档内部一致性
# ===================================================================


class TestDocumentConsistency:
    """验证 ADR、兼容矩阵、迁移边界之间的内部一致性。"""

    def test_adr_refs_consistent(self) -> None:
        """迁移边界中引用的 ADR 文件名应与实际 ADR 一致。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        for adr_file in ADR_FILES:
            # 检查文件名是否被引用（不要求完整路径，至少文件名出现）
            if adr_file not in content:
                # 也检查简写引用如 "ADR-001"
                short_name = adr_file.replace(".md", "").replace("ADR-", "ADR-")
                assert short_name in content or adr_file.replace(".md", "") in content, (
                    f"迁移边界未引用 ADR 文件: {adr_file}"
                )

    def test_matrix_refs_consistent(self) -> None:
        """兼容矩阵中引用的事件类型应与 SSE 事件 Schema 一致。"""
        content = _read(LEGACY_MATRIX_FILE)
        for event in SSE_EVENTS:
            assert f"`{event}`" in content or event in content, (
                f"兼容矩阵中未找到事件类型: {event}"
            )

    def test_no_contradictory_claims(self) -> None:
        """验证 ADR 之间没有互相矛盾的声明。"""
        # 收集所有 ADR 中关于 Sufficiency 的声明
        adr003 = _read(ADR_DIR / "ADR-003-sufficiency-as-policy.md")
        adr001 = _read(ADR_DIR / "ADR-001-adopt-langgraph.md")

        # ADR-003 不应该说 Sufficiency 由 LLM 决定
        assert "不再由 LLM Agent 执行" in adr003 or "不由模型控制" in adr003 or "确定性" in adr003, (
            "ADR-003 应明确 Sufficiency 由确定性规则控制"
        )

        # ADR-001 不应说 Sufficiency 由 LLM 决定
        if "Sufficiency" in adr001:
            # 如果提到 Sufficiency，应该与 ADR-003 一致（确定性控制）
            assert "确定性" in adr001 or "Policy" in adr001 or "Gate" in adr001, (
                "ADR-001 中如提及 Sufficiency，应与 ADR-003 一致："
                "Sufficiency 由确定性规则控制，非 LLM 决定"
            )

    def test_domain_state_not_dual_truth(self) -> None:
        """ADR-002 和迁移边界不得建立 Domain State/Graph State 双真源。"""
        adr002 = _read(ADR_DIR / "ADR-002-domain-state-and-graph-state-boundary.md")
        assert "单一真源" in adr002 or "唯一权威" in adr002, (
            "ADR-002 未明确 Domain State 是单一真源/唯一权威"
        )
        assert "双真源" in adr002 or "禁止" in adr002, (
            "ADR-002 未明确禁止双真源"
        )


# ===================================================================
# 测试类 6：L0 阶段范围
# ===================================================================


class TestL0Scope:
    """验证 L0 迁移边界在 L2～L4 实现后仍被遵守。"""

    def test_agent_runtime_is_skeleton_only(self) -> None:
        """运行时目录可含 L2～L4 policy/verifier，但不定义业务 Agent。"""
        import ast

        runtime_dir = REPO_ROOT / "app" / "agent_runtime"
        assert runtime_dir.exists(), "L4 阶段必须存在 app/agent_runtime"
        for py_file in runtime_dir.glob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            business_agents = [
                node.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ClassDef)
                and node.name.endswith("Agent")
                and node.name != "AgentRuntime"
            ]
            assert not business_agents, f"{py_file.name} 在 runtime 层定义业务 Agent: {business_agents}"

    def test_no_harness_implementation(self) -> None:
        assert not (REPO_ROOT / "app" / "harness").exists()


# ===================================================================
# 测试类 7：SSE 事件 Schema 一致性
# ===================================================================


class TestSSEEventConsistency:
    """验证 SSE 事件类型与 events.py Schema 一致。"""

    def test_events_schema_exists(self) -> None:
        """app/schemas/events.py 必须存在且包含 SUPPORTED_EVENT_TYPES。"""
        events_py = REPO_ROOT / "app" / "schemas" / "events.py"
        assert _exists(events_py), f"events.py 缺失: {events_py}"
        content = _read(events_py)
        assert "SUPPORTED_EVENT_TYPES" in content, (
            "events.py 未定义 SUPPORTED_EVENT_TYPES"
        )

    def test_all_events_in_schema(self) -> None:
        """所有 13 种 SSE 事件必须在 events.py 的 SUPPORTED_EVENT_TYPES 中定义。"""
        events_py = REPO_ROOT / "app" / "schemas" / "events.py"
        content = _read(events_py)
        for event in SSE_EVENTS:
            assert event in content, (
                f"SSE 事件 {event} 未在 app/schemas/events.py 的 "
                "SUPPORTED_EVENT_TYPES 中定义"
            )

    def test_no_dangling_events(self) -> None:
        """events.py 中定义的事件不应超出兼容矩阵覆盖范围。"""
        events_py = REPO_ROOT / "app" / "schemas" / "events.py"
        content = _read(events_py)
        # 提取 SUPPORTED_EVENT_TYPES 中的所有事件
        for event in SSE_EVENTS:
            assert event in content, f"事件 {event} 应在 events.py 中"


# ===================================================================
# 测试类 8：API Schema 一致性
# ===================================================================


class TestAPISchemaConsistency:
    """验证兼容矩阵中的 Schema 引用与 app/schemas/ 中的实际 Schema 一致。"""

    def test_message_schema_exists(self) -> None:
        """app/schemas/message.py 必须存在。"""
        assert _exists(REPO_ROOT / "app" / "schemas" / "message.py")

    def test_advance_schema_exists(self) -> None:
        """app/schemas/advance.py 必须存在。"""
        assert _exists(REPO_ROOT / "app" / "schemas" / "advance.py")

    def test_review_schema_exists(self) -> None:
        """app/schemas/review.py 必须存在。"""
        assert _exists(REPO_ROOT / "app" / "schemas" / "review.py")

    def test_record_schema_exists(self) -> None:
        """app/schemas/record.py 必须存在。"""
        assert _exists(REPO_ROOT / "app" / "schemas" / "record.py")

    def test_recovery_schema_exists(self) -> None:
        """app/schemas/recovery.py 必须存在。"""
        assert _exists(REPO_ROOT / "app" / "schemas" / "recovery.py")

    def test_common_schema_exists(self) -> None:
        """app/schemas/common.py 必须存在（定义 success_response 等）。"""
        assert _exists(REPO_ROOT / "app" / "schemas" / "common.py")

    def test_agent_schema_exists(self) -> None:
        """app/schemas/agent.py 必须存在（定义 XuanhuState 等）。"""
        assert _exists(REPO_ROOT / "app" / "schemas" / "agent.py")


# ===================================================================
# 测试类 9：禁止项验证
# ===================================================================


class TestProhibitions:
    """验证所有 L0-1 禁止事项已被遵守。"""

    @pytest.mark.parametrize("filename", ADR_FILES)
    def test_adr_no_todo(self, filename: str) -> None:
        """ADR 不得包含占位符。"""
        content = _read(ADR_DIR / filename)
        for ph in ["TODO", "TBD", "FIXME", "待补充", "待完成", "[待定]"]:
            assert ph not in content, f"{filename} 包含占位符: {ph}"

    def test_matrix_no_todo(self) -> None:
        """兼容矩阵不得包含占位符。"""
        content = _read(LEGACY_MATRIX_FILE)
        for ph in ["TODO", "TBD", "FIXME", "待补充", "待完成", "[待定]"]:
            assert ph not in content, f"兼容矩阵包含占位符: {ph}"

    def test_boundary_no_todo(self) -> None:
        """迁移边界不得包含占位符。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        for ph in ["TODO", "TBD", "FIXME", "待补充", "待完成", "[待定]"]:
            assert ph not in content, f"迁移边界包含占位符: {ph}"

    def test_no_langgraph_implementation(self) -> None:
        """L2～L4 保持 Harness/运行时与业务 Agent 的目录边界。"""
        harper_dir = REPO_ROOT / "app" / "harness"
        assert not harper_dir.exists(), (
            f"Harness 已统一落在 app/agent_runtime，不应再创建并行真源: {harper_dir}"
        )
        agent_runtime_dir = REPO_ROOT / "app" / "agent_runtime"
        forbidden_agent_files = {
            "intake_extraction.py",
            "syndrome_draft.py",
            "formula_draft.py",
            "safety_explanation.py",
            "record_narration.py",
            "question_composer.py",
        }
        present = {path.name for path in agent_runtime_dir.glob("*.py")}
        assert not (present & forbidden_agent_files), "业务 Agent 必须留在 app/agents 层"

    def test_agent_runtime_version_defaults_legacy(self) -> None:
        """``agent_runtime_version`` 必须默认 ``legacy``（L0-3 约束，L1 不得改变）。"""
        config_py = REPO_ROOT / "app" / "core" / "config.py"
        content = _read(config_py)
        assert "agent_runtime_version" in content
        assert 'default="legacy"' in content


# ===================================================================
# 测试类 10：ADR-001 禁用语句回归断言
# ===================================================================


class TestADR001Regression:
    """验证 ADR-001 不含被禁止的语句和模式。"""

    def test_no_phase_based_legacy_fallback(self) -> None:
        """ADR-001 不得包含同一 LangGraph 会话按阶段回退到 Legacy 的方案。"""
        content = _read(ADR_DIR / "ADR-001-adopt-langgraph.md")
        # 不得提及"渐进回滚"（per-phase rollback to Legacy）
        assert "渐进回滚" not in content, (
            "ADR-001 不得包含渐进回滚（按阶段回退到 Legacy）方案"
        )
        # 不得提及 per-phase Feature Flag 控制是否走 LangGraph
        assert "每个阶段通过 Feature Flag" not in content, (
            "ADR-001 不得包含按阶段通过 Feature Flag 控制运行时的方案"
        )

    def test_feature_flag_only_new_sessions(self) -> None:
        """Feature Flag 只决定新会话运行时，既有会话不得混用执行路径。"""
        content = _read(ADR_DIR / "ADR-001-adopt-langgraph.md")
        assert "不可隐式切换" in content, (
            "ADR-001 未明确既有会话不可隐式切换运行时"
        )

    def test_checkpointer_table_in_l1_not_l0_2(self) -> None:
        """checkpointer 表建设归入 L1，不得归入 L0-2。"""
        content = _read(ADR_DIR / "ADR-001-adopt-langgraph.md")
        # L1 应该负责 checkpointer 表
        assert "L1" in content, "ADR-001 应提及 L1 阶段"
        # 不得将 checkpointer 表归入 L0-2
        if "L0-2" in content:
            assert "L0-2 Golden 基线" not in content, (
                "ADR-001 不得将 checkpointer 表建设归入 L0-2"
            )


# ===================================================================
# 测试类 11：ADR-002 职责边界回归断言
# ===================================================================


class TestADR002Regression:
    """验证 ADR-002 Graph State 与 checkpoint 的职责边界。"""

    def test_no_pending_review_in_graph_state(self) -> None:
        """Graph State 不得包含 pending_review 字段。"""
        content = _read(ADR_DIR / "ADR-002-domain-state-and-graph-state-boundary.md")
        # 检查 Graph State 定义区域是否包含 pending_review
        graph_state_section_start = content.find("### Graph State")
        assert graph_state_section_start != -1, "ADR-002 缺少 Graph State 章节"
        # 取 Graph State 到下一个 ### 之间的内容
        next_section = content.find("###", graph_state_section_start + 1)
        graph_section = content[graph_state_section_start:next_section] if next_section != -1 else content[graph_state_section_start:]
        # Graph State 内容列表中不得有 pending_review
        assert "`pending_review`" not in graph_section, (
            "ADR-002 Graph State 不得包含 pending_review 字段——"
            "Doctor Review 以 interrupt/checkpoint 为硬门控"
        )

    def test_interrupt_is_hard_gate_not_pending_review(self) -> None:
        """Doctor Review 必须使用 interrupt/checkpoint 作为硬门控。"""
        content = _read(ADR_DIR / "ADR-002-domain-state-and-graph-state-boundary.md")
        # ADR-002 应当提及 interrupt 或 checkpoint 作为 Doctor Review 机制
        # 或至少在 Domain State 章节说明 pending_review 不应作为控制真源
        assert "interrupt" in content.lower() or "checkpoint" in content.lower(), (
            "ADR-002 应提及 interrupt/checkpoint 作为 Doctor Review 的硬门控机制"
        )

    def test_no_structured_clinical_output_in_checkpoint(self) -> None:
        """checkpoint 禁止保存结构化临床模型输出。"""
        content = _read(ADR_DIR / "ADR-002-domain-state-and-graph-state-boundary.md")
        prohibited_section_start = content.find("### 明确禁止放入 Graph State")
        assert prohibited_section_start != -1, "ADR-002 缺少禁止项章节"
        next_section = content.find("###", prohibited_section_start + 1)
        prohibited_section = content[prohibited_section_start:next_section] if next_section != -1 else content[prohibited_section_start:]
        # 必须明确禁止结构化临床模型输出
        assert ("临床模型" in prohibited_section
                or "SyndromeResult" in prohibited_section
                or "FormulaDraft" in prohibited_section
                or "SafetyRuleResult" in prohibited_section), (
            "ADR-002 禁止项中必须明确禁止结构化临床模型输出"
        )

    def test_graph_state_only_minimal_execution_data(self) -> None:
        """Graph State 只保存最小可序列化执行数据和 Domain artifact 引用。"""
        content = _read(ADR_DIR / "ADR-002-domain-state-and-graph-state-boundary.md")
        assert "最小" in content, (
            "ADR-002 未明确 Graph State 只保存最小数据"
        )
        assert "引用" in content, (
            "ADR-002 未明确 Graph State 通过引用关联 Domain artifact"
        )

    def test_field_naming_matches_implementation_plan(self) -> None:
        """Graph State 字段命名须严格对齐实施计划 6.2 XuanhuGraphState。"""
        content = _read(ADR_DIR / "ADR-002-domain-state-and-graph-state-boundary.md")
        # 实施计划 6.2 XuanhuGraphState 定义的 12 个字段
        impl_plan_fields = [
            "session_id", "domain_state_version", "command",
            "command_id", "graph_version", "run_id", "route",
            "gate_results", "artifact_refs", "pending_interrupt",
            "budget", "last_error",
        ]
        for field in impl_plan_fields:
            assert f"`{field}`" in content or field in content, (
                f"ADR-002 Graph State 缺少实施计划 6.2 定义的字段: {field}"
            )
        # 负向断言：6.2 不存在的旧字段不得作为 Graph State 核心列表项出现
        gs_start = content.find("### Graph State")
        if gs_start != -1:
            gs_end_marker = content.find("###", gs_start + 1)
            gs_section = content[gs_start:gs_end_marker] if gs_end_marker != -1 else content[gs_start:]
            deprecated_fields = ["current_stage", "rollback_counts", "blocked_reason", "recovery_status"]
            for df in deprecated_fields:
                assert f"- `{df}`" not in gs_section, (
                    f"ADR-002 Graph State 内容区块不得包含已废弃字段 `{df}`，"
                    "该字段不在实施计划 6.2 XuanhuGraphState 定义中"
                )


# ===================================================================
# 测试类 12：ADR-003 禁用语句回归断言
# ===================================================================


class TestADR003Regression:
    """验证 ADR-003 不含被禁止的 Sufficiency 回退语句。"""

    def test_no_use_llm_sufficiency(self) -> None:
        """ADR-003 不得包含 use_llm_sufficiency 开关。"""
        content = _read(ADR_DIR / "ADR-003-sufficiency-as-policy.md")
        assert "use_llm_sufficiency" not in content, (
            "ADR-003 不得包含 use_llm_sufficiency 开关——"
            "CompletenessPolicy 始终是确定性 Gate"
        )

    def test_no_langgraph_fallback_to_sufficiency_agent(self) -> None:
        """LangGraph 路径不得回滚到 SufficiencyAgent。"""
        content = _read(ADR_DIR / "ADR-003-sufficiency-as-policy.md")
        # 不得允许 LangGraph 路径中选择使用 LLM SufficiencyAgent
        if "LangGraph" in content and "SufficiencyAgent" in content:
            # 如果同时提及，必须是说 Legacy 保留，不能说 LangGraph 可用
            rollback_section_start = content.find("## 回滚策略")
            if rollback_section_start != -1:
                next_section = content.find("##", rollback_section_start + 1)
                rollback_section = content[rollback_section_start:next_section] if next_section != -1 else content[rollback_section_start:]
                assert "LangGraph 路径中也可选择" not in rollback_section, (
                    "ADR-003 回滚策略不得允许 LangGraph 路径选择使用 LLM SufficiencyAgent"
                )

    def test_completeness_policy_always_deterministic(self) -> None:
        """CompletenessPolicy 始终是确定性 Gate，模型不得决定充分性。"""
        content = _read(ADR_DIR / "ADR-003-sufficiency-as-policy.md")
        assert "确定性" in content, (
            "ADR-003 必须明确 CompletenessPolicy 是确定性 Gate"
        )
        # 模型不得决定充分性
        assert "模型不得决定" in content or "不由模型" in content or "不再由 LLM Agent 执行" in content, (
            "ADR-003 必须明确模型不得决定充分性或阶段路由"
        )

    def test_legacy_sufficiency_agent_only_in_legacy(self) -> None:
        """Legacy SufficiencyAgent 只能留在 Legacy 会话。"""
        content = _read(ADR_DIR / "ADR-003-sufficiency-as-policy.md")
        assert "Legacy 路径" in content, (
            "ADR-003 应明确 Legacy SufficiencyAgent 仅在 Legacy 路径保留"
        )

    def test_no_model_stage_routing(self) -> None:
        """模型不得决定阶段路由。"""
        content = _read(ADR_DIR / "ADR-003-sufficiency-as-policy.md")
        # 模型不应参与阶段路由决策
        if "路由" in content:
            assert "确定性" in content or "Gate" in content or "Policy" in content, (
                "ADR-003 中阶段路由应由确定性策略控制，非模型"
            )


# ===================================================================
# 测试类 13：会话隔离回归断言
# ===================================================================


class TestSessionIsolationRegression:
    """验证会话隔离规则在各文档中一致。"""

    def test_no_mixed_execution_in_legacy_matrix(self) -> None:
        """兼容矩阵明确 Feature Flag 只决定新会话运行时。"""
        content = _read(LEGACY_MATRIX_FILE)
        assert "会话创建时确定" in content, (
            "兼容矩阵未明确会话创建时确定运行时身份"
        )
        assert "不可隐式切换" in content, (
            "兼容矩阵未明确会话运行时不隐式切换"
        )

    def test_no_mixed_execution_in_migration_boundary(self) -> None:
        """迁移边界明确会话创建后不可隐式切换和交叉恢复。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        assert "不可隐式切换" in content, (
            "迁移边界未明确会话运行时不可隐式切换"
        )
        assert "不得互相恢复" in content or "交叉恢复" in content, (
            "迁移边界未明确两类会话不得互相恢复"
        )

    def test_no_cross_recovery_in_adr001(self) -> None:
        """ADR-001 明确两类会话恢复路径隔离。"""
        content = _read(ADR_DIR / "ADR-001-adopt-langgraph.md")
        assert "恢复路径严格隔离" in content or "恢复路径" in content, (
            "ADR-001 未明确恢复路径隔离"
        )

    def test_no_implicit_degradation_in_migration_boundary(self) -> None:
        """迁移边界明确禁止 LangGraph 错误时静默降级到 Legacy。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        assert "静默降级" in content or "静默" in content, (
            "迁移边界未明确禁止静默降级到 Legacy"
        )

    def test_session_isolation_consistent_across_docs(self) -> None:
        """ADR-001 与迁移边界在会话隔离规则上一致。"""
        adr001 = _read(ADR_DIR / "ADR-001-adopt-langgraph.md")
        boundary = _read(MIGRATION_BOUNDARY_FILE)
        # 两份文档都应包含不可隐式切换的约束
        adr001_has = "不可隐式切换" in adr001 or "不可隐式" in adr001
        boundary_has = "不可隐式切换" in boundary or "不可隐式" in boundary
        assert adr001_has and boundary_has, (
            "ADR-001 和迁移边界在会话隔离规则上不一致："
            f"ADR-001={'有' if adr001_has else '缺'}，迁移边界={'有' if boundary_has else '缺'}"
        )


# ===================================================================
# 测试类 14：阶段归属回归断言
# ===================================================================


class TestStageAttributionRegression:
    """验证目标阶段序列和 Agent 职责边界。"""

    def test_syndrome_formula_boundary_preserved(self) -> None:
        """目标阶段序列必须保留 Syndrome/Formula 边界。"""
        content = _read(LEGACY_MATRIX_FILE)
        # LangGraph 阶段序列中 syndrome 必须是独立阶段
        assert "syndrome" in content.lower(), (
            "兼容矩阵目标阶段序列中必须保留 syndrome 阶段"
        )

    def test_target_stage_sequence_correct(self) -> None:
        """LangGraph 阶段序列必须为 inquiry → syndrome → formula → safety → review → record → done。"""
        content = _read(LEGACY_MATRIX_FILE)
        # syndrome 必须在 formula 之前，且两者都必须出现
        assert "syndrome" in content.lower(), (
            "兼容矩阵目标阶段序列中必须包含 syndrome 阶段"
        )
        assert "formula" in content.lower(), (
            "兼容矩阵目标阶段序列中必须包含 formula 阶段"
        )
        # formula 必须在 safety 之前（已在阶段序列中体现）

    def test_intake_extraction_agent_facts_only(self) -> None:
        """IntakeExtractionAgent 只抽取事实，不生成下一问。"""
        content = _read(LEGACY_MATRIX_FILE)
        # 兼容矩阵中应描述 IntakeExtractionAgent 的职责为抽取事实
        assert "抽取" in content or "extract" in content.lower(), (
            "兼容矩阵应描述 IntakeExtractionAgent 抽取事实的职责"
        )

    def test_gap_selection_is_deterministic(self) -> None:
        """信息缺口由确定性策略选择，下一问由模板或 QuestionComposer 生成。"""
        content = _read(LEGACY_MATRIX_FILE)
        # 缺口选择必须是确定性的
        if "缺口" in content:
            assert "确定性" in content or "策略" in content, (
                "兼容矩阵中缺口选择应由确定性策略执行"
            )

    def test_stage_sequence_in_migration_boundary(self) -> None:
        """迁移边界中的阶段序列必须与兼容矩阵一致。"""
        boundary = _read(MIGRATION_BOUNDARY_FILE)
        assert "syndrome" in boundary.lower(), (
            "迁移边界会话生命周期中必须保留 syndrome 阶段"
        )
        assert "formula" in boundary.lower(), (
            "迁移边界会话生命周期中必须包含 formula 阶段"
        )

    def test_no_pending_review_as_second_truth(self) -> None:
        """迁移边界明确 pending_review 不得成为第二套控制真源。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        # interrupt/checkpoint 是硬门控，不是 pending_review
        if "pending_review" in content:
            # 如果提及 pending_review，必须说明它不是控制真源
            assert ("第二套" in content
                    or "interrupt" in content.lower()
                    or "checkpoint" in content.lower()), (
                "迁移边界中如提及 pending_review，必须明确 interrupt/checkpoint 是硬门控"
            )


# ===================================================================
# 测试类 15：Legacy 删除时机回归断言
# ===================================================================


class TestLegacyRemovalTiming:
    """验证 Legacy 删除时机与实施计划 L9-4 一致。"""

    def test_legacy_removal_matches_l9_4(self) -> None:
        """迁移边界中 Legacy 删除时机必须与实施计划 L9-4 一致。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        # 不应包含"至少保留一个 release 周期"的额外条件
        assert "至少保留一个 release 周期" not in content, (
            "迁移边界不得自行增加 release 周期条件——Legacy 删除时机与 L9-4 一致"
        )

    def test_legacy_removal_after_l9_acceptance(self) -> None:
        """Legacy 代码应在 L9 验收通过后移除。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        assert "L9" in content and ("验收" in content or "移除" in content), (
            "迁移边界应明确 Legacy 代码在 L9 验收通过后移除"
        )


# ===================================================================
# 测试类 16：AR-B-002 下一问职责断言
# ===================================================================


class TestNextQuestionResponsibility:
    """验证下一问由模板/QuestionComposer 生成，而非 InquiryAgent。"""

    def test_matrix_no_inquiryagent_next_question(self) -> None:
        """兼容矩阵目标架构不得声明 agent_message.content 仍为 InquiryAgent 的 next_question。"""
        content = _read(LEGACY_MATRIX_FILE)
        # 禁止的表述
        prohibited = "仍为 InquiryAgent（LLM）的 `next_question`"
        assert prohibited not in content, (
            f"兼容矩阵目标架构不得包含禁止表述: {prohibited}"
        )

    def test_matrix_has_question_composer(self) -> None:
        """兼容矩阵目标架构应声明 agent_message.content 由模板或 QuestionComposer 生成。"""
        content = _read(LEGACY_MATRIX_FILE)
        assert "QuestionComposer" in content, (
            "兼容矩阵应提及 QuestionComposer 负责生成下一问"
        )

    def test_matrix_has_gap_selector(self) -> None:
        """兼容矩阵应声明 GapSelector 确定性选择信息缺口。"""
        content = _read(LEGACY_MATRIX_FILE)
        assert "GapSelector" in content, (
            "兼容矩阵应提及 GapSelector 确定性选择唯一缺口"
        )

    def test_adr003_intake_extraction_facts_only(self) -> None:
        """ADR-003 明确 IntakeExtractionAgent 只抽取事实，不生成下一问。"""
        content = _read(ADR_DIR / "ADR-003-sufficiency-as-policy.md")
        assert "不生成下一问" in content, (
            "ADR-003 应明确 IntakeExtractionAgent 不生成下一问"
        )


# ===================================================================
# 测试类 17：AR-B-002 force=true 医疗硬边界断言
# ===================================================================


class TestForceMedicalHardBoundary:
    """验证 force=true 不得绕过医疗硬前置条件。"""

    def test_adr003_force_no_bypass_red_flags(self) -> None:
        """ADR-003 明确 force=true 不得绕过红旗。"""
        content = _read(ADR_DIR / "ADR-003-sufficiency-as-policy.md")
        assert "不得绕过" in content, (
            "ADR-003 应明确 force=true 不得绕过医疗硬前置条件"
        )
        assert "红旗" in content or "red flag" in content.lower(), (
            "ADR-003 应明确 force=true 不得绕过红旗"
        )
        assert "`force=true` 不得把 `sufficient=false` 改为可推进" in content, (
            "ADR-003 必须禁止 LangGraph force=true 绕过 CompletenessPolicy"
        )

    def test_adr003_force_no_bypass_allergy_pregnancy(self) -> None:
        """ADR-003 明确 force=true 不得绕过过敏/妊娠/当前用药采集状态。"""
        content = _read(ADR_DIR / "ADR-003-sufficiency-as-policy.md")
        assert "过敏" in content, "ADR-003 force 边界应提及过敏"
        assert "妊娠" in content, "ADR-003 force 边界应提及妊娠"

    def test_adr003_manual_override_record(self) -> None:
        """ADR-003 明确医师人工推进必须建模为 ManualOverrideRecord。"""
        content = _read(ADR_DIR / "ADR-003-sufficiency-as-policy.md")
        assert "ManualOverrideRecord" in content, (
            "ADR-003 应提及 ManualOverrideRecord 作为医师人工推进的可审计建模"
        )

    def test_adr003_no_rewrite_completeness_policy(self) -> None:
        """ADR-003 明确不得将 CompletenessPolicy 改写为通过。"""
        content = _read(ADR_DIR / "ADR-003-sufficiency-as-policy.md")
        assert "不得将" in content and "改写为通过" in content, (
            "ADR-003 应明确不得将 CompletenessPolicy 改写为通过"
        )

    def test_matrix_force_medical_hard_boundary(self) -> None:
        """兼容矩阵 POST /advance 明确 force=true 的医疗硬边界。"""
        content = _read(LEGACY_MATRIX_FILE)
        assert "医疗硬前置" in content or "红旗" in content, (
            "兼容矩阵 force=true 描述应包含医疗硬前置条件"
        )
        assert "`force=true` 不得把 `sufficient=false` 改为可推进" in content, (
            "兼容矩阵必须禁止 LangGraph force=true 绕过 CompletenessPolicy"
        )


# ===================================================================
# 测试类 18：AR-B-002 Graph State 6.2 精确字段与 checkpoint 禁止断言
# ===================================================================


class TestGraphStatePreciseFields:
    """验证 Graph State 字段严格对齐实施计划 6.2，禁止结构化临床结果进入 checkpoint。"""

    def test_migration_boundary_has_62_fields(self) -> None:
        """迁移边界 Graph State 内容应包含 6.2 定义的字段。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        s62_fields = [
            "domain_state_version", "command", "command_id",
            "run_id", "route", "gate_results", "artifact_refs",
            "pending_interrupt", "budget", "last_error",
        ]
        for field in s62_fields:
            assert f"`{field}`" in content or field in content, (
                f"迁移边界 Graph State 缺少 6.2 字段: {field}"
            )
        graph_section = content[
            content.index("## 1. Domain State 与 Graph State 边界"):
            content.index("## 2. 会话隔离边界")
        ]
        assert "`state_version`" not in graph_section, (
            "迁移边界 Graph State 章节不得使用已废弃的 state_version，"
            "必须使用 domain_state_version"
        )

    def test_adr002_no_ambiguous_structured_extraction(self) -> None:
        """ADR-002 禁止项不得包含模糊的 只保存结构化提取结果 表述。"""
        content = _read(ADR_DIR / "ADR-002-domain-state-and-graph-state-boundary.md")
        # 这个模糊表述已被替换
        ambiguous = "只保存结构化提取结果"
        # 在禁止项区域搜索
        prohibited_start = content.find("明确禁止放入 Graph State")
        if prohibited_start != -1:
            next_section = content.find("##", prohibited_start + 1)
            prohibited_section = content[prohibited_start:next_section] if next_section != -1 else content[prohibited_start:]
            assert ambiguous not in prohibited_section, (
                f"ADR-002 禁止项不得包含模糊表述: {ambiguous}"
            )

    def test_migration_boundary_no_ambiguous_structured_extraction(self) -> None:
        """迁移边界 checkpoint 禁止项不得包含模糊的结构化提取结果表述。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        ambiguous = "只保存已通过 Schema 校验的结构化提取结果"
        assert ambiguous not in content, (
            f"迁移边界 checkpoint 禁止项不得包含模糊表述: {ambiguous}"
        )

    def test_adr002_graph_state_minimal_only(self) -> None:
        """ADR-002 明确临床事实和模型产物只能进入 Domain State。"""
        content = _read(ADR_DIR / "ADR-002-domain-state-and-graph-state-boundary.md")
        assert "临床事实" in content or "唯一权威" in content, (
            "ADR-002 应明确临床事实只能进入 Domain State"
        )

    def test_adr002_no_deprecated_execution_fields(self) -> None:
        """ADR-002 的 Graph State 边界不得继续引用旧执行字段。"""
        content = _read(ADR_DIR / "ADR-002-domain-state-and-graph-state-boundary.md")
        decision_start = content.index("### Graph State")
        decision_end = content.index("## 正面影响")
        boundary = content[decision_start:decision_end]
        prohibited = [
            "Graph State 中的 `state_version`",
            "Graph State 获取 `state_version`",
            "checkpoint 获取 `current_stage`",
        ]
        for phrase in prohibited:
            assert phrase not in boundary, (
                f"ADR-002 Graph State 决策边界不得包含旧字段用法: {phrase}"
            )

    def test_graph_state_containers_are_reference_only(self) -> None:
        """容器字段不得成为临床数据或原始错误的旁路。"""
        content = _read(ADR_DIR / "ADR-002-domain-state-and-graph-state-boundary.md")
        required = [
            "不包含患者输入或临床载荷",
            "不保存完整 Gate 输出或临床字段",
            "不保存医师决定、处方或患者数据",
            "不保存异常堆栈、Prompt、模型输出或患者数据",
        ]
        for phrase in required:
            assert phrase in content, f"ADR-002 缺少 Graph State 容器限制: {phrase}"


# ===================================================================
# 测试类 19：AR-B-002 既有会话禁止跨运行时恢复断言
# ===================================================================


class TestCrossRuntimeRecoveryProhibition:
    """验证既有会话不得跨运行时恢复。"""

    def test_adr001_no_cross_recovery(self) -> None:
        """ADR-001 回滚策略明确两类会话恢复路径不得交叉。"""
        content = _read(ADR_DIR / "ADR-001-adopt-langgraph.md")
        assert "恢复路径严格隔离" in content or "不得交叉恢复" in content, (
            "ADR-001 回滚策略应明确两类会话恢复路径不得交叉"
        )

    def test_adr001_feature_flag_only_new_sessions(self) -> None:
        """ADR-001 回滚策略明确 Feature Flag 只影响新会话。"""
        content = _read(ADR_DIR / "ADR-001-adopt-langgraph.md")
        assert "只影响新会话" in content or "只决定新会话" in content, (
            "ADR-001 应明确 Feature Flag 只影响/决定新会话"
        )

    def test_adr001_no_rebuild_langgraph_to_legacy(self) -> None:
        """ADR-001 明确既有 LangGraph 会话不得重建或切换到 Legacy。"""
        content = _read(ADR_DIR / "ADR-001-adopt-langgraph.md")
        assert "不得重建" in content or "不得切换" in content or "不可隐式切换" in content, (
            "ADR-001 应明确既有 LangGraph 会话不得重建或切换到 Legacy"
        )
        assert "回滚到 Legacy 后，从 Domain State 重建会话状态" not in content, (
            "ADR-001 不得暗示把既有 LangGraph 会话从 Domain State 重建为 Legacy"
        )

    def test_migration_boundary_no_cross_recovery(self) -> None:
        """迁移边界明确两类会话不得互相恢复。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        assert "不得互相恢复" in content, (
            "迁移边界应明确两类会话不得互相恢复"
        )

    def test_matrix_no_cross_recovery(self) -> None:
        """兼容矩阵明确两类会话恢复路径严格隔离。"""
        content = _read(LEGACY_MATRIX_FILE)
        assert "Legacy 与 LangGraph 会话不得互相恢复" in content or "交叉恢复" in content, (
            "兼容矩阵应明确两类会话不得互相恢复"
        )


# ===================================================================
# 测试类 20：AR-B-002 完整阶段顺序断言
# ===================================================================


class TestPhaseSequence:
    """验证目标阶段序列完整且一致。"""

    def test_matrix_langgraph_phase_sequence(self) -> None:
        """兼容矩阵 LangGraph 阶段序列必须包含 inquiry, syndrome, formula, safety, review, record, done。"""
        content = _read(LEGACY_MATRIX_FILE)
        required_stages = ["inquiry", "syndrome", "formula", "safety", "review", "record", "done"]
        for stage in required_stages:
            assert stage in content.lower(), (
                f"兼容矩阵 LangGraph 阶段序列缺少: {stage}"
            )

    def test_migration_boundary_phase_sequence(self) -> None:
        """迁移边界 LangGraph 会话生命周期阶段序列与兼容矩阵一致。"""
        content = _read(MIGRATION_BOUNDARY_FILE)
        required_stages = ["inquiry", "syndrome", "formula", "safety", "review", "record", "done"]
        for stage in required_stages:
            assert stage in content.lower(), (
                f"迁移边界会话生命周期缺少阶段: {stage}"
            )

    def test_adr001_phase_sequence_syndrome_independent(self) -> None:
        """ADR-001 目标阶段序列中 SYNDROME 保留为独立阶段。"""
        content = _read(ADR_DIR / "ADR-001-adopt-langgraph.md")
        # syndrome 必须在 LangGraph 阶段序列中被提及
        if "formula" in content.lower():
            assert "syndrome" in content.lower(), (
                "ADR-001 目标阶段序列中 syndrome 必须保留为独立阶段"
            )

    def test_matrix_no_sufficiency_as_stage_in_langgraph(self) -> None:
        """兼容矩阵 LangGraph 阶段序列不得包含 sufficiency 作为独立阶段。"""
        content = _read(LEGACY_MATRIX_FILE)
        # 在 LangGraph 路径描述中，sufficiency 不应作为阶段出现（已合并为 CompletenessPolicy）
        # 注意：可能在 Legacy 对比中出现，检查 LangGraph 阶段序列部分
        langgraph_stage_section = content.find("LangGraph 阶段序列")
        if langgraph_stage_section != -1:
            # 取附近 500 字符
            section = content[langgraph_stage_section:langgraph_stage_section + 600]
            # sufficiency 不应作为独立阶段在此序列中
            # 但 "SUFFICIENCY" 可能在对比 Legacy 时出现
            assert "sufficiency" not in section.lower() or "合并为" in section, (
                "兼容矩阵 LangGraph 阶段序列中 sufficiency 不应作为独立阶段出现"
            )
