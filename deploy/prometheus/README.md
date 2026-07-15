# Xuanhu Outbox monitoring

Scrape the backend at `GET /api/v1/metrics/outbox`. The endpoint returns the
Prometheus 0.0.4 text format with the exact content type
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

Validate both syntax and behavior from the repository root:

```bash
promtool check rules deploy/prometheus/rules/xuanhu-outbox-alerts.yml
promtool test rules deploy/prometheus/tests/xuanhu-outbox-alerts.test.yml
```
