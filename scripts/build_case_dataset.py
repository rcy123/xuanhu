# -*- coding: utf-8 -*-
"""真实问诊医案标准化：DB 会话 → 标准医案数据集（对齐 sample_cases.json）。

数据源（每个 done 会话）：
    medical_records.record_json（最终处方/安全/复核）
    artifact_revision_payloads：syndrome_draft（证型/依据/治法/证据）、
                                formula_draft（基础方/候选方/方义）
    observations（主诉/现病史/十问/舌脉/患者信息）

输出（data/generated_cases/cases.json）对齐 import_knowledge --type cases 的
输入 schema：entry_type/title/disease_category/syndrome/treatment_principle/
formula_summary/content/source/metadata。PII 清洗复用 prepare_rag_bundle 的
RECORD_ID_RE/PII_SCAN_PATTERNS（命中→替换/标记，无法清洗则进入 quarantine）。

用法:
    uv run python -m scripts.build_case_dataset
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import selectors
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import text  # noqa: E402

from app.db.session import get_session_factory  # noqa: E402
from scripts.prepare_rag_bundle import PII_SCAN_PATTERNS, RECORD_ID_RE  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "xuanhu.generated-cases.v1"
REDACTED_RECORD_ID = "[REDACTED_RECORD_ID]"

# 主诉 → 病名映射（disease_category 用；无映射留空由医师归口）
DISEASE_KEYWORDS: tuple[tuple[str, ...], str] = (
    (("咳嗽", "咳"), "咳嗽"),
    (("泄泻", "便溏", "腹泻"), "泄泻"),
    (("胁痛",), "胁痛"),
    (("不寐", "失眠", "入睡"), "不寐"),
    (("水肿", "浮肿"), "水肿"),
    (("胃痛", "胃脘"), "胃痛"),
    (("带下",), "带下"),
    (("自汗", "盗汗"), "自汗"),
    (("头痛",), "头痛"),
    (("眩晕", "头晕"), "眩晕"),
)


def _fact_text(value) -> str:
    """observation value 兼容 str/dict/list → 短文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = [str(v) for v in value.values() if isinstance(v, str) and v]
        return "，".join(parts)
    if isinstance(value, (list, tuple)):
        return "，".join(str(v) for v in value if v)
    return str(value)


def _pick_facts(observations: list[dict], *keys: str) -> list[str]:
    """按 fact_key 取 active 观察的文本值（去重）。"""
    seen: set[str] = set()
    out: list[str] = []
    for obs in observations:
        if obs["fact_key"] in keys:
            text_value = _fact_text(obs["value"])
            if text_value and text_value not in seen:
                seen.add(text_value)
                out.append(text_value)
    return out


def _disease_category(symptom_text: str) -> str:
    for keywords, name in DISEASE_KEYWORDS:
        if any(keyword in symptom_text for keyword in keywords):
            return name
    return ""


def _age_band(age) -> str:
    try:
        age_value = int(age)
    except (TypeError, ValueError):
        return "unspecified"
    if age_value < 18:
        return "child"
    if age_value >= 60:
        return "elderly"
    return "adult"


def _sex(sex) -> str:
    if sex in ("male", "female"):
        return sex
    return "unspecified"


def _sex_cn(sex_value) -> str:
    return "男" if sex_value == "male" else "女" if sex_value == "female" else ""


async def _load_session(session_id: str) -> dict | None:
    """聚合单会话的全部医案原料。"""
    async with get_session_factory()() as db:
        session_row = (
            await db.execute(
                text(
                    "SELECT current_stage, status FROM consult_sessions WHERE id=:sid"
                ),
                {"sid": session_id},
            )
        ).one_or_none()
        if session_row is None or session_row[1] != "done":
            return None
        record = (
            await db.execute(
                text(
                    "SELECT record_json FROM medical_records "
                    "WHERE session_id=:sid ORDER BY version DESC LIMIT 1"
                ),
                {"sid": session_id},
            )
        ).one_or_none()
        payload_rows = (
            await db.execute(
                text(
                    "SELECT ar.artifact_type, arp.payload FROM artifact_revision_payloads arp "
                    "JOIN artifact_revisions ar ON ar.id = arp.artifact_revision_id "
                    "WHERE ar.session_id=:sid AND ar.status='current' ORDER BY ar.revision"
                ),
                {"sid": session_id},
            )
        ).all()
        obs_rows = (
            await db.execute(
                text(
                    "SELECT fact_key, value FROM observations "
                    "WHERE session_id=:sid AND status='active'"
                ),
                {"sid": session_id},
            )
        ).all()
    return {
        "session_id": session_id,
        "record_json": (record[0] if record else None) or {},
        "artifacts": {kind: payload for kind, payload in payload_rows},
        "observations": [{"fact_key": k, "value": v} for k, v in obs_rows],
    }


