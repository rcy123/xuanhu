"""Focused contract tests for the hard patient-style dataset builder."""

from __future__ import annotations

from scripts import build_rag_hard_patient_eval as hard


def test_copy_gate_measures_contiguous_cjk_spans() -> None:
    source = "近来饭后腹部胀痛，受凉以后更明显，夜间睡得不安稳。"
    query = "我这几天一吃完东西肚子就不舒服，着凉后会更严重，晚上也睡不好。"

    jaccard, copy_rate, max_span = hard._char4_copy_metrics(query, source)

    assert 0.0 <= jaccard <= 1.0
    assert 0.0 <= copy_rate <= 1.0
    assert max_span < 6


def test_fidelity_parser_requires_all_gate_conditions() -> None:
    accepted = hard._parse_fidelity(
        '{"supported":true,"salient_fact_count":2,"unsupported_claim":false,"patient_voice":true}'
    )
    rejected = hard._parse_fidelity(
        '{"supported":true,"salient_fact_count":1,"unsupported_claim":false,"patient_voice":true}'
    )

    assert accepted.passed is True
    assert accepted.reason_codes == ()
    assert rejected.passed is False
    assert rejected.reason_codes == ("insufficient_salient_facts",)


def test_hard_manifest_rejects_same_generator_and_judge_model() -> None:
    manifest = {
        "hardening": {
            "variant": hard.HARD_VARIANT,
            "lexical_gate": {
                "query_min_chars": hard.HARD_QUERY_MIN_CHARS,
                "query_max_chars": hard.HARD_QUERY_MAX_CHARS,
                "max_char4_jaccard": hard.MAX_CHAR4_JACCARD,
                "max_query_char4_copy_rate": hard.MAX_QUERY_CHAR4_COPY_RATE,
                "max_copied_cjk_span": hard.MAX_COPIED_CJK_SPAN,
            },
            "generator": {"prompt_version": hard.GENERATOR_PROMPT_VERSION, "model": "same"},
            "fidelity_judge": {"prompt_version": hard.JUDGE_PROMPT_VERSION, "model": "same"},
        }
    }

    problems = hard._validate_hard_manifest(manifest)

    assert any(problem.check == "hardening_model_independence" for problem in problems)
