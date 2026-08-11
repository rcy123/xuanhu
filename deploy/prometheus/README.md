# Xuanhu monitoring

Two Prometheus scrape endpoints are exported by the backend:

- `GET /api/v1/metrics` — performance histograms plus the bounded R5/R9 outcome
  counters (gateway requests, structured-output fallback, safety decisions,
  question-contract, coverage-evaluation, and follow-up outcomes).
- `GET /api/v1/metrics/outbox` — durable Outbox health gauges.

Both return the Prometheus 0.0.4 text format with the exact content type
`text/plain; version=0.0.4; charset=utf-8`.

Example scrape job:

```yaml
scrape_configs:
  - job_name: xuanhu-api
    metrics_path: /api/v1/metrics/outbox
    static_configs:
      - targets: ["xuanhu-api:8000"]
```

Load `rules/xuanhu-outbox-alerts.yml` through Prometheus `rule_files`. The
backlog-age and dead-letter rules compare live aggregate gauges with threshold
gauges exported from `OUTBOX_READY_MAX_OLDEST_AGE_SECONDS` and
`OUTBOX_READY_MAX_DEAD_LETTERS`; this keeps readiness and alerting on one
configuration contract.

`OUTBOX_PUBLISHER_ENABLED=false` is an explicit degraded/critical state: the
readiness endpoint returns HTTP 503 and the publisher-disabled alert fires.
The missing-metrics rule is matched per `job="xuanhu-api"` and `instance`, so
one healthy replica cannot hide another replica that is down or omits metrics.

```yaml
rule_files:
  - /etc/prometheus/rules/xuanhu-outbox-alerts.yml
```

The endpoint exposes counts, age, configured thresholds, publisher state, and
health availability only. It has no dynamic labels and never exports session,
event, run, user, clinical, payload, exception, or credential data.

## R5 operational outcome counters

`GET /api/v1/metrics` also exports three bounded, low-cardinality counters
instrumented on the production call paths:

| Metric | Labels | Counting semantics |
| ------ | ------ | ------------------ |
| `xuanhu_gateway_requests_total` | `operation` ∈ {`chat`, `chat_structured`, `embed`}, `outcome` ∈ {`success`, `error`, `truncated`, `parse_failed`} | Exactly one increment per top-level `chat`/`chat_structured`/`embed` call. `error` covers gateway transport/response failures; `truncated` and `parse_failed` are structured-output terminal failures kept separate. Recorded on every return and every gateway raise — the original exception is never masked. |
| `xuanhu_gateway_structured_fallback_total` | `outcome` ∈ {`attempted`, `success`, `failure`} | One increment per JSON-mode structured fallback attempt (legacy, unbounded caller path). Because a retrying caller falls back on each attempt, the `attempted` count can exceed the number of top-level structured calls; the fallback-rate alert therefore reads it as **fallback attempts per structured call**, not percent of calls. `success`/`failure` are parse-level resolutions; a transport error mid-fallback surfaces at request level as `error` instead. |
| `xuanhu_safety_checks_total` | `outcome` ∈ {`passed`, `blocked`} | One increment per authoritative `SafetyRuleEngine` decision, observed only after the passed/blocked decision exists. Advisory pre-checks are excluded so the gate is not double counted. |

Every label value is drawn from a fixed allowlist defined in `app/core/metrics.py`.
Any unexpected value is fail-closed to a fixed `unknown` bucket — it can never
create a new time series — and the gateway duration histograms carry no dynamic
labels. The endpoint and content type are unchanged.

## R9 question-contract outcome counters

`GET /api/v1/metrics` also exports three bounded, low-cardinality counters for
the R9 question-contract flow, plus one label-free aspect-count histogram:

| Metric | Labels | Counting semantics |
| ------ | ------ | ------------------ |
| `xuanhu_question_contracts_total` | `outcome` ∈ {`created`, `degraded`, `rejected`, `integrity_error`} | One increment per terminal question-contract creation outcome. `integrity_error` is an internal consistency failure during contract assembly. |
| `xuanhu_question_coverage_evaluations_total` | `outcome` ∈ {`satisfied`, `partial`, `no_progress`, `ambiguous`, `unable`, `invalid`, `error`} | One increment per coverage-fold evaluation of a question. `invalid` and `error` feed the coverage-failure-rate alert. |
| `xuanhu_question_contract_followups_total` | `outcome` ∈ {`asked`, `cap_reached`, `manual`} | One increment per residual follow-up decision. `cap_reached` marks a follow-up suppressed by the follow-up cap. |
| `xuanhu_question_contract_aspects` | (none) | Histogram of how many coverage aspects are frozen into each contract, with integer-oriented buckets. |

The R9 allowlists live in `app/core/metrics.py` alongside the R5 ones and carry
the same fail-closed `unknown` guarantee. No R9 metric carries session,
dimension, or free-form text labels.

## Alert rules

- `rules/xuanhu-outbox-alerts.yml` — durable Outbox readiness/dead-letter rules.
- `rules/xuanhu-r5-alerts.yml` — model-drift rules with minimum-volume guards:
  `XuanhuStructuredTerminalFailureRateHigh`, `XuanhuStructuredFallbackRateHigh`,
  `XuanhuStructuredFallbackFailureRateHigh`, and `XuanhuSafetyBlockRateDrift`.
  Each carries `severity`/`service`/`component` labels and a `runbook`
  annotation, with no PHI labels. Structured terminal failure, structured
  fallback/failure, and safety block-rate drift are computed from the bounded
  counters above.
- `rules/xuanhu-r9-alerts.yml` — question-contract rules:
  `XuanhuQuestionCoverageFailureRateHigh` (coverage invalid/error ratio),
  `XuanhuQuestionCapReachedHigh` (follow-up cap exhaustion), and
  `XuanhuQuestionContractIntegrityError`. Each carries the same
  `severity`/`service`/`component` labels and `runbook` annotation with a
  minimum-volume guard, computed from the bounded R9 counters above.

Load all three rule files through Prometheus `rule_files`.

Validate both syntax and behavior from the repository root:

```bash
promtool check rules deploy/prometheus/rules/xuanhu-outbox-alerts.yml
promtool test rules deploy/prometheus/tests/xuanhu-outbox-alerts.test.yml
promtool check rules deploy/prometheus/rules/xuanhu-r5-alerts.yml
promtool test rules deploy/prometheus/tests/xuanhu-r5-alerts.test.yml
promtool check rules deploy/prometheus/rules/xuanhu-r9-alerts.yml
promtool test rules deploy/prometheus/tests/xuanhu-r9-alerts.test.yml
```

CI validates all six commands; the unit suite independently guards the R5 and
R9 rule structure and PHI-free labels (`tests/test_r5_metrics.py`,
`tests/test_r9_question_contract_metrics.py`).
