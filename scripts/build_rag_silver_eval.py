"""构建并冻结 RAG 弱监督评测数据集 — rag-silver-v1。

用法::

    uv run python -m scripts.build_rag_silver_eval build \\
        --prepared-bundle <isolated-staging> \\
        --output-dir data/rag_eval_silver/v1 \\
        --seed 20260807 --smoke-size 20 --test-size 200
    uv run python -m scripts.build_rag_silver_eval verify \\
        --dataset-dir data/rag_eval_silver/v1 \\
        --prepared-bundle <isolated-staging>

设计依据：docs/05_RAG效果评测/02-弱监督数据集构建规范.md、03-实现与文件变更清单.md。

数据集定位：基于医案症状自动构建、经过答案泄漏校验的弱监督工程评测集。
禁止使用"专家标注""医学金标准""临床准确率""诊断正确率"等表述。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# 固定合同常量
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1.0"
DATASET_VERSION = "rag-silver-v1"
FIXED_SEED = 20260807
FIXED_TEST_SIZE = 200
FIXED_SMOKE_SIZE = 20
SYMPTOM_MAX_CHARS = 900
MIN_SYMPTOM_CHINESE_CHARS = 40
QUERY_MIN_CHARS = 25
QUERY_MAX_CHARS = 180
EXCESSIVE_OVERLAP_JACCARD = 0.60
NEAR_DUPLICATE_JACCARD = 0.90
UNCLASSIFIED_STRATUM = "未分类"
LOW_FREQUENCY_STRATUM = "其他低频类"
LOW_FREQUENCY_THRESHOLD = 5
RECORD_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# 答案边界标记（按文档顺序；实际取所有标记中最早出现的位置）
ANSWER_BOUNDARY_MARKERS = [
    "辨为",
    "辨证为",
    "诊断",
    "中医诊断",
    "证属",
    "治以",
    "治拟",
    "拟以",
    "给予",
    "投以",
    "处方",
    "方用",
]

# 答案边界之后要整行删除的标签行。内容会先经过 NFKC，故这里同时接受
# ASCII / 全角冒号和标签内部空白，不能只匹配未规范化的 ``证型：``。
_LABEL_LINE_PATTERN = re.compile(r"(?:证\s*型|治\s*法|方\s*剂|处\s*方|方\s*药)\s*[:：]")

# 结论式措辞（Query 风格泄漏）
CONCLUSION_STYLE_KEYWORDS = ["辨证为", "证属", "治法", "方用", "处方", "方剂", "药用"]

QUERY_PROMPT_VERSION = "rag-silver-query-v1"

SYSTEM_PROMPT = (
    "你是检索评测 Query 改写器。请把输入的患者症状材料改写为一条自然、独立、\n"
    "适合检索相似中医医案的中文查询。只保留患者可观察到的症状、持续时间、诱因、\n"
    "舌脉等信息；不要给出或猜测疾病名称、证型、治法、方剂、药物、医案标题、医生结论。\n"
    "不要提及“原文”“医案”“根据材料”。不得复制长句，应改变句式但保持事实一致。\n"
    '输出严格 JSON：{"query":"..."}，query 长度 25 到 180 个中文字符。'
)

USER_PROMPT_TEMPLATE = "患者症状材料：\n{symptom_text}"

QUERY_MODEL_TEMPERATURE = 0.1
QUERY_MODEL_MAX_RETRIES = 2
QUERY_MODEL_RETRY_BACKOFF_SECONDS = [1.0, 3.0]

JsonObject = dict[str, Any]


# ---------------------------------------------------------------------------
# 通用工具：哈希、JSON、原子写
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    """计算文件内容（原始字节）的 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    """计算 UTF-8 文本的 SHA-256。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """规范 JSON 序列化：UTF-8、键排序、缩进 2、末尾单个换行。"""
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return (text + "\n").encode("utf-8")


def compact_json_bytes(value: Any) -> bytes:
    """紧凑规范 JSON：UTF-8、键排序、紧凑分隔符、无末尾换行（用于 hash 输入）。"""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text.encode("utf-8")


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """同目录临时文件 + 原子替换。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_bytes(data)
    temporary_path.replace(path)


def write_json_atomic(path: Path, value: Any) -> None:
    write_bytes_atomic(path, canonical_json_bytes(value))


# ---------------------------------------------------------------------------
# 内容规范化与症状片段提取（doc 02 §3）
# ---------------------------------------------------------------------------


def normalize_content(text: str) -> str:
    """NFKC 规范化 + 换行统一 + 连续空白折叠（保留段落换行）+ 去首尾空白。"""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    collapsed_lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in lines]
    result = "\n".join(collapsed_lines)
    result = re.sub(r"\n{2,}", "\n", result)
    return result.strip()


def _build_boundary_pattern() -> re.Pattern[str]:
    """构建答案边界标记的正则：允许标记内部有冒号/空白变体。"""
    alternatives = []
    for marker in ANSWER_BOUNDARY_MARKERS:
        # 每个字符之间允许可选空白，标记末尾允许紧跟常见冒号
        spaced = r"\s*".join(re.escape(ch) for ch in marker)
        alternatives.append(spaced)
    pattern = "(?:" + "|".join(alternatives) + r")\s*[:：]?"
    return re.compile(pattern)


_BOUNDARY_PATTERN = _build_boundary_pattern()


def find_answer_boundary(text: str) -> int | None:
    """查找答案边界标记最早出现的位置；无匹配返回 None。"""
    match = _BOUNDARY_PATTERN.search(text)
    if match is None:
        return None
    return match.start()


def strip_label_lines(text: str) -> str:
    """删除显式包含标签（证型：/治法：/方剂：/处方：/方药：）的整行。"""
    lines = text.split("\n")
    kept = [line for line in lines if not _LABEL_LINE_PATTERN.search(line)]
    return "\n".join(kept)


def count_chinese_chars(text: str) -> int:
    """统计中文字符数（CJK 统一表意文字基本区）。"""
    return sum(1 for ch in text if "一" <= ch <= "鿿")


@dataclass
class SymptomExtractionResult:
    symptom_text: str | None
    reason: str | None


def extract_symptom_fragment(content: str) -> SymptomExtractionResult:
    """从规范化 content 中提取症状片段（答案边界截断 + 标签行剔除 + 长度校验）。"""
    normalized = normalize_content(content)
    boundary = find_answer_boundary(normalized)
    fragment = normalized if boundary is None else normalized[:boundary]
    fragment = strip_label_lines(fragment).strip()

    if count_chinese_chars(fragment) < MIN_SYMPTOM_CHINESE_CHARS:
        return SymptomExtractionResult(symptom_text=None, reason="insufficient_symptom_text")

    truncated = fragment[:SYMPTOM_MAX_CHARS]
    return SymptomExtractionResult(symptom_text=truncated, reason=None)


# ---------------------------------------------------------------------------
# 答案泄漏与结论式措辞检查（doc 02 §5.2）
# ---------------------------------------------------------------------------

_PUNCT_STRIP_PATTERN = re.compile(r"[\s，。！？；：、,.!?;:\"'“”‘’()（）\[\]【】]+")


def normalize_for_leakage_compare(text: str) -> str:
    """NFKC + 小写 + 去空白/常见标点，用于泄漏比较。"""
    normalized = unicodedata.normalize("NFKC", text).lower()
    return _PUNCT_STRIP_PATTERN.sub("", normalized)


def strip_title_numbering(title: str) -> str:
    """去除标题编号/括号，得到裸标题（用于补充 forbidden term）。"""
    stripped = re.sub(r"^[\s\d一二三四五六七八九十、.．,，:：()（）\[\]【】]+", "", title)
    stripped = re.sub(r"[（(].*?[）)]", "", stripped)
    return stripped.strip()