def _redact_pii(text_value: str) -> str:
    """替换病历号/患者标识符；命中更强 PII 时返回空串（quarantine）。"""
    if not text_value:
        return text_value
    redacted = RECORD_ID_RE.sub(REDACTED_RECORD_ID, text_value)
    for pattern in PII_SCAN_PATTERNS.values():
        if pattern.search(redacted):
            return ""
    return redacted


def _compose_content(raw: dict) -> str:
    """拼自然语言医案正文。"""
    observations = raw["observations"]
    record_json = raw["record_json"] or {}
    syndrome_payload = (raw["artifacts"] or {}).get("syndrome_draft") or {}
    formula_payload = (raw["artifacts"] or {}).get("formula_draft") or {}
    syndrome_output = syndrome_payload.get("output") or {}
    formula_output = formula_payload.get("output") or {}

    chief = _pick_facts(observations, "chief_complaint.symptom")
    course = _pick_facts(observations, "chief_complaint.course")
    present = [
        f"{obs['fact_key'].split('.')[-1]}：{_fact_text(obs['value'])}"
        for obs in observations
        if obs["fact_key"].startswith("present_illness.")
        and obs["fact_key"] != "present_illness.change"
    ]
    ten_questions = [
        f"{obs['fact_key'].split('.')[-1]}：{_fact_text(obs['value'])}"
        for obs in observations
        if obs["fact_key"].startswith("ten_questions.")
    ]
    inspection = _pick_facts(observations, "four_diagnosis.inspection")
    palpation = _pick_facts(observations, "four_diagnosis.palpation")
    sex_value = _pick_facts(observations, "patient.sex")
    age_value = _pick_facts(observations, "patient.age")
    past_history = _pick_facts(observations, "past_history")

    lines: list[str] = []
    sex_text = _sex_cn(sex_value[0] if sex_value else "")
    age_text = age_value[0] if age_value else ""
    identity = f"患者{sex_text}，{age_text}岁" if sex_text or age_text else "患者"
    chief_text = "；".join(chief) or "未采集主诉"
    course_text = "；".join(course)
    lines.append(f"{identity}。主诉：{chief_text}" + (f"，病程{course_text}。" if course_text else "。"))
    if present:
        lines.append("现病史：" + "；".join(present) + "。")
    if ten_questions:
        lines.append("问诊：" + "；".join(ten_questions) + "。")
    if inspection or palpation:
        lines.append("四诊：" + ("；".join(inspection) + ("。" if inspection else "")) + ("；".join(palpation) + "。" if palpation else ""))
    else:
        lines.append("舌脉未采集。")
    if past_history:
        lines.append("既往史：" + "；".join(past_history) + "。")

    syndrome = syndrome_output.get("syndrome")
    basis = syndrome_output.get("syndrome_basis") or []
    principle = syndrome_output.get("treatment_principle")
    if syndrome:
        lines.append(f"辨证为{syndrome}。")
        claims = [str(item.get("claim", "")) for item in basis if item.get("claim")]
        if claims:
            lines.append("辨证依据：" + "；".join(claims) + "。")
    if principle:
        lines.append(f"治以{principle}。")

    candidate = formula_output.get("candidate_formula") or {}
    name = candidate.get("name")
    composition = candidate.get("composition") or []
    if name and composition:
        composition_text = "、".join(
            f"{item.get('herb')} {item.get('dose')}{item.get('unit') or 'g'}"
            for item in composition
        )
        lines.append(f"方用{name}：{composition_text}。")
        rationale = candidate.get("rationale")
        if rationale:
            lines.append(f"方义：{rationale}。")
    elif record_json:
        formula = record_json.get("formula") or {}
        name = formula.get("name")
        comp = formula.get("composition") or []
        if name and comp:
            composition_text = "、".join(
                f"{item.get('herb')} {item.get('dose')}{item.get('unit') or 'g'}"
                for item in comp
            )
            lines.append(f"方用{name}：{composition_text}。")

    return "\n".join(line for line in lines if line)


