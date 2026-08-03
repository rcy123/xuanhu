# -*- coding: utf-8 -*-
"""批量真实问诊驱动：生成 20-30 条多场景医案会话。

用法:
    uv run python -m scripts.generate_case_sessions --limit 12

流程（与 tmp/full_flow_test.py 同构）：
    create → intake 循环（IntakeAnswers 匹配器 + 场景 overrides）→ safety
    assertions confirm → advance 循环（blocked 时按原因 recover / review
    modify）→ review confirm → record advance → done。

输出:
    staging/generated_sessions/<session_id>.json  每条会话快照（messages +
    observations 摘要 + 各 artifact payload 引用）
    data/generated_cases/manifest.json            场景清单（label/chief/
    patient/session_id/result）

真实后端 http://localhost:8000 必须已启动（XUANHU_RAG_ENABLED 默认开启）。
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = "http://localhost:8000/api/v1/consult"
DOCTOR = "codex-agent-test"
ROOT = Path(__file__).resolve().parents[1]
SESSION_DIR = ROOT / "staging" / "generated_sessions"
MANIFEST_PATH = ROOT / "data" / "generated_cases" / "manifest.json"

# 安全拦截自动修正映射：未收录药材 → 已收录同类药（无映射则移除该药）。
SAFETY_REPLACE_HERB = {
    "煅石膏": "石膏",
    "黄芩叶": "黄芩",
    "生黄芪": "黄芪",
    "蜜黄芪": "黄芪",
    "炒白术": "白术",
    "焦白术": "白术",
    "麸炒白术": "白术",
    "法半夏": "半夏",
    "姜半夏": "半夏",
    "生甘草": "甘草",
    "炙甘草": "甘草",
    "酒白芍": "白芍",
    "炒白芍": "白芍",
    "生地黄": "地黄",
    "熟地黄": "地黄",
    "盐杜仲": "杜仲",
    "炒杜仲": "杜仲",
    "烫狗脊": "狗脊",
    "盐车前子": "车前子",
    "酒黄精": "黄精",
    "蒸黄精": "黄精",
}


def req(method: str, path: str, payload=None, headers=None, timeout: int = 300):
    url = BASE + path
    h = {"X-Doctor-Id": DOCTOR}
    if headers:
        h.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return resp.status, body
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            err_body = {"code": "HTTP_ERROR", "message": str(exc)}
        return exc.code, err_body


def post_message(sid: str, content: str, state_version: int):
    return req(
        "POST",
        f"/sessions/{sid}/messages",
        {"role": "patient_proxy", "content": content},
        headers={
            "X-State-Version": str(state_version),
            "X-Idempotency-Key": f"gen-{uuid.uuid4()}",
        },
    )


def post_advance(sid: str, state_version: int, force: bool = False):
    return req(
        "POST",
        f"/sessions/{sid}/advance",
        {"force": force},
        headers={
            "X-State-Version": str(state_version),
            "X-Idempotency-Key": f"gen-adv-{uuid.uuid4()}",
        },
    )


def get_session(sid: str) -> dict:
    _, body = req("GET", f"/sessions/{sid}", timeout=30)
    return body["data"]


def get_messages(sid: str) -> list[dict]:
    _, body = req("GET", f"/sessions/{sid}/messages", timeout=30)
    return body["data"]["items"]


def latest_agent_message(msgs: list[dict]) -> dict | None:
    for m in msgs:
        if m["role"] == "agent" and m.get("content"):
            return m
    return None


class IntakeAnswers:
    """关键词答案匹配器。场景 overrides 优先于默认规则。"""

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self.overrides = overrides or {}

    def answer_for(self, q: str) -> str:
        t = q or ""
        if "。" in t:
            t = t.split("。", 1)[1]

        # 场景覆盖优先（最长关键词先匹配）
        for keyword in sorted(self.overrides, key=len, reverse=True):
            if keyword in t:
                return self.overrides[keyword]

        # ---- Safety（通用否定，让抽取器按问题维度提取） ----
        if "过敏" in t or "药敏" in t or "药物" in t or "用药" in t:
            return "没有"
        if "妊娠" in t or "怀孕" in t or "哺乳" in t or "孕产" in t:
            return "没有"
        if "既往" in t or "疾病史" in t or "高血压" in t or "糖尿病" in t or "病史" in t:
            return "无"

        # ---- Chief complaint & course ----
        if "持续多长" in t or "多长时间" in t or "病程" in t or "多久了" in t:
            return "一周了"
        if "诱发" in t or "诱因" in t or "起因" in t:
            return "受凉后出现"
        if "缓解" in t or "治疗" in t or "吃过" in t or "服药" in t:
            return "没有治疗过"

        # ---- Ten questions ----
        if "怕冷" in t or "寒热" in t or "发热" in t or "体温" in t:
            return "怕冷，有点发热，体温38度"
        if "汗" in t or "出汗" in t:
            return "稍微有点出汗"
        if "头" in t or "身" in t or "肢体" in t or "关节" in t:
            return "头有点晕，身上酸痛乏力"
        if "大便" in t or "小便" in t or "二便" in t or "排便" in t:
            return "大便正常，小便略黄"
        if "食欲" in t or "饮食" in t or "胃口" in t or "吃饭" in t:
            return "食欲正常"
        if "胸" in t or "腹" in t or "心" in t or "胃脘" in t:
            return "胸口有点闷"
        if "口渴" in t or "喝水" in t or "口干" in t:
            return "口渴，想喝温水"
        if "睡眠" in t or "睡觉" in t or "失眠" in t:
            return "睡眠尚可"
        if "呼吸" in t or "咳嗽" in t or "咳" in t or "痰" in t or "喘" in t:
            return "咳嗽，痰多色白"
        if "鼻" in t or "流涕" in t or "喷嚏" in t:
            return "流清涕"
        if "咽" in t or "喉" in t or "嗓子" in t:
            return "嗓子有点疼"
        if "舌" in t or "脉" in t:
            return "舌苔薄白，脉浮"
        if "加重" in t or "减轻" in t or "变化" in t or "稳定" in t:
            return "症状逐渐加重"
        if "月经" in t or "带下" in t or "白带" in t or "经带" in t:
            return "白带量多色黄"
        if "腰" in t or "腿" in t or "水肿" in t or "浮肿" in t:
            return "腰膝酸软，双下肢水肿"

        return "没有其他特殊不适"


# ---------------------------------------------------------------------------
# 场景定义（高频证型 + 语料空缺）
# ---------------------------------------------------------------------------

SCENARIOS: list[dict] = [
    {
        "label": "风寒感冒咳嗽",
        "chief_complaint": "受凉后咳嗽三天，痰白稀，怕冷，流清涕",
        "patient_info": {"name": "张伟", "sex": "male", "age": 35},
        "overrides": {"怕冷": "怕冷明显，无汗", "咳嗽": "咳嗽，痰白稀，夜间加重"},
    },
    {
        "label": "脾虚湿困泄泻",
        "chief_complaint": "大便溏稀两个月，食少乏力，腹胀",
        "patient_info": {"name": "李芳", "sex": "female", "age": 42},
        "overrides": {"大便": "大便溏稀，一日三次，黏腻不爽", "食欲": "食欲不振，饭后腹胀", "怕冷": "怕冷，手足不温", "舌": "舌淡胖有齿痕，苔白腻，脉濡缓"},
    },
    {
        "label": "肝郁气滞胁痛",
        "chief_complaint": "情志不舒后右胁胀痛一月，嗳气频作",
        "patient_info": {"name": "王强", "sex": "male", "age": 45},
        "overrides": {"胸": "右胁胀痛，走窜不定，生气后加重", "食欲": "食欲欠佳，嗳气", "睡眠": "入睡困难，多梦", "月经": "无异常"},
    },
    {
        "label": "心脾两虚不寐",
        "chief_complaint": "失眠多梦三个月，心悸健忘，神疲乏力",
        "patient_info": {"name": "赵敏", "sex": "female", "age": 38},
        "overrides": {"睡眠": "难以入睡，多梦易醒，醒后难再睡", "头": "头晕，心悸", "食欲": "食欲不振", "怕冷": "怕冷不明显"},
    },
    {
        "label": "痰湿蕴肺咳嗽",
        "chief_complaint": "咳嗽反复发作一月，痰多色白黏稠，胸闷",
        "patient_info": {"name": "孙立", "sex": "male", "age": 50},
        "overrides": {"咳嗽": "咳嗽，痰多色白黏稠，晨起加重", "胸": "胸闷憋气，痰鸣", "口渴": "不渴，口中黏腻", "舌": "舌苔白腻，脉滑"},
    },
    {
        "label": "肾阳虚水肿",
        "chief_complaint": "双下肢水肿半月，腰膝酸软，畏寒肢冷",
        "patient_info": {"name": "周强", "sex": "male", "age": 60},
        "overrides": {"腰": "腰膝酸软，双下肢水肿按之凹陷", "怕冷": "畏寒肢冷明显", "小便": "小便清长，夜尿频多", "大便": "大便溏薄"},
    },
    {
        "label": "胃阴虚胃痛",
        "chief_complaint": "胃脘灼痛两周，饥不欲食，口干咽燥",
        "patient_info": {"name": "吴静", "sex": "female", "age": 33},
        "overrides": {"口渴": "口干咽燥，喜冷饮", "胸": "胃脘灼痛，空腹加重", "食欲": "饥不欲食", "舌": "舌红少苔，脉细数", "大便": "大便干结"},
    },
    {
        "label": "湿热下注带下",
        "chief_complaint": "白带量多色黄黏稠一月，外阴瘙痒，口苦",
        "patient_info": {"name": "郑丽", "sex": "female", "age": 30},
        "overrides": {"月经": "白带量多色黄，气味腥臭", "口渴": "口苦口黏", "小便": "小便短赤", "舌": "舌红苔黄腻，脉滑数"},
    },
    {
        "label": "气虚自汗",
        "chief_complaint": "自汗盗汗一月，动则汗出更甚，神疲乏力",
        "patient_info": {"name": "冯刚", "sex": "male", "age": 28},
        "overrides": {"汗": "自汗明显，活动后汗出如洗，怕风", "头": "神疲乏力，气短懒言", "食欲": "食欲不振", "怕冷": "怕风怕冷"},
    },
    {
        "label": "血虚头痛",
        "chief_complaint": "头痛隐隐反复三月，面色萎黄，心悸失眠",
        "patient_info": {"name": "陈琳", "sex": "female", "age": 36},
        "overrides": {"头": "头痛隐隐，劳累后加重，面色萎黄", "睡眠": "失眠多梦", "月经": "月经量少色淡", "舌": "舌淡苔薄白，脉细弱"},
    },
    {
        "label": "肝阳上亢眩晕",
        "chief_complaint": "头晕目眩两周，头胀痛，急躁易怒，面红",
        "patient_info": {"name": "许斌", "sex": "male", "age": 48},
        "overrides": {"头": "头晕目眩，头胀痛，面红目赤", "睡眠": "睡眠差，多梦", "口渴": "口干口苦", "怕冷": "怕热，心烦", "舌": "舌红苔黄，脉弦数"},
    },
    {
        "label": "肺燥干咳",
        "chief_complaint": "干咳无痰半月，咽干鼻燥，口渴",
        "patient_info": {"name": "何芳", "sex": "female", "age": 40},
        "overrides": {"咳嗽": "干咳无痰或少痰，痰黏难咳", "口渴": "咽干鼻燥，口渴", "咽": "咽喉干痒", "舌": "舌红少津，脉细数", "大便": "大便偏干"},
    },
]


# ---------------------------------------------------------------------------
# 流程驱动
# ---------------------------------------------------------------------------


def _safety_confirm(sid: str, state_version: int) -> int:
    """确认 proposed safety assertions，返回刷新后的 state_version。"""
    _, body = req("GET", f"/sessions/{sid}/safety-assertions?status=proposed", timeout=30)
    for assertion in (body["data"] or {}).get("items") or []:
        req(
            "POST",
            f"/sessions/{sid}/safety-assertions/{assertion['assertion_id']}/confirm",
            {"reason_code": "DOCTOR_CONFIRMED"},
            timeout=30,
        )
    return get_session(sid).get("state_version", state_version)


def _review_resolve(sid: str, state_version: int) -> int:
    """review 阶段 confirm；被安全拒绝时自动 modify 修正。"""
    st, body = req(
        "POST",
        f"/sessions/{sid}/review",
        {"action": "confirm"},
        headers={"X-State-Version": str(state_version)},
        timeout=120,
    )
    print(f"  REVIEW confirm status={st} stage={(body.get('data') or {}).get('current_stage')}")
    if st == 200:
        return (body.get("data") or {}).get("state_version", state_version)
    # confirm 被安全引擎拒绝（方剂仍不通过）→ 转自动 modify 修正
    if body.get("code") == "SAFETY_REVIEW_BLOCKED":
        print("  REVIEW confirm blocked → auto modify")
        return _safety_blocked_modify(sid, state_version)
    return state_version


def _safety_blocked_modify(sid: str, state_version: int) -> int:
    """读安全拦截原因，自动 modify 修正方剂使其通过二次安全审核。

    - 未收录药（知识缺口）：替换为已收录同类，无映射则移除。
    - dose_limit（超 max_dose）：降至知识库 max_dose；知识库无值则从
      suggestion 提取最大剂量；仍无则按 0.8 系数保守下调。
    - 保留其余组成；pregnancy_status 等状态类 warning 不构成方剂修改。

    用同步 psycopg 读取（脚本主流程是同步 urllib，避免嵌套事件循环）。
    """
    import os

    import psycopg

    def _load() -> tuple[list[dict], dict, dict[str, float]]:
        with psycopg.connect(os.environ["DB_URL"]) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT issues, formula_snapshot FROM safety_rule_runs "
                    "WHERE session_id=%s ORDER BY created_at DESC LIMIT 1",
                    (uuid.UUID(sid),),
                )
                row = cur.fetchone()
                cur.execute("SELECT name, max_dose FROM herbs WHERE deleted_at IS NULL")
                max_dose_rows = cur.fetchall()
        max_dose = {name: float(value) for name, value in max_dose_rows if value}
        if row is None:
            return [], {}, max_dose
        return (row[0] or []), (row[1] or {}), max_dose

    issues_raw, formula, max_dose = _load()
    # 从 suggestion 提取（药名, 上限）：「白术」剂量 15.0g 超过上限 12.0g
    _limit_pattern = re.compile(r"「([^」]+)」剂量\s*[\d.]+\s*g\s*超过(?:最大剂量|上限)\s*([\d.]+)\s*g")
    dose_limits: list[tuple[str, float]] = []
    unknown_herbs: set[str] = set()
    for issue in issues_raw:
        suggestion = str(issue.get("suggestion", ""))
        match = _limit_pattern.search(suggestion)
        if issue.get("type") == "dose_limit" and match:
            dose_limits.append((match.group(1), float(match.group(2))))
        if "未收录" in suggestion:
            for herb in issue.get("herbs") or []:
                unknown_herbs.add(herb)

    def _herb_matches(comp_herb: str, issue_herb: str) -> bool:
        """炮制前缀容错：白术/炒白术/麸炒白术 互认。"""
        return comp_herb == issue_herb or issue_herb in comp_herb or comp_herb in issue_herb

    composition = list(formula.get("composition") or [])
    notes: list[str] = []
    kept: list[dict] = []
    for item in composition:
        herb = item.get("herb", "")
        dose = item.get("dose")
        if any(_herb_matches(herb, unknown) for unknown in unknown_herbs):
            replacement = SAFETY_REPLACE_HERB.get(herb) or SAFETY_REPLACE_HERB.get(
                next((u for u in unknown_herbs if _herb_matches(herb, u)), "")
            )
            if replacement:
                item = {**item, "herb": replacement}
                notes.append(f"{herb}→{replacement}")
            else:
                notes.append(f"移除未收录药{herb}")
                continue
        limit: float | None = None
        for issue_herb, extracted in dose_limits:
            if _herb_matches(herb, issue_herb):
                limit = extracted
                break
        if limit is None:
            limit = max_dose.get(herb)
        if limit is None and isinstance(dose, (int, float)):
            limit = round(float(dose) * 0.8, 1)
        if limit is not None and isinstance(dose, (int, float)) and dose > limit:
            item = {**item, "dose": limit}
            notes.append(f"{herb} {dose}g→{limit}g")
        kept.append(item)
    st, body = req(
        "POST",
        f"/sessions/{sid}/review",
        {
            "action": "modify",
            "formula_override": {
                "composition": kept,
                "rationale": "医师自动修正：" + ("；".join(notes) if notes else "调整方剂组成"),
            },
            "feedback": "自动修正安全拦截",
        },
        headers={"X-State-Version": str(state_version)},
        timeout=120,
    )
    print(f"  BLOCKED modify status={st} notes={notes}")
    if st != 200:
        return state_version
    return (body.get("data") or {}).get("state_version", state_version)


def _intake_loop(sid: str, answers: IntakeAnswers, state_version: int, max_rounds: int = 30) -> int:
    """驱动问诊直到 completion_notice，返回最新 state_version。"""
    for _round in range(max_rounds):
        msgs = get_messages(sid)
        qmsg = latest_agent_message(msgs)
        if qmsg is None:
            break
        sd = qmsg.get("structured_delta") or {}
        if sd.get("kind") == "completion_notice":
            print(f"  [intake {_round}] COMPLETION_NOTICE")
            break
        ans = answers.answer_for(qmsg["content"])
        for _attempt in range(3):
            st, body = post_message(sid, ans, state_version)
            if st == 200:
                break
            sess_now = get_session(sid)
            state_version = sess_now.get("state_version", state_version)
        if st != 200:
            print(f"  [intake {_round}] MSG FAILED {st}: {body.get('code')}")
            break
        data = body.get("data") or {}
        state_version = data.get("state_version", state_version)
        agent = data.get("agent_message") or {}
        if (agent.get("structured_delta") or {}).get("kind") == "completion_notice":
            print(f"  [intake {_round}] complete")
            break
    return state_version


def run_scenario(scenario: dict, max_rounds: int = 30) -> dict:
    label = scenario["label"]
    print(f"\n{'=' * 70}\nSCENARIO: {label}\nchief={scenario['chief_complaint']!r}")
    answers = IntakeAnswers(scenario.get("overrides") or {})

    st, body = req(
        "POST",
        "/sessions",
        {"patient_info": scenario["patient_info"], "chief_complaint": scenario["chief_complaint"]},
        timeout=30,
    )
    sid = (body.get("data") or {}).get("session_id")
    print(f"CREATE status={st} sid={sid}")
    if not sid:
        return {"label": label, "ok": False, "error": body}

    state_version = 1
    state_version = _intake_loop(sid, answers, state_version, max_rounds)
    state_version = _safety_confirm(sid, state_version)

    # advance 循环：inquiry → syndrome → formula → safety → review / done
    for adv in range(1, 12):
        st, body = post_advance(sid, state_version)
        if st != 200:
            code = body.get("code")
            print(f"  ADV {adv} status={st} code={code}")
            if code in ("MODEL_GATEWAY_UNAVAILABLE", "STATE_RECOVERY_REQUIRED"):
                st2, body2 = req(
                    "POST",
                    f"/sessions/{sid}/recover",
                    {"action": "retry_current_stage", "reason": "advance 失败自动重试"},
                    headers={"X-State-Version": str(state_version)},
                    timeout=180,
                )
                sess = get_session(sid)
                state_version = sess.get("state_version", state_version)
                if sess.get("current_stage") == "inquiry":
                    # 重新完整问诊（completeness gate 需重建）
                    state_version = _intake_loop(sid, answers, state_version, max_rounds)
                    state_version = _safety_confirm(sid, state_version)
                continue
            break

        data = body.get("data") or {}
        state_version = data.get("state_version", state_version)
        stage = data.get("current_stage")
        print(f"  ADV {adv} ok v={state_version} stage={stage}")
        if stage in ("review", "done", "record"):
            break
        if stage == "blocked":
            sess = get_session(sid)
            if sess.get("blocked_reason") == "safety_rule_blocked":
                state_version = _safety_blocked_modify(sid, state_version)
                continue
            # 其他 blocked（reasoning 模型失败等）：recover 后回 inquiry 重新完整问诊
            st2, _b2 = req(
                "POST",
                f"/sessions/{sid}/recover",
                {"action": "retry_current_stage", "reason": "reasoning 失败自动重试"},
                headers={"X-State-Version": str(state_version)},
                timeout=180,
            )
            sess = get_session(sid)
            state_version = sess.get("state_version", state_version)
            print(f"  RECOVER status={st2} stage={sess.get('current_stage')}")
            if sess.get("current_stage") == "inquiry":
                state_version = _intake_loop(sid, answers, state_version, max_rounds)
                state_version = _safety_confirm(sid, state_version)
            continue

    # review 处理
    sess = get_session(sid)
    if sess.get("current_stage") == "review":
        state_version = _review_resolve(sid, state_version)

    # record 生成
    sess = get_session(sid)
    if sess.get("current_stage") == "record":
        st, body = post_advance(sid, state_version)
        state_version = (body.get("data") or {}).get("state_version", state_version)
        print(f"  RECORD advance status={st} stage={(body.get('data') or {}).get('current_stage')}")

    sess = get_session(sid)
    print(f"FINAL: stage={sess.get('current_stage')} status={sess.get('status')}")
    return {
        "label": label,
        "ok": sess.get("current_stage") in ("done", "record", "review"),
        "session_id": sid,
        "stage": sess.get("current_stage"),
        "status": sess.get("status"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="批量真实问诊生成医案会话")
    parser.add_argument("--limit", type=int, default=12, help="场景数量上限")
    parser.add_argument("--start", type=int, default=0, help="起始场景下标（可续跑）")
    args = parser.parse_args()

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = MANIFEST_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    manifest: list[dict] = []
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    scenarios = SCENARIOS[args.start : args.start + args.limit]
    for scenario in scenarios:
        result = run_scenario(scenario)
        manifest.append(result)
        if result.get("session_id"):
            # 会话快照：messages + 会话状态（artifacts 由 build_case_dataset 从 DB 读）
            snapshot = {
                "session_id": result["session_id"],
                "label": scenario["label"],
                "chief_complaint": scenario["chief_complaint"],
                "patient_info": scenario["patient_info"],
                "messages": get_messages(result["session_id"]),
                "session": get_session(result["session_id"]),
            }
            (SESSION_DIR / f"{result['session_id']}.json").write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    ok = sum(1 for item in manifest[-len(scenarios):] if item.get("ok"))
    print(f"\n=== 完成 {len(scenarios)} 场景，成功 {ok} ===")
    for item in manifest[-len(scenarios):]:
        print(f"  {item['label']}: {item.get('stage')}/{item.get('status')} ok={item.get('ok')}")


if __name__ == "__main__":
    main()