def extract_formula_names(formula_summary: str) -> list[str]:
    """从 formula_summary 中提取方名：冒号前名称 + 显式药方名 token。"""
    names: list[str] = []
    if not formula_summary:
        return names
    for line in formula_summary.split("\n"):
        line = line.strip()
        if not line:
            continue
        for sep in ("：", ":"):
            if sep in line:
                names.append(line.split(sep, 1)[0].strip())
                break
        for token in re.findall(r"[一-鿿]{2,8}[汤丸散膏丹饮煎方剂]", line):
            names.append(token)
    return names


def extract_conclusion_phrases(content: str) -> list[str]:
    """提取答案边界之后显式"辨证为/证属/治以/处方/方用"短语（简化为整段短句）。"""
    normalized = normalize_content(content)
    boundary = find_answer_boundary(normalized)
    if boundary is None:
        return []
    tail = normalized[boundary:]
    phrases: list[str] = []
    for marker in ("辨证为", "证属", "治以", "处方", "方用"):
        idx = tail.find(marker)
        if idx == -1:
            continue
        snippet = tail[idx : idx + 40]
        end = min(
            [pos for pos in (snippet.find("。"), snippet.find("；"), snippet.find("\n")) if pos != -1] or [len(snippet)]
        )
        phrases.append(snippet[:end])
    return phrases


def build_forbidden_terms(
    *,
    title: str,
    syndrome: str | None,
    treatment_principle: str | None,
    formula_summary: str | None,
    content: str,
) -> list[str]:
    """构建 forbidden_terms 列表（doc 02 §5.2）。"""
    terms: list[str] = [title, strip_title_numbering(title)]
    if syndrome:
        terms.append(syndrome)
    if treatment_principle:
        terms.append(treatment_principle)
    if formula_summary:
        terms.extend(extract_formula_names(formula_summary))
    terms.extend(extract_conclusion_phrases(content))
    # 去重、去空
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        term = term.strip()
        if not term or term in seen:
            continue
        seen.add(term)
        result.append(term)
    return result


def check_answer_leakage(query: str, forbidden_terms: list[str]) -> bool:
    """任一长度>=2中文字符的 forbidden term 完整出现在 Query 中即泄漏。"""
    normalized_query = normalize_for_leakage_compare(query)
    for term in forbidden_terms:
        normalized_term = normalize_for_leakage_compare(term)
        if count_chinese_chars(normalized_term) >= 2 and normalized_term in normalized_query:
            return True
    return False


def check_conclusion_style_leakage(query: str) -> bool:
    """Query 含"辨证为、证属、治法、方用、处方、方剂、药用"等结论提示词。"""
    normalized_query = normalize_for_leakage_compare(query)
    return any(normalize_for_leakage_compare(keyword) in normalized_query for keyword in CONCLUSION_STYLE_KEYWORDS)


# ---------------------------------------------------------------------------
# 长度、4-gram Jaccard 与近重复检查（doc 02 §5.1, §5.3）
# ---------------------------------------------------------------------------


def normalize_query_length(query: str) -> str:
    """NFKC + 空白规范化后用于长度校验的文本。"""
    normalized = unicodedata.normalize("NFKC", query)
    return re.sub(r"\s+", " ", normalized).strip()


def check_query_length(query: str) -> bool:
    normalized = normalize_query_length(query)
    length = len(normalized)
    return QUERY_MIN_CHARS <= length <= QUERY_MAX_CHARS


def char_ngrams(text: str, n: int = 4) -> set[str]:
    if len(text) < n:
        return set()
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def jaccard_similarity(a: str, b: str) -> float:
    """字符 n-gram Jaccard 相似度；任一文本不足 4 字符返回 1.0（视为无效/最大重叠）。"""
    ngrams_a = char_ngrams(a)
    ngrams_b = char_ngrams(b)
    if not ngrams_a or not ngrams_b:
        return 1.0
    intersection = len(ngrams_a & ngrams_b)
    union = len(ngrams_a | ngrams_b)
    return intersection / union if union else 0.0


def normalize_query_for_dedup(query: str) -> str:
    """去标点/空白的小写 normalized_query（用于重复/近重复检查）。"""
    return normalize_for_leakage_compare(query)


def check_excessive_source_overlap(query: str, symptom_text: str) -> bool:
    """按合同用规范化 Query 与症状片段计算复制重叠。"""
    return (
        jaccard_similarity(
            normalize_query_for_dedup(query),
            normalize_query_for_dedup(symptom_text),
        )
        > EXCESSIVE_OVERLAP_JACCARD
    )


def check_near_duplicate(normalized_query: str, accepted_normalized_queries: list[str]) -> bool:
    for other in accepted_normalized_queries:
        if jaccard_similarity(normalized_query, other) > NEAR_DUPLICATE_JACCARD:
            return True
    return False


# ---------------------------------------------------------------------------
# 候选与分层 (doc 02 §2, §6)
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """结构合格候选（模型调用前）。"""

    record_key: str
    title: str
    stratum: str
    symptom_text: str
    source_symptom_sha256: str
    syndrome: str | None
    treatment_principle: str | None
    formula_summary: str | None
    content: str
    forbidden_terms: list[str] = field(default_factory=list)


@dataclass
class CandidateRejection:
    record_key: str
    stratum: str
    reason: str


def load_structurally_valid_candidates(
    prepared_cases: list[JsonObject],
) -> tuple[list[Candidate], list[CandidateRejection]]:
    """从 prepared cases.json 加载并做结构合格性过滤（doc 02 §2, §3）。"""
    candidates: list[Candidate] = []
    rejections: list[CandidateRejection] = []
    seen_record_keys: set[str] = set()

    for record in prepared_cases:
        if record.get("entry_type") != "case":
            continue
        metadata = record.get("metadata") or {}
        record_key = metadata.get("record_key", "")
        title = record.get("title", "")
        content = record.get("content", "")

        if not RECORD_KEY_PATTERN.fullmatch(record_key or ""):
            continue
        if not content or not title:
            continue
        if record_key in seen_record_keys:
            continue
        seen_record_keys.add(record_key)

        disease_category = record.get("disease_category") or ""
        stratum = disease_category.strip() or UNCLASSIFIED_STRATUM

        extraction = extract_symptom_fragment(content)
        if extraction.symptom_text is None:
            rejections.append(
                CandidateRejection(record_key=record_key, stratum=stratum, reason=extraction.reason or "unknown")
            )
            continue

        forbidden_terms = build_forbidden_terms(
            title=title,
            syndrome=record.get("syndrome"),
            treatment_principle=record.get("treatment_principle"),
            formula_summary=record.get("formula_summary"),
            content=content,
        )

        candidates.append(
            Candidate(
                record_key=record_key,
                title=title,
                stratum=stratum,
                symptom_text=extraction.symptom_text,
                source_symptom_sha256=sha256_text(extraction.symptom_text),
                syndrome=record.get("syndrome"),
                treatment_principle=record.get("treatment_principle"),
                formula_summary=record.get("formula_summary"),
                content=content,
                forbidden_terms=forbidden_terms,
            )
        )

    return candidates, rejections


def apply_low_frequency_merge(candidates: list[Candidate]) -> None:
    """空 disease category 已归"未分类"；<5 条结构合格候选的类别合并为"其他低频类"（原地修改）。"""
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.stratum] = counts.get(candidate.stratum, 0) + 1
    for candidate in candidates:
        if candidate.stratum != UNCLASSIFIED_STRATUM and counts[candidate.stratum] < LOW_FREQUENCY_THRESHOLD:
            candidate.stratum = LOW_FREQUENCY_STRATUM


def stratum_sort_key(seed: int, record_key: str) -> str:
    """层内排序 key：sha256(f"{seed}|{record_key}")。"""
    return sha256_text(f"{seed}|{record_key}")


def group_by_stratum(candidates: list[Candidate], seed: int) -> dict[str, list[Candidate]]:
    """按层分组，层内按 sha256(seed|record_key) 升序排列。"""
    groups: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.stratum, []).append(candidate)
    for items in groups.values():
        items.sort(key=lambda c: stratum_sort_key(seed, c.record_key))
    return groups