def _build_case(raw: dict, label: str | None) -> dict | None:
    observations = raw["observations"]
    syndrome_output = ((raw["artifacts"] or {}).get("syndrome_draft") or {}).get("output") or {}
    formula_payload = (raw["artifacts"] or {}).get("formula_draft") or {}
    formula_output = formula_payload.get("output") or {}
    candidate = formula_output.get("candidate_formula") or {}
    formula_name = candidate.get("name") or ""
    composition = candidate.get("composition") or []

    syndrome = syndrome_output.get("syndrome") or ""
    principle = syndrome_output.get("treatment_principle") or ""
    chief_text = "；".join(_pick_facts(observations, "chief_complaint.symptom"))
    content = _compose_content(raw)
    content = _redact_pii(content)
    title = _redact_pii(f"{syndrome}（{chief_text[:24]}）" if syndrome else f"医案（{chief_text[:24]}）")

    if not content or not title:
        return None

    composition_text = "、".join(
        f"{item.get('herb')} {item.get('dose')}{item.get('unit') or 'g'}"
        for item in composition[:8]
    )
    formula_summary = f"{formula_name}：{composition_text}" if formula_name and composition_text else formula_name

    sex_values = _pick_facts(observations, "patient.sex")
    age_values = _pick_facts(observations, "patient.age")
    syndrome_payload = (raw["artifacts"] or {}).get("syndrome_draft") or {}
    run_spec = formula_payload.get("run_spec") or {}
    run_spec_syndrome = syndrome_payload.get("run_spec") or {}
    policy_version = run_spec.get("policy_version") or run_spec_syndrome.get("policy_version") or ""

    disease = _disease_category(chief_text)
    if syndrome:
        tags = [token for token in re.split(r"[，,、；;（）()]", syndrome) if token][:3]
    else:
        tags = [disease] if disease else []

    return {
        "entry_type": "case",
        "title": title,
        "disease_category": disease,
        "syndrome": syndrome,
        "treatment_principle": principle,
        "formula_summary": formula_summary,
        "content": content,
        "source": "悬壶真实问诊医案集 v1",
        "metadata": {
            "schema_version": SCHEMA_VERSION,
            "deidentified": True,
            "age_band": _age_band(age_values[0] if age_values else None),
            "sex": _sex(sex_values[0] if sex_values else None),
            "tags": tags,
            "license": "generated",
            "provenance": {
                "session_id": raw["session_id"],
                "policy_version": policy_version,
                "rag_mode": "rag_retrieved" if "rag.v1" in policy_version else "no-rag",
                "record_id": (raw.get("record_json") or {}).get("record_id"),
            },
        },
    }


async def _build_all(session_ids: list[str], labels: dict[str, str]) -> list[dict]:
    cases: list[dict] = []
    skipped: list[str] = []
    for session_id in session_ids:
        raw = await _load_session(session_id)
        if raw is None:
            skipped.append(session_id)
            continue
        case = _build_case(raw, labels.get(session_id))
        if case is None:
            skipped.append(session_id)
            continue
        cases.append(case)
        print(f"  built case: {case['title'][:50]}... (session {session_id[:8]})")
    if skipped:
        print(f"  skipped {len(skipped)} sessions: {[s[:8] for s in skipped]}")
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description="真实问诊医案标准化")
    parser.add_argument("--manifest", type=Path, default=ROOT / "data" / "generated_cases" / "manifest.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "generated_cases" / "cases.json")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写文件")
    args = parser.parse_args()

    if not args.manifest.exists():
        print(f"manifest 不存在: {args.manifest}")
        return

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    done = [item for item in manifest if item.get("ok") and item.get("session_id")]
    labels = {item["session_id"]: item.get("label", "") for item in done}
    print(f"manifest 中完成会话: {len(done)}")
    if not done:
        return

    cases = asyncio.run(
        _build_all([item["session_id"] for item in done], labels),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )

    print(f"\n生成医案: {len(cases)} 条")
    if args.dry_run:
        for case in cases[:5]:
            print(f"  - {case['title']} | {case.get('disease_category')} | {case.get('syndrome')}")
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入: {args.output}")


if __name__ == "__main__":
    main()