def largest_remainder_allocation(counts: dict[str, int], total_slots: int) -> dict[str, int]:
    """最大余额法配额分配（doc 02 §6 步骤 3/5/6 共用）。

    每个非空层先分配 1 条，剩余名额按候选数占比做最大余额法分配；
    余数并列时按层名 UTF-8 字节序（升序）优先。若某层的最终配额超过该层候选数，
    在此处按候选数封顶——由调用方在耗尽再分配时用剩余层的剩余候选数和缺额
    重新调用本函数（doc 02 §6 步骤 6），本函数不做递归再分配。
    """
    non_empty_strata = [s for s, c in counts.items() if c > 0]
    if not non_empty_strata or total_slots <= 0:
        return dict.fromkeys(non_empty_strata, 0)

    if total_slots <= len(non_empty_strata):
        allocation = dict.fromkeys(non_empty_strata, 0)
        ordered = sorted(non_empty_strata, key=lambda s: s.encode("utf-8"))
        for stratum in ordered[:total_slots]:
            allocation[stratum] = 1
        return allocation

    allocation = dict.fromkeys(non_empty_strata, 1)
    remaining = total_slots - len(non_empty_strata)
    total_count = sum(counts[s] for s in non_empty_strata)

    exact_shares = {s: (counts[s] / total_count) * remaining for s in non_empty_strata}
    base_extra = {s: int(exact_shares[s]) for s in non_empty_strata}
    leftover = remaining - sum(base_extra.values())

    ordered_by_remainder = sorted(
        non_empty_strata,
        key=lambda s: (-(exact_shares[s] - base_extra[s]), s.encode("utf-8")),
    )
    for stratum in ordered_by_remainder[:leftover]:
        base_extra[stratum] += 1

    for stratum in non_empty_strata:
        allocation[stratum] = min(allocation[stratum] + base_extra[stratum], counts[stratum])

    return allocation


# ---------------------------------------------------------------------------
# 稳定 query_id 与最终排序 (doc 02 §6 步骤 7)
# ---------------------------------------------------------------------------


def final_sort_key(seed: int, record_key: str) -> str:
    """最终排序 key：sha256(f"split|{seed}|{record_key}")。"""
    return sha256_text(f"split|{seed}|{record_key}")


def stable_query_id(split: str, record_key: str, normalized_query: str) -> str:
    """稳定 query_id：<split>-<sha256("rag-silver-v1|{split}|{record_key}|{normalized_query}") 前16位>。"""
    digest = sha256_text(f"rag-silver-v1|{split}|{record_key}|{normalized_query}")
    return f"{split}-{digest[:16]}"


# ---------------------------------------------------------------------------
# Query 模型响应解析与内容合理性 (doc 02 §4, §5.1)
# ---------------------------------------------------------------------------

_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)
_CODE_FRAGMENT_PATTERN = re.compile(r"[{}<>]|^```")
_EXPLANATION_MARKERS = ("根据材料", "根据原文", "以下是", "抱歉", "作为", "我认为", "综上")


def parse_query_model_response(raw_text: str) -> tuple[str | None, bool]:
    """解析模型响应为 query 字符串。返回 (query, fence_removed)；解析失败返回 (None, False)。"""
    text = raw_text.strip()
    fence_removed = False
    match = _FENCE_PATTERN.match(text)
    if match:
        text = match.group(1).strip()
        fence_removed = True
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None, fence_removed
    if not isinstance(payload, dict) or set(payload.keys()) != {"query"}:
        return None, fence_removed
    query = payload.get("query")
    if not isinstance(query, str):
        return None, fence_removed
    return query, fence_removed


def check_query_content_sanity(query: str) -> bool:
    """不得为空、不得只含标点，不得包含 URL、代码片段或模型解释用语。"""
    stripped = query.strip()
    if not stripped:
        return False
    if not re.search(r"[\w一-鿿]", stripped):
        return False
    if _URL_PATTERN.search(stripped):
        return False
    if _CODE_FRAGMENT_PATTERN.search(stripped):
        return False
    return not any(marker in stripped for marker in _EXPLANATION_MARKERS)


# ---------------------------------------------------------------------------
# Query Generator (doc 03 §2.2)
# ---------------------------------------------------------------------------


class ChatGateway(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        trace_id: str,
    ) -> str: ...


@dataclass
class QueryGenerationResult:
    """QueryGenerator 单次调用的技术层结果（不含 Schema/泄漏校验）。"""

    raw_response: str | None
    model: str
    attempt_count: int
    latency_ms: float
    error_type: str | None


class QueryGenerator:
    """透明 Query 生成器：对象接口只接收 symptom_text，只做技术重试，不做"修正"重试。"""

    def __init__(
        self,
        gateway: ChatGateway,
        *,
        model: str,
        temperature: float = QUERY_MODEL_TEMPERATURE,
        max_tokens: int = 4096,
        max_retries: int = QUERY_MODEL_MAX_RETRIES,
        retry_backoff_seconds: list[float] | None = None,
        sleep: Any = None,
    ) -> None:
        self._gateway = gateway
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds or QUERY_MODEL_RETRY_BACKOFF_SECONDS
        self._sleep = sleep or asyncio.sleep

    async def generate(self, symptom_text: str, *, trace_id: str) -> QueryGenerationResult:
        """调用 Chat Gateway 生成一次 Query 候选，技术失败按 1s/3s 退避重试最多两次。"""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(symptom_text=symptom_text)},
        ]

        attempt = 0
        while True:
            attempt += 1
            started = time.monotonic()
            try:
                raw_response = await self._gateway.chat(
                    messages,
                    model=self._model,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    trace_id=trace_id,
                )
            except Exception as exc:  # 技术失败统一分类记录，不记录完整异常内容
                latency_ms = (time.monotonic() - started) * 1000.0
                error_type = type(exc).__name__
                if attempt > self._max_retries:
                    return QueryGenerationResult(
                        raw_response=None,
                        model=self._model,
                        attempt_count=attempt,
                        latency_ms=latency_ms,
                        error_type=error_type,
                    )
                backoff_index = min(attempt - 1, len(self._retry_backoff_seconds) - 1)
                await self._sleep(self._retry_backoff_seconds[backoff_index])
                continue
            latency_ms = (time.monotonic() - started) * 1000.0
            return QueryGenerationResult(
                raw_response=raw_response,
                model=self._model,
                attempt_count=attempt,
                latency_ms=latency_ms,
                error_type=None,
            )


# ---------------------------------------------------------------------------
# 单候选校验与抽样编排 (doc 02 §5, §6)
# ---------------------------------------------------------------------------


@dataclass
class AcceptedQuery:
    query_id: str
    query: str
    normalized_query: str
    target_record_key: str
    stratum: str
    source_symptom_sha256: str
    query_sha256: str
    response_fence_removed: bool = False


@dataclass
class RejectedAttempt:
    record_key: str
    stratum: str
    primary_reason: str
    all_reasons: list[str]
    query: str | None
    symptom_sha256: str
    model_attempts: int
    timestamp: str
    response_fence_removed: bool = False


def evaluate_query_candidate(
    query: str,
    *,
    candidate: Candidate,
    accepted_normalized_queries: list[str],
    accepted_target_keys: set[str],
) -> list[str]:
    """对已解析出的 query 字符串执行 §5.1-§5.3 校验，返回命中的拒绝原因（空=通过）。"""
    reasons: list[str] = []
    if not check_query_content_sanity(query):
        reasons.append("invalid_query_content")
    if not check_query_length(query):
        reasons.append("invalid_query_length")
    if check_answer_leakage(query, candidate.forbidden_terms):
        reasons.append("answer_leakage")
    if check_conclusion_style_leakage(query):
        reasons.append("answer_style_leakage")
    if check_excessive_source_overlap(query, candidate.symptom_text):
        reasons.append("excessive_source_overlap")
    if candidate.record_key in accepted_target_keys:
        reasons.append("duplicate_target")
    normalized_query = normalize_query_for_dedup(query)
    if normalized_query in accepted_normalized_queries:
        reasons.append("duplicate_query")
    elif check_near_duplicate(normalized_query, accepted_normalized_queries):
        reasons.append("near_duplicate_query")
    return reasons


def timestamp_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def process_candidate(
    candidate: Candidate,
    *,
    generator: QueryGenerator,
    split: str,
    accepted_normalized_queries: list[str],
    accepted_target_keys: set[str],
) -> AcceptedQuery | RejectedAttempt:
    """对单个候选调用生成模型并跑完 §5 全部门禁，返回接受或拒绝记录。"""
    trace_id = f"rag-silver-{split}-{candidate.record_key[:12]}"
    gen_result = await generator.generate(candidate.symptom_text, trace_id=trace_id)
    timestamp = timestamp_now()

    if gen_result.raw_response is None:
        return RejectedAttempt(
            record_key=candidate.record_key,
            stratum=candidate.stratum,
            primary_reason="model_call_failed",
            all_reasons=["model_call_failed"],
            query=None,
            symptom_sha256=candidate.source_symptom_sha256,
            model_attempts=gen_result.attempt_count,
            timestamp=timestamp,
            response_fence_removed=False,
        )

    query, fence_removed = parse_query_model_response(gen_result.raw_response)
    if query is None:
        return RejectedAttempt(
            record_key=candidate.record_key,
            stratum=candidate.stratum,
            primary_reason="invalid_response_schema",
            all_reasons=["invalid_response_schema"],
            query=None,
            symptom_sha256=candidate.source_symptom_sha256,
            model_attempts=gen_result.attempt_count,
            timestamp=timestamp,
            response_fence_removed=fence_removed,
        )

    reasons = evaluate_query_candidate(
        query,
        candidate=candidate,
        accepted_normalized_queries=accepted_normalized_queries,
        accepted_target_keys=accepted_target_keys,
    )
    if reasons:
        return RejectedAttempt(
            record_key=candidate.record_key,
            stratum=candidate.stratum,
            primary_reason=reasons[0],
            all_reasons=reasons,
            query=query,
            symptom_sha256=candidate.source_symptom_sha256,
            model_attempts=gen_result.attempt_count,
            timestamp=timestamp,
            response_fence_removed=fence_removed,
        )

    normalized_query = normalize_query_for_dedup(query)
    return AcceptedQuery(
        query_id=stable_query_id(split, candidate.record_key, normalized_query),
        query=query,
        normalized_query=normalized_query,
        target_record_key=candidate.record_key,
        stratum=candidate.stratum,
        source_symptom_sha256=candidate.source_symptom_sha256,
        query_sha256=sha256_text(query),
        response_fence_removed=fence_removed,
    )


@dataclass
class QuotaRedistribution:
    exhausted_stratum: str
    shortfall: int
    redistributed_to: dict[str, int]


@dataclass
class SplitResult:
    accepted: list[AcceptedQuery]
    rejected: list[RejectedAttempt]
    redistributions: list[QuotaRedistribution]


def remaining_candidates_after(
    grouped: dict[str, list[Candidate]], cursors: dict[str, int]
) -> dict[str, list[Candidate]]:
    """取每层游标之后未被消费的候选（doc 02 §6 步骤 5 的输入）。"""
    remaining: dict[str, list[Candidate]] = {}
    for stratum, items in grouped.items():
        tail = items[cursors.get(stratum, 0) :]
        if tail:
            remaining[stratum] = tail
    return remaining


async def build_split(
    grouped: dict[str, list[Candidate]],
    quota: dict[str, int],
    *,
    generator: QueryGenerator,
    split: str,
    target_size: int,
    accepted_normalized_queries: list[str],
    accepted_target_keys: set[str],
) -> tuple[SplitResult, dict[str, int]]:
    """按层内顺序生成并校验，耗尽即按剩余层重新做最大余额再分配（doc 02 §6 步骤 4/6）。

    返回 (SplitResult, cursors)；cursors 供后续 split 计算"剩余候选"。
    """
    cursors = dict.fromkeys(grouped, 0)
    remaining_quota = dict(quota)
    accepted: list[AcceptedQuery] = []
    rejected: list[RejectedAttempt] = []
    redistributions: list[QuotaRedistribution] = []
    strata = sorted(grouped.keys(), key=lambda s: s.encode("utf-8"))

    while sum(remaining_quota.values()) > 0 and len(accepted) < target_size:
        progressed = False
        for stratum in strata:
            if remaining_quota.get(stratum, 0) <= 0:
                continue
            items = grouped[stratum]
            cursor = cursors[stratum]
            while remaining_quota[stratum] > 0 and cursor < len(items) and len(accepted) < target_size:
                candidate = items[cursor]
                cursor += 1
                result = await process_candidate(
                    candidate,
                    generator=generator,
                    split=split,
                    accepted_normalized_queries=accepted_normalized_queries,
                    accepted_target_keys=accepted_target_keys,
                )
                if isinstance(result, AcceptedQuery):
                    accepted.append(result)
                    accepted_normalized_queries.append(result.normalized_query)
                    accepted_target_keys.add(result.target_record_key)
                    remaining_quota[stratum] -= 1
                    progressed = True
                else:
                    rejected.append(result)
            cursors[stratum] = cursor

            if remaining_quota[stratum] > 0 and cursor >= len(items):
                shortfall = remaining_quota[stratum]
                remaining_quota[stratum] = 0
                remaining_counts = {
                    s: len(grouped[s]) - cursors[s]
                    for s in strata
                    if s != stratum and (len(grouped[s]) - cursors[s]) > 0
                }
                if remaining_counts:
                    redistribution = largest_remainder_allocation(remaining_counts, shortfall)
                    for s, extra in redistribution.items():
                        if extra:
                            remaining_quota[s] = remaining_quota.get(s, 0) + extra
                    redistributions.append(
                        QuotaRedistribution(
                            exhausted_stratum=stratum,
                            shortfall=shortfall,
                            redistributed_to={s: v for s, v in redistribution.items() if v},
                        )
                    )
        if not progressed and all(remaining_quota.get(s, 0) == 0 or cursors[s] >= len(grouped[s]) for s in strata):
            break

    return SplitResult(accepted=accepted, rejected=rejected, redistributions=redistributions), cursors


# ---------------------------------------------------------------------------
# 序列化与文件加载 (doc 02 §7, §8)
# ---------------------------------------------------------------------------


def accepted_to_jsonl_record(item: AcceptedQuery, *, split: str) -> JsonObject:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "split": split,
        "query_id": item.query_id,
        "query": item.query,
        "target_record_key": item.target_record_key,
        "stratum": item.stratum,
        "source_symptom_sha256": item.source_symptom_sha256,
        "query_sha256": item.query_sha256,
        "response_fence_removed": item.response_fence_removed,
    }


def rejected_to_jsonl_record(item: RejectedAttempt) -> JsonObject:
    return {
        "record_key": item.record_key,
        "stratum": item.stratum,
        "primary_reason": item.primary_reason,
        "all_reasons": item.all_reasons,
        "query": item.query,
        "symptom_sha256": item.symptom_sha256,
        "model_attempts": item.model_attempts,
        "timestamp": item.timestamp,
        "response_fence_removed": item.response_fence_removed,
    }


def write_jsonl_atomic(path: Path, records: list[JsonObject]) -> None:
    lines = [compact_json_bytes(record).decode("utf-8") for record in records]
    content = ("\n".join(lines) + "\n") if lines else ""
    write_bytes_atomic(path, content.encode("utf-8"))


def read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl_file(path: Path) -> list[JsonObject]:
    records: list[JsonObject] = []
    text = path.read_text(encoding="utf-8")
    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError(f"{path} 第 {line_number} 行不是 JSON 对象")
        records.append(record)
    return records


def structural_counts_by_stratum(grouped: dict[str, list[Candidate]]) -> dict[str, int]:
    return {stratum: len(items) for stratum, items in grouped.items()}


def _staging_cases_output(staging_manifest: JsonObject) -> JsonObject | None:
    """返回 staging manifest 中唯一的 prepared cases 输出描述。"""
    outputs = staging_manifest.get("outputs")
    if not isinstance(outputs, list):
        return None
    matches = [
        item
        for item in outputs
        if isinstance(item, dict)
        and item.get("kind") == "cases"
        and item.get("disposition") == "prepared"
        and item.get("relative_path") == "prepared/cases.json"
    ]
    return matches[0] if len(matches) == 1 else None


def _staging_raw_cases_source(staging_manifest: JsonObject) -> JsonObject | None:
    """提取原始 theory cases 的可审计元数据，不读取或复制原始医案内容。"""
    source_files = staging_manifest.get("source_files")
    if not isinstance(source_files, list):
        return None
    matches = [
        item
        for item in source_files
        if isinstance(item, dict)
        and item.get("kind") == "cases"
        and isinstance(item.get("path"), str)
        and isinstance(item.get("sha256"), str)
        and isinstance(item.get("bytes"), int)
    ]
    if len(matches) != 1:
        return None
    source = matches[0]
    return {
        "path": source["path"],
        "bytes": source["bytes"],
        "sha256": source["sha256"],
        "record_count": source.get("record_count"),
    }


def validate_staging_manifest(
    staging_manifest: JsonObject, prepared_cases: list[JsonObject], actual_sha256: str
) -> str | None:
    """验证本次构建只消费 manifest 所声明的 prepared cases 快照。"""
    prepared_output = _staging_cases_output(staging_manifest)
    if prepared_output is None:
        return "staging manifest 缺少唯一的 prepared/cases.json 输出记录"
    if prepared_output.get("sha256") != actual_sha256:
        return "prepared/cases.json SHA-256 与 staging manifest 不一致"
    if prepared_output.get("record_count") != len(prepared_cases):
        return "prepared/cases.json 记录数与 staging manifest 不一致"
    if _staging_raw_cases_source(staging_manifest) is None:
        return "staging manifest 缺少唯一的原始 cases 哈希与大小"
    return None


# ---------------------------------------------------------------------------
# Git 元数据 (doc 02 §8)
# ---------------------------------------------------------------------------


def git_head_and_dirty(repo_root: Path) -> tuple[str | None, bool | None]:
    """读取当前 git HEAD 与 dirty 状态；非 git 环境返回 (None, None)，不阻断构建。"""
    import subprocess

    try:
        head = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
        return head, bool(status.strip())
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Manifest 构建与冻结校验 (doc 02 §8, §9, §10)
# ---------------------------------------------------------------------------

FROZEN_FILES = ("smoke.jsonl", "test.jsonl", "rejected.jsonl", "manifest.json")


def dataset_dir_is_frozen(output_dir: Path) -> bool:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        return False
    try:
        manifest = read_json_file(manifest_path)
    except (json.JSONDecodeError, OSError):
        return False
    return bool(manifest.get("frozen") is True)


@dataclass
class BuildConfig:
    prepared_bundle: Path
    output_dir: Path
    seed: int
    smoke_size: int
    test_size: int
    query_model: str
    query_prompt_max_chars: int = SYMPTOM_MAX_CHARS


def validate_build_config(config: BuildConfig) -> None:
    """校验命令行参数与 rag-silver-v1 固定合同一致（doc 03 §2）。"""
    if config.seed != FIXED_SEED:
        raise SystemExit(f"seed 必须为 {FIXED_SEED}（rag-silver-v1 固定合同）")
    if config.smoke_size != FIXED_SMOKE_SIZE:
        raise SystemExit(f"smoke-size 必须为 {FIXED_SMOKE_SIZE}")
    if config.test_size != FIXED_TEST_SIZE:
        raise SystemExit(f"test-size 必须为 {FIXED_TEST_SIZE}")


def check_split_mutual_exclusion(smoke: list[AcceptedQuery], test: list[AcceptedQuery]) -> list[str]:
    """校验 smoke/test 之间不共享 target、Query 或近重复 Query（doc 02 §6 末段）。"""
    problems: list[str] = []
    smoke_targets = {item.target_record_key for item in smoke}
    test_targets = {item.target_record_key for item in test}
    shared_targets = smoke_targets & test_targets
    if shared_targets:
        problems.append(f"shared_target_record_key: {len(shared_targets)}")

    smoke_norms = {item.normalized_query for item in smoke}
    test_norms = {item.normalized_query for item in test}
    shared_queries = smoke_norms & test_norms
    if shared_queries:
        problems.append(f"shared_normalized_query: {len(shared_queries)}")

    for s_item in smoke:
        for t_item in test:
            if jaccard_similarity(s_item.normalized_query, t_item.normalized_query) > NEAR_DUPLICATE_JACCARD:
                problems.append(f"near_duplicate_across_split: {s_item.query_id}~{t_item.query_id}")
    return problems


def stratum_stats(
    structural_counts: dict[str, int],
    test_quota: dict[str, int],
    smoke_quota: dict[str, int],
    accepted: list[AcceptedQuery],
    rejected: list[RejectedAttempt],
) -> dict[str, JsonObject]:
    accepted_by_stratum: dict[str, int] = {}
    for accepted_item in accepted:
        accepted_by_stratum[accepted_item.stratum] = accepted_by_stratum.get(accepted_item.stratum, 0) + 1
    rejected_by_stratum: dict[str, int] = {}
    for rejected_item in rejected:
        rejected_by_stratum[rejected_item.stratum] = rejected_by_stratum.get(rejected_item.stratum, 0) + 1

    strata = sorted(structural_counts.keys(), key=lambda s: s.encode("utf-8"))
    stats: dict[str, JsonObject] = {}
    for stratum in strata:
        stats[stratum] = {
            "structural_candidates": structural_counts.get(stratum, 0),
            "test_quota": test_quota.get(stratum, 0),
            "smoke_quota": smoke_quota.get(stratum, 0),
            "accepted": accepted_by_stratum.get(stratum, 0),
            "rejected": rejected_by_stratum.get(stratum, 0),
        }
    return stats


def rejection_reason_counts(rejected: list[RejectedAttempt]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in rejected:
        for reason in item.all_reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def build_manifest(
    *,
    config: BuildConfig,
    staging_manifest: JsonObject,
    staging_manifest_sha256: str,
    prepared_cases_sha256: str,
    prepared_cases_count: int,
    git_head: str | None,
    git_dirty: bool | None,
    generated_at: str,
    query_model: str,
    query_gateway_max_tokens: int,
    query_gateway_mode: str,
    structural_counts: dict[str, int],
    test_quota: dict[str, int],
    smoke_quota: dict[str, int],
    test_redistributions: list[QuotaRedistribution],
    smoke_redistributions: list[QuotaRedistribution],
    test_accepted: list[AcceptedQuery],
    smoke_accepted: list[AcceptedQuery],
    all_rejected: list[RejectedAttempt],
    smoke_path_sha256: str,
    test_path_sha256: str,
    rejected_path_sha256: str,
    mutual_exclusion_problems: list[str],
) -> JsonObject:
    """按 doc 02 §8 组装冻结 manifest.json。"""
    all_accepted = test_accepted + smoke_accepted
    raw_cases_source = _staging_raw_cases_source(staging_manifest)
    if raw_cases_source is None:
        raise ValueError("已验证的 staging manifest 缺少 cases source 元数据")
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "generated_at": generated_at,
        "git_commit": git_head,
        "git_dirty": git_dirty,
        "seed": config.seed,
        "sampling_algorithm_version": "rag-silver-v1-stratified-largest-remainder",
        "source": {
            "raw_cases": raw_cases_source,
            "staging_manifest_path": str(config.prepared_bundle / "manifest.json"),
            "staging_manifest_sha256": staging_manifest_sha256,
            "prepared_cases_path": str(config.prepared_bundle / "prepared" / "cases.json"),
            "prepared_cases_sha256": prepared_cases_sha256,
            "prepared_cases_record_count": prepared_cases_count,
        },
        "query_generator": {
            "prompt_version": QUERY_PROMPT_VERSION,
            "system_prompt_sha256": sha256_text(SYSTEM_PROMPT),
            "user_prompt_template_sha256": sha256_text(USER_PROMPT_TEMPLATE),
            "model": query_model,
            "gateway_mode": query_gateway_mode,
            "temperature": QUERY_MODEL_TEMPERATURE,
            "max_tokens": query_gateway_max_tokens,
            "max_retries": QUERY_MODEL_MAX_RETRIES,
            "retry_backoff_seconds": QUERY_MODEL_RETRY_BACKOFF_SECONDS,
            "symptom_max_chars": SYMPTOM_MAX_CHARS,
        },
        "thresholds": {
            "min_symptom_chinese_chars": MIN_SYMPTOM_CHINESE_CHARS,
            "query_min_chars": QUERY_MIN_CHARS,
            "query_max_chars": QUERY_MAX_CHARS,
            "excessive_overlap_jaccard": EXCESSIVE_OVERLAP_JACCARD,
            "near_duplicate_jaccard": NEAR_DUPLICATE_JACCARD,
            "low_frequency_threshold": LOW_FREQUENCY_THRESHOLD,
        },
        "stratum_stats": stratum_stats(structural_counts, test_quota, smoke_quota, all_accepted, all_rejected),
        "rejection_reason_counts": rejection_reason_counts(all_rejected),
        "quota_redistributions": {
            "test": [vars(item) for item in test_redistributions],
            "smoke": [vars(item) for item in smoke_redistributions],
        },
        "counts": {
            "test": len(test_accepted),
            "smoke": len(smoke_accepted),
            "rejected": len(all_rejected),
        },
        "mutual_exclusion_check": {
            "status": "PASS" if not mutual_exclusion_problems else "FAIL",
            "problems": mutual_exclusion_problems,
        },
        "artifact_sha256": {
            "smoke.jsonl": smoke_path_sha256,
            "test.jsonl": test_path_sha256,
            "rejected.jsonl": rejected_path_sha256,
        },
        "builder": {
            "source_sha256": sha256_file(Path(__file__)),
        },
        "frozen": True,
    }


# ---------------------------------------------------------------------------
# verify 子命令的只读复算 (doc 02 §2 --verify-only 语义)
# ---------------------------------------------------------------------------


@dataclass
class VerifyProblem:
    check: str
    detail: str


_FROZEN_QUERY_REQUIRED_FIELDS = {
    "schema_version",
    "dataset_version",
    "split",
    "query_id",
    "query",
    "target_record_key",
    "stratum",
    "source_symptom_sha256",
    "query_sha256",
}
_ANSWER_FIELDS_FORBIDDEN_IN_FROZEN_QUERIES = {
    "title",
    "syndrome",
    "treatment_principle",
    "formula_summary",
    "content",
    "metadata",
}


def _read_jsonl_for_verification(path: Path, artifact_name: str, problems: list[VerifyProblem]) -> list[JsonObject]:
    try:
        return read_jsonl_file(path)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        problems.append(VerifyProblem(f"{artifact_name}_parse", type(exc).__name__))
        return []


def _verify_near_duplicate_queries(
    records: list[JsonObject],
    *,
    split_name: str,
    problems: list[VerifyProblem],
) -> None:
    normalized_records: list[tuple[str, str]] = []
    for record in records:
        query = record.get("query")
        query_id = record.get("query_id")
        if isinstance(query, str) and isinstance(query_id, str):
            normalized_records.append((query_id, normalize_query_for_dedup(query)))

    for left_index, (left_id, left_query) in enumerate(normalized_records):
        for right_id, right_query in normalized_records[left_index + 1 :]:
            if jaccard_similarity(left_query, right_query) > NEAR_DUPLICATE_JACCARD:
                problems.append(VerifyProblem(f"{split_name}_near_duplicate_query", f"{left_id}~{right_id}"))


def _verify_split_records(
    split_name: str,
    records: list[JsonObject],
    candidate_by_key: dict[str, Candidate],
    prepared_keys: set[str],
    problems: list[VerifyProblem],
) -> None:
    seen_query_ids: set[str] = set()
    seen_targets: set[str] = set()
    seen_normalized_queries: set[str] = set()

    for record in records:
        query_id = record.get("query_id")
        missing_fields = _FROZEN_QUERY_REQUIRED_FIELDS - set(record)
        if missing_fields:
            problems.append(VerifyProblem(f"{split_name}_schema", f"missing={sorted(missing_fields)}"))
        leaked_fields = _ANSWER_FIELDS_FORBIDDEN_IN_FROZEN_QUERIES & set(record)
        if leaked_fields:
            problems.append(VerifyProblem(f"{split_name}_answer_fields", f"fields={sorted(leaked_fields)}"))
        if record.get("schema_version") != SCHEMA_VERSION:
            problems.append(VerifyProblem(f"{split_name}_schema_version", str(query_id)))
        if record.get("dataset_version") != DATASET_VERSION:
            problems.append(VerifyProblem(f"{split_name}_dataset_version", str(query_id)))
        if record.get("split") != split_name:
            problems.append(VerifyProblem(f"{split_name}_split", str(query_id)))

        target_key = record.get("target_record_key")
        if not isinstance(target_key, str) or not RECORD_KEY_PATTERN.fullmatch(target_key):
            problems.append(VerifyProblem(f"{split_name}_target_record_key", str(query_id)))
            continue
        if target_key in seen_targets:
            problems.append(VerifyProblem(f"{split_name}_target_uniqueness", target_key))
        seen_targets.add(target_key)
        if target_key not in prepared_keys:
            problems.append(VerifyProblem(f"{split_name}_target_in_prepared_corpus", str(query_id)))
            continue

        candidate = candidate_by_key.get(target_key)
        if candidate is None:
            problems.append(VerifyProblem(f"{split_name}_target_is_structurally_valid", str(query_id)))
            continue
        if record.get("stratum") != candidate.stratum:
            problems.append(VerifyProblem(f"{split_name}_stratum", str(query_id)))
        if record.get("source_symptom_sha256") != candidate.source_symptom_sha256:
            problems.append(VerifyProblem(f"{split_name}_source_symptom_sha256", str(query_id)))

        query = record.get("query")
        if not isinstance(query, str):
            problems.append(VerifyProblem(f"{split_name}_query_type", str(query_id)))
            continue
        if not check_query_content_sanity(query):
            problems.append(VerifyProblem(f"{split_name}_query_content", str(query_id)))
        if not check_query_length(query):
            problems.append(VerifyProblem(f"{split_name}_query_length", str(query_id)))
        if record.get("query_sha256") != sha256_text(query):
            problems.append(VerifyProblem(f"{split_name}_query_sha256", str(query_id)))
        if check_answer_leakage(query, candidate.forbidden_terms):
            problems.append(VerifyProblem(f"{split_name}_answer_leakage", str(query_id)))
        if check_conclusion_style_leakage(query):
            problems.append(VerifyProblem(f"{split_name}_answer_style_leakage", str(query_id)))
        if check_excessive_source_overlap(query, candidate.symptom_text):
            problems.append(VerifyProblem(f"{split_name}_source_overlap", str(query_id)))

        normalized_query = normalize_query_for_dedup(query)
        if normalized_query in seen_normalized_queries:
            problems.append(VerifyProblem(f"{split_name}_query_uniqueness", str(query_id)))
        seen_normalized_queries.add(normalized_query)

        expected_id = stable_query_id(split_name, target_key, normalized_query)
        if not isinstance(query_id, str) or query_id != expected_id:
            problems.append(VerifyProblem(f"{split_name}_query_id_stability", str(query_id)))
        if isinstance(query_id, str):
            if query_id in seen_query_ids:
                problems.append(VerifyProblem(f"{split_name}_query_id_uniqueness", query_id))
            seen_query_ids.add(query_id)

    _verify_near_duplicate_queries(records, split_name=split_name, problems=problems)


def verify_frozen_dataset(dataset_dir: Path, prepared_bundle: Path) -> list[VerifyProblem]:
    """只读复算 Schema/数量/泄漏/互斥/哈希，不调用模型、不改文件（doc 02 §9 --verify-only）。"""
    problems: list[VerifyProblem] = []

    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        return [VerifyProblem("manifest_exists", "manifest.json 不存在")]
    try:
        manifest = read_json_file(manifest_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [VerifyProblem("manifest_parse", type(exc).__name__)]
    if not isinstance(manifest, dict):
        return [VerifyProblem("manifest_schema", "manifest.json 必须是 JSON 对象")]

    if manifest.get("schema_version") != SCHEMA_VERSION:
        problems.append(VerifyProblem("schema_version", f"manifest.schema_version != {SCHEMA_VERSION}"))
    if manifest.get("dataset_version") != DATASET_VERSION:
        problems.append(VerifyProblem("dataset_version", f"manifest.dataset_version != {DATASET_VERSION}"))
    if manifest.get("frozen") is not True:
        problems.append(VerifyProblem("frozen_flag", "manifest.frozen 不为 true"))
    if manifest.get("seed") != FIXED_SEED:
        problems.append(VerifyProblem("seed", f"manifest.seed != {FIXED_SEED}"))

    smoke_path = dataset_dir / "smoke.jsonl"
    test_path = dataset_dir / "test.jsonl"
    rejected_path = dataset_dir / "rejected.jsonl"

    artifacts = manifest.get("artifact_sha256")
    if not isinstance(artifacts, dict):
        artifacts = {}
        problems.append(VerifyProblem("artifact_sha256_schema", "manifest.artifact_sha256 必须是对象"))
    for name, path in (("smoke.jsonl", smoke_path), ("test.jsonl", test_path), ("rejected.jsonl", rejected_path)):
        expected_sha = artifacts.get(name)
        if not path.exists():
            problems.append(VerifyProblem(f"{name}_exists", f"{name} 不存在"))
            continue
        actual_sha = sha256_file(path)
        if expected_sha != actual_sha:
            problems.append(VerifyProblem(f"{name}_sha256", f"{name} sha256 与 manifest 不一致"))

    smoke_records = _read_jsonl_for_verification(smoke_path, "smoke.jsonl", problems) if smoke_path.exists() else []
    test_records = _read_jsonl_for_verification(test_path, "test.jsonl", problems) if test_path.exists() else []

    if len(smoke_records) != FIXED_SMOKE_SIZE:
        problems.append(VerifyProblem("smoke_count", f"smoke.jsonl 应恰好 {FIXED_SMOKE_SIZE} 条"))
    if len(test_records) != FIXED_TEST_SIZE:
        problems.append(VerifyProblem("test_count", f"test.jsonl 应恰好 {FIXED_TEST_SIZE} 条"))

    smoke_targets = {r.get("target_record_key") for r in smoke_records}
    test_targets = {r.get("target_record_key") for r in test_records}
    if smoke_targets & test_targets:
        problems.append(VerifyProblem("split_target_exclusion", "smoke/test 共享 target_record_key"))

    staging_manifest_path = prepared_bundle / "manifest.json"
    prepared_cases_path = prepared_bundle / "prepared" / "cases.json"
    if not staging_manifest_path.exists():
        problems.append(VerifyProblem("staging_manifest_exists", str(staging_manifest_path)))
        return problems
    if not prepared_cases_path.exists():
        problems.append(VerifyProblem("prepared_cases_exists", str(prepared_cases_path)))
        return problems
    try:
        staging_manifest = read_json_file(staging_manifest_path)
        prepared_cases = read_json_file(prepared_cases_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        problems.append(VerifyProblem("prepared_bundle_parse", type(exc).__name__))
        return problems
    if not isinstance(staging_manifest, dict) or not isinstance(prepared_cases, list):
        problems.append(VerifyProblem("prepared_bundle_schema", "staging manifest 或 cases.json 类型错误"))
        return problems

    prepared_cases_sha256 = sha256_file(prepared_cases_path)
    staging_manifest_sha256 = sha256_file(staging_manifest_path)
    source = manifest.get("source")
    if not isinstance(source, dict):
        problems.append(VerifyProblem("manifest_source_schema", "manifest.source 必须是对象"))
    else:
        if source.get("prepared_cases_sha256") != prepared_cases_sha256:
            problems.append(VerifyProblem("prepared_cases_sha256", "prepared/cases.json 与 manifest 不一致"))
        if source.get("staging_manifest_sha256") != staging_manifest_sha256:
            problems.append(VerifyProblem("staging_manifest_sha256", "staging manifest 与 manifest 不一致"))
        if source.get("prepared_cases_record_count") != len(prepared_cases):
            problems.append(
                VerifyProblem("prepared_cases_record_count", "prepared/cases.json 记录数与 manifest 不一致")
            )
        if _staging_raw_cases_source(staging_manifest) is None:
            problems.append(VerifyProblem("raw_cases_source", "staging manifest 缺少 cases 原始哈希/大小"))

    staging_problem = validate_staging_manifest(staging_manifest, prepared_cases, prepared_cases_sha256)
    if staging_problem is not None:
        problems.append(VerifyProblem("staging_snapshot", staging_problem))

    candidates, _ = load_structurally_valid_candidates(prepared_cases)
    apply_low_frequency_merge(candidates)
    candidate_by_key = {candidate.record_key: candidate for candidate in candidates}
    prepared_keys: set[str] = set()
    for record in prepared_cases:
        if not isinstance(record, dict) or record.get("entry_type") != "case":
            continue
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            continue
        record_key = metadata.get("record_key")
        if isinstance(record_key, str):
            prepared_keys.add(record_key)
    _verify_split_records("smoke", smoke_records, candidate_by_key, prepared_keys, problems)
    _verify_split_records("test", test_records, candidate_by_key, prepared_keys, problems)

    smoke_normalized = {
        normalize_query_for_dedup(record["query"]) for record in smoke_records if isinstance(record.get("query"), str)
    }
    test_normalized = {
        normalize_query_for_dedup(record["query"]) for record in test_records if isinstance(record.get("query"), str)
    }
    if smoke_normalized & test_normalized:
        problems.append(VerifyProblem("split_query_exclusion", "smoke/test 共享 normalized_query"))
    for smoke_record in smoke_records:
        smoke_query = smoke_record.get("query")
        if not isinstance(smoke_query, str):
            continue
        for test_record in test_records:
            test_query = test_record.get("query")
            if not isinstance(test_query, str):
                continue
            if (
                jaccard_similarity(normalize_query_for_dedup(smoke_query), normalize_query_for_dedup(test_query))
                > NEAR_DUPLICATE_JACCARD
            ):
                problems.append(
                    VerifyProblem(
                        "split_near_duplicate_query",
                        f"{smoke_record.get('query_id', '?')}~{test_record.get('query_id', '?')}",
                    )
                )

    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        problems.append(VerifyProblem("counts_schema", "manifest.counts 必须是对象"))
    elif counts.get("smoke") != len(smoke_records) or counts.get("test") != len(test_records):
        problems.append(VerifyProblem("counts", "manifest.counts 与 JSONL 实际数量不一致"))

    return problems


# ---------------------------------------------------------------------------
# build 子命令主流程
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def run_build(config: BuildConfig) -> int:
    validate_build_config(config)

    if dataset_dir_is_frozen(config.output_dir):
        print(f"输出目录已冻结（manifest.frozen=true），拒绝覆盖: {config.output_dir}", file=sys.stderr)
        return 1

    staging_manifest_path = config.prepared_bundle / "manifest.json"
    prepared_cases_path = config.prepared_bundle / "prepared" / "cases.json"
    if not staging_manifest_path.exists() or not prepared_cases_path.exists():
        print(f"隔离 staging 缺少 manifest.json 或 prepared/cases.json: {config.prepared_bundle}", file=sys.stderr)
        return 1

    staging_manifest = read_json_file(staging_manifest_path)
    if not isinstance(staging_manifest, dict):
        print("staging manifest 必须是 JSON 对象", file=sys.stderr)
        return 1
    staging_manifest_sha256 = sha256_file(staging_manifest_path)
    prepared_cases = read_json_file(prepared_cases_path)
    if not isinstance(prepared_cases, list):
        print("prepared/cases.json 必须是 JSON 数组", file=sys.stderr)
        return 1
    prepared_cases_sha256 = sha256_file(prepared_cases_path)
    staging_problem = validate_staging_manifest(staging_manifest, prepared_cases, prepared_cases_sha256)
    if staging_problem is not None:
        print(f"staging 快照校验失败: {staging_problem}", file=sys.stderr)
        return 1

    candidates, _structural_rejections = load_structurally_valid_candidates(prepared_cases)
    apply_low_frequency_merge(candidates)
    grouped = group_by_stratum(candidates, config.seed)
    structural_counts = structural_counts_by_stratum(grouped)

    from app.core.config import get_settings
    from app.core.gateway import ModelGatewayClient
    from app.core.rewrite_gateway import build_rewrite_gateway_settings

    settings = get_settings()
    configured_query_model = settings.rag_query_rewrite_model or settings.chat_model
    if config.query_model and config.query_model != configured_query_model:
        print("--query-model 必须与当前有效的 rewrite 模型一致", file=sys.stderr)
        return 1
    if settings.rag_query_rewrite_model_temperature != QUERY_MODEL_TEMPERATURE:
        print(
            f"rag_query_rewrite_model_temperature 必须为 {QUERY_MODEL_TEMPERATURE}（rag-silver-v1 固定合同）",
            file=sys.stderr,
        )
        return 1

    rewrite_gateway_settings = build_rewrite_gateway_settings(settings)
    gateway = ModelGatewayClient(settings=rewrite_gateway_settings or settings)
    query_model = configured_query_model
    query_gateway_mode = (
        "dedicated_rewrite_gateway" if rewrite_gateway_settings is not None else "default_model_gateway"
    )
    generator = QueryGenerator(
        gateway,
        model=query_model,
        temperature=QUERY_MODEL_TEMPERATURE,
        max_tokens=settings.rag_query_rewrite_model_max_tokens,
    )

    test_quota = largest_remainder_allocation(structural_counts, config.test_size)
    accepted_normalized_queries: list[str] = []
    accepted_target_keys: set[str] = set()

    test_result, test_cursors = await build_split(
        grouped,
        test_quota,
        generator=generator,
        split="test",
        target_size=config.test_size,
        accepted_normalized_queries=accepted_normalized_queries,
        accepted_target_keys=accepted_target_keys,
    )

    remaining_grouped = remaining_candidates_after(grouped, test_cursors)
    remaining_structural_counts = structural_counts_by_stratum(remaining_grouped)
    smoke_quota = largest_remainder_allocation(remaining_structural_counts, config.smoke_size)

    smoke_result, _smoke_cursors = await build_split(
        remaining_grouped,
        smoke_quota,
        generator=generator,
        split="smoke",
        target_size=config.smoke_size,
        accepted_normalized_queries=accepted_normalized_queries,
        accepted_target_keys=accepted_target_keys,
    )

    if len(test_result.accepted) != config.test_size:
        print(f"正式集配额未填满：{len(test_result.accepted)}/{config.test_size}", file=sys.stderr)
        return 1
    if len(smoke_result.accepted) != config.smoke_size:
        print(f"Smoke 配额未填满：{len(smoke_result.accepted)}/{config.smoke_size}", file=sys.stderr)
        return 1

    mutual_exclusion_problems = check_split_mutual_exclusion(smoke_result.accepted, test_result.accepted)
    if mutual_exclusion_problems:
        print(f"smoke/test 互斥检查失败: {mutual_exclusion_problems}", file=sys.stderr)
        return 1

    test_sorted = sorted(test_result.accepted, key=lambda item: final_sort_key(config.seed, item.target_record_key))
    smoke_sorted = sorted(smoke_result.accepted, key=lambda item: final_sort_key(config.seed, item.target_record_key))
    all_rejected = test_result.rejected + smoke_result.rejected

    config.output_dir.mkdir(parents=True, exist_ok=True)
    test_path = config.output_dir / "test.jsonl"
    smoke_path = config.output_dir / "smoke.jsonl"
    rejected_path = config.output_dir / "rejected.jsonl"

    write_jsonl_atomic(test_path, [accepted_to_jsonl_record(item, split="test") for item in test_sorted])
    write_jsonl_atomic(smoke_path, [accepted_to_jsonl_record(item, split="smoke") for item in smoke_sorted])
    write_jsonl_atomic(rejected_path, [rejected_to_jsonl_record(item) for item in all_rejected])

    git_head, git_dirty = git_head_and_dirty(_PROJECT_ROOT)
    manifest = build_manifest(
        config=config,
        staging_manifest=staging_manifest,
        staging_manifest_sha256=staging_manifest_sha256,
        prepared_cases_sha256=prepared_cases_sha256,
        prepared_cases_count=len(prepared_cases),
        git_head=git_head,
        git_dirty=git_dirty,
        generated_at=timestamp_now(),
        query_model=query_model,
        query_gateway_max_tokens=settings.rag_query_rewrite_model_max_tokens,
        query_gateway_mode=query_gateway_mode,
        structural_counts=structural_counts,
        test_quota=test_quota,
        smoke_quota=smoke_quota,
        test_redistributions=test_result.redistributions,
        smoke_redistributions=smoke_result.redistributions,
        test_accepted=test_sorted,
        smoke_accepted=smoke_sorted,
        all_rejected=all_rejected,
        smoke_path_sha256=sha256_file(smoke_path),
        test_path_sha256=sha256_file(test_path),
        rejected_path_sha256=sha256_file(rejected_path),
        mutual_exclusion_problems=mutual_exclusion_problems,
    )
    write_json_atomic(config.output_dir / "manifest.json", manifest)

    print(f"构建完成：test={len(test_sorted)} smoke={len(smoke_sorted)} rejected={len(all_rejected)}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="build_rag_silver_eval")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="生成并冻结 rag-silver-v1 数据集")
    build_parser.add_argument("--prepared-bundle", type=Path, required=True)
    build_parser.add_argument("--output-dir", type=Path, required=True)
    build_parser.add_argument("--seed", type=int, required=True)
    build_parser.add_argument("--smoke-size", type=int, required=True)
    build_parser.add_argument("--test-size", type=int, required=True)
    build_parser.add_argument("--query-model", type=str, default="")

    verify_parser = subparsers.add_parser("verify", help="只读复算已冻结数据集")
    verify_parser.add_argument("--dataset-dir", type=Path, required=True)
    verify_parser.add_argument("--prepared-bundle", type=Path, required=True)

    return parser


async def _main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "build":
        config = BuildConfig(
            prepared_bundle=args.prepared_bundle,
            output_dir=args.output_dir,
            seed=args.seed,
            smoke_size=args.smoke_size,
            test_size=args.test_size,
            query_model=args.query_model,
        )
        return await run_build(config)

    if args.command == "verify":
        problems = verify_frozen_dataset(args.dataset_dir, args.prepared_bundle)
        if problems:
            for problem in problems:
                print(f"[FAIL] {problem.check}: {problem.detail}", file=sys.stderr)
            print(f"verify 失败，共 {len(problems)} 项问题", file=sys.stderr)
            return 1
        print("verify 通过：数据集与 manifest 一致")
        return 0

    parser.error(f"未知子命令: {args.command}")
    return 2


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
