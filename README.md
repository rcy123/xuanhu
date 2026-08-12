<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./image.png">
    <img alt="Xuanhu" src="./image.png" width="128" height="128" style="border-radius: 24px;">
  </picture>
</p>

<h1 align="center">悬壶 · Xuanhu</h1>

<p align="center">
  <strong>中医 AI 辅助诊疗工作台</strong><br>
  <em>AI-Powered Traditional Chinese Medicine Clinical Assistant</em>
</p>

<p align="center">
  <a href="#-features">Features</a> •
  <a href="#-architecture">Architecture</a> •
  <a href="#-tech-stack">Tech Stack</a> •
  <a href="#-getting-started">Getting Started</a> •
  <a href="#-project-structure">Project Structure</a> •
  <a href="#-development">Development</a> •
  <a href="#-safety--compliance">Safety & Compliance</a>
</p>

---

**悬壶（Xuanhu）** is an AI-powered clinical assistant designed for licensed Traditional Chinese Medicine (TCM) practitioners. It provides a complete clinical workflow — from patient inquiry and syndrome differentiation (辨证) to formula drafting (开方), safety review, and medical record generation — all within a structured, auditable digital workspace.

> ⚕️ **辅助决策工具** — all outputs are for reference only and require confirmation by a licensed TCM practitioner.

---

## ✨ Features

### 🔄 End-to-End Clinical Workflow

Navigate the full TCM consultation lifecycle in one seamless workspace:

| Stage | Description |
|---|---|
| **Inquiry** (问诊) | AI-assisted structured patient interview with completeness tracking, clarification recovery, and slot-level question contracts (R9) |
| **Syndrome Differentiation** (辨证) | Multi-agent syndrome analysis with RAG evidence grounding and verifier chain |
| **Formula Drafting** (开方) | Two-stage drafting: base formula selection → personalized modification (加减方) |
| **Formula Selection** (医师选方) | Multi-alternative display with confidence scoring — doctor selects from AI-generated options |
| **Safety Review** (安全审核) | Deterministic rule engine checks 10+ safety dimensions with rollback guidance |
| **Doctor Review** (医师确认) | Mandatory human-in-the-loop confirmation with modify/reject capabilities |
| **Medical Record** (病历生成) | Structured consultation documentation export |

### 🤖 Multi-Agent LangGraph Runtime

Built on **LangGraph**, the agent orchestration layer manages:

- **Intake Agent** — Structured symptom & sign collection, completeness evaluation, ABSTAINED routing, and clarification recovery
- **SyndromeDraft Agent** — Syndrome differentiation (辨证) with RAG evidence grounding, verifier chain validation, and authority snapshot caching
- **FormulaDraft Agent** — Two-stage formula generation: base formula drafting from syndrome → personalized modification (加减方) with evidence-grounded adjustments
- **Safety Agent** — Prescription safety verification gate with deterministic rule engine and rollback guidance
- **Recovery Agent** — Graceful error handling and state recovery with deadlock prevention
- **Triage Policy** — Automated collection sufficiency determination with cached gate computation

### 🎯 Question Contract — Slot-Level Inquiry Completeness (R9)

A generic contract layer that guarantees no question is silently half-answered — the system remembers exactly what it asked and refuses to close a dimension on a coarse reply:

- **Frozen contracts** — every question freezes 1–4 verifiable coverage aspects (slots) as an immutable contract; an answer produces an append-only coverage event ledger
- **Deterministic Rubric planning** — all 11 ten-question dimensions (寒热/汗出/头身/二便/饮食/胸腹/口渴/睡眠/呼吸/疼痛/月经带下) ship versioned Rubrics: required slots always asked, conditional slots freeze only on *positive* facts (a "睡眠正常" fact never pulls 多梦易醒; a complaint "失眠一周" does)
- **Residual follow-up chains** — a partial answer ("有痰" without color/amount) forces a follow-up that re-asks only the missing slots until the chain converges or the cap is reached — no more skipped questions
- **Semantic sufficiency validation** — 31 conservative wordlists downgrade an over-claimed "addressed" to `unclear` when the evidence lacks the required terms (e.g. "有痰" cannot satisfy 痰色), keeping the slot in the residual loop; 降级不误伤、不 reject、不扩拒绝面
- **Privacy-masked evidence** — identity sequences (phone numbers etc.) are masked with equal-length `█` before the model; a shared mask-wildcard matcher keeps evidence verification aligned while persisted digests always cover the **raw** text, never the masked rendering
- **Observability** — bounded counters/histograms for contract creation, coverage folds, and follow-up decisions, plus 6 Prometheus alert rules (degraded / partial / manual-required / cap-reached / integrity-error / failure-rate)

### 🧠 Intelligent RAG Pipeline

All agent reasoning is grounded in a structured TCM knowledge base via a multi-stage retrieval pipeline:

| Stage | Component | Description |
|---|---|---|
| **Rewrite** | Query Rewrite Gateway | Lightweight model (Qwen3.5-2B-free) rewrites clinical queries for better retrieval recall (304–845ms) |
| **Retrieve** | Hybrid Search | Milvus vector search + PostgreSQL full-text search, 8-way concurrent with shared `RAGRetriever` |
| **Rerank** | Multi-Tier Reranker | MVP weighted sum → Cross-Encoder API (e.g., jina-reranker-m0) → LLM Reranker (0–10 scoring) |
| **Evidence** | Structured Evidence | Ranked, traceable `Evidence` objects with source priority, relevance scores, and chunk provenance |

> 🔄 **Graceful degradation** — If Cross-Encoder or LLM reranker calls fail, the system automatically falls back to MVP weighted scoring without blocking the pipeline.

### 📊 Multi-Formula Alternative Selection (医师选方)

The formula drafting stage generates **multiple alternative base formulas** — each with a distinct therapeutic angle (侧重), confidence score, and herb composition — empowering the practitioner to choose the most appropriate approach:

- **Multi-angle generation** — AI produces 2–4 base formula candidates, each emphasizing different treatment priorities
- **Confidence scoring** — Each alternative includes a 0–100% confidence score with color-coded indicators
- **Side-by-side comparison** — Full herb composition tables, rationale, and therapeutic angle displayed for each option
- **One-click select** — Practitioner selects the preferred alternative; the system proceeds with personalized modification (加减方)

### ⚡ Embedding Cache Preheat

Three-layer cache warm-up eliminates cold-start latency for vector embeddings:

| Layer | Scope | Description |
|---|---|---|
| **L1 Entity** | Herb + Formula titles | Pre-embeds all known entity names from the knowledge base |
| **L2 Template** | Entity × Query templates | Pre-computes embeddings for common query patterns (e.g., "{herb} 的功效与禁忌") |
| **L3 Runtime** | Live query patterns | Cache populated during normal agent operations via Redis |

**Results**: ~60% cache hit rate in TCM consultation scenarios, **89–209×** speedup on cache hits (~4ms Redis vs ~570ms gateway RTT).

```bash
# Full preheat (L1 + L2)
uv run python scripts/prewarm_embedding_cache.py --all

# Benchmark before/after
uv run python scripts/prewarm_embedding_cache.py --all --benchmark

# Check cache statistics
uv run python scripts/prewarm_embedding_cache.py --stats
```

### 🛡️ Deterministic Safety Engine

A comprehensive, LLM-free safety rule engine checks every prescription against:

| Rule | Severity |
|---|---|
| **Eighteen Incompatibilities** (十八反) | 🔴 Blocker |
| **Nineteen Fears** (十九畏) | 🔴 Blocker |
| **Pregnancy Contraindications** (妊娠禁忌) | 🔴 Blocker / 🟠 High |
| **Combination Incompatibilities** (配伍禁忌) | 🔴 Blocker |
| **Dose Limits** (剂量上限, per Chinese Pharmacopoeia) | 🟠 High / 🔴 Blocker |
| **Allergy Check** (过敏检查) | 🔴 Blocker |
| **Unit Conversion** (剂量单位换算) | 🟡 Warning |
| **Unknown Herb Detection** | 🟠 High |

> ✅ No LLM involvement in safety decisions — rules are pure deterministic functions backed by a structured herb knowledge base.

### 🖥️ Modern Clinical Workspace

- **Responsive sidebar** with session list and collapsible navigation
- **Real-time streaming** of agent reasoning via SSE
- **Stage progress bar** showing current workflow phase
- **Multi-formula alternative cards** — compare 2–4 base formula options with confidence scores and therapeutic angles before selecting
- **Interactive formula editing** with safety validation and side-by-side base vs. modified comparison
- **Doctor review panel** — accept, modify, or reject prescriptions
- **Safety confirmation** workflow for unresolved assertions with rollback guidance
- **Medical record preview** with structured TCM documentation

---

## 🏗 Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    Frontend (React)                       │
│            xuanhu-ui · Vite · Ant Design                  │
└──────────────────────────┬───────────────────────────────┘
                           │ REST + SSE
┌──────────────────────────▼───────────────────────────────┐
│                  Backend API (FastAPI)                     │
│                app/api · 10+ route modules                 │
└──────────────────────────┬───────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────┐
│              Agent Runtime (LangGraph)                     │
│  ┌────────┐ ┌───────────┐ ┌──────────┐ ┌──────────┐     │
│  │ Intake │ │ 辨证-开方  │ │  Safety  │ │ Recovery │     │
│  │Subgraph│ │三Agent流水 │ │   Gate   │ │ Subgraph │     │
│  │        │ │Syndrome→  │ │          │ │          │     │
│  │        │ │Formula→   │ │          │ │          │     │
│  │        │ │Modification│ │          │ │          │     │
│  └────────┘ └───────────┘ └──────────┘ └──────────┘     │
└──────────────────────────┬───────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────┐
│                    Infrastructure                           │
│  ┌──────────┐ ┌─────┐ ┌──────────┐ ┌──────────────────┐  │
│  │PostgreSQL│ │Redis│ │  Milvus  │ │   Model Gateway  │  │
│  │   16     │ │  7  │ │ (Vector) │ │  ┌────────────┐  │  │
│  │          │ │     │ │          │ │  │Main (mimo) │  │  │
│  │          │ │     │ │          │ │  ├────────────┤  │  │
│  │          │ │     │ │          │ │  │Rewrite(Qwen)│  │  │
│  │          │ │     │ │          │ │  ├────────────┤  │  │
│  │          │ │     │ │          │ │  │Reranker   │  │  │
│  │          │ │     │ │          │ │  ├────────────┤  │  │
│  │          │ │     │ │          │ │  │Embedding  │  │  │
│  └──────────┘ └─────┘ └──────────┘ └──┴────────────┘  │  │
└──────────────────────────────────────────────────────────┘
```

### Key Design Decisions

- **Domain-Driven Design** with Command/Query Responsibility Segregation (CQRS) and outbox event pattern for reliable event publishing
- **Idempotent HTTP Commands** — all state-mutating operations use idempotency keys with conflict detection
- **Snapshot-based Read Models** — session state is projected into read-optimized materialized views
- **Shared LangGraph Runtime** — a lifespan-managed compiled graph instance shared across requests
- **Safety-first** — deterministic rules are always enforced before any LLM output reaches the user
- **Multi-Gateway Architecture** — dedicated gateways for main reasoning, query rewriting (Qwen3.5-2B), embedding, and reranker, each independently configurable with fallback chains
- **Authority Snapshot Caching** — reasoning agent authority snapshots cached at commit with invalidation on write, reducing DB round-trips by 60–70%
- **Three-Tier Embedding Cache** — entity → template → runtime warming via Redis, achieving ~60% hit rate and ~4ms retrieval
- **Two-Stage Formula Drafting** — base formula selection (multi-alternative, practitioner-chosen) → personalized modification (加减方), separated for clinical transparency
- **Deterministic Question Contracts (R9)** — every question freezes verifiable aspects into an immutable contract; answers fold into an append-only coverage ledger; residual follow-ups re-ask only the missing slots until convergence, so a coarse reply can never silently close a clinical dimension
- **Lease-fenced Durable Commands** — both the durable async-command worker and the HTTP idempotency executor run handlers under a shared monotonic lease guard (`app/services/lease_guard.py`). A handler may only keep writing while it can renew its lease; if the owner token/status is lost or the local deadline is exhausted, the stale handler is cancelled/drained and the executor fails closed (`HTTP_COMMAND_RECOVERY_REQUIRED`) instead of settling a stale clinical write. Operators tune the async worker's lease/heartbeat timing through its environment settings (production defaults: heartbeat 20s / lease 60s). The HTTP executor's lease/heartbeat timing is fixed in production (20s / 90s); its constructor kwargs are an internal/test injection seam, not an operator configuration surface.

---

## 🛠 Tech Stack

### Backend

| Category | Technology |
|---|---|
| Framework | [FastAPI](https://fastapi.tiangolo.com/) |
| Runtime | Python 3.12 |
| Agent Orchestration | [LangGraph](https://www.langchain.com/langgraph) |
| Database ORM | SQLAlchemy 2.0 + Alembic |
| Database | PostgreSQL 16 |
| Cache / Locking | Redis 7 |
| Vector Store | Milvus 2.5 (via etcd + MinIO) |
| Main LLM Gateway | deepseek-v4-flash-0731 @ dmxapi (multi-gateway, independently configured) |
| Rewrite Gateway | Lightweight LLM (e.g. qwen3-8b) for RAG query rewriting |
| Reranker | Cross-Encoder (e.g. jina-reranker-m0) + LLM fallback |
| Embedding | Qwen3-Embedding-8B via dedicated embedding gateway |
| Validation | Pydantic v2 |
| Linting | Ruff + mypy (strict mode) |

### Frontend

| Category | Technology |
|---|---|
| Framework | React 19 |
| Build Tool | Vite 8 |
| UI Library | Ant Design 6 |
| Language | TypeScript 6 |
| Routing | React Router 7 |
| Testing | Vitest + Testing Library + Playwright |
| Linting | oxlint |

### DevOps

| Tool | Purpose |
|---|---|
| Docker Compose | Local middleware orchestration |
| Prometheus | Monitoring (15 alert rules: outbox / R5 safety / R9 question-contract) |
| gitleaks | Secret scanning |

---

## 🚀 Getting Started

### Prerequisites

- Python 3.12+
- Node.js 20+
- Docker & Docker Compose
- [uv](https://docs.astral.sh/uv/) (Python package manager)

### 1. Clone & Configure

```bash
git clone https://github.com/yourusername/xuanhu.git
cd xuanhu

# Environment configuration
cp .env.example .env
# Edit .env with your settings (model gateway URL, database credentials, etc.)
```

### 2. Start Infrastructure

```bash
docker compose up -d
```

This starts PostgreSQL 16, Redis 7, Milvus (with etcd + MinIO).

### 3. Backend Setup

```bash
# Create virtual environment and install dependencies
uv sync

# Run database migrations
uv run alembic upgrade head

# Seed knowledge base data (optional)
uv run python scripts/seed_data.py

# Start the API server
uv run xuanhu-api
```

The API is available at `http://localhost:8000` with interactive docs at `/docs`.

### 4. Frontend Setup

```bash
cd frontend
npm install
npm run dev
```

The frontend is available at `http://localhost:5173`.

### 5. Verify

```bash
# Health check
curl http://localhost:8000/api/v1/health

# Readiness check
curl http://localhost:8000/api/v1/health/ready
```

---

## 📁 Project Structure

```
xuanhu/
├── app/                          # Backend application
│   ├── agent_runtime/            # LangGraph agent orchestration
│   │   ├── graph.py              # Main state graph construction
│   │   ├── intake_subgraph.py    # Patient intake & symptom collection
│   │   ├── reasoning_subgraph.py # Syndrome differentiation & formula
│   │   ├── review_node.py        # Review gate subgraph
│   │   ├── recovery_node.py      # Error recovery subgraph
│   │   ├── routing.py            # Command routing logic
│   │   ├── runner.py             # Graph execution runner
│   │   ├── state.py             # Global graph state definition
│   │   ├── context_builder.py   # LLM context assembly
│   │   ├── repository.py        # Domain repository (CQRS + outbox, fail-closed envelope)
│   │   ├── checkpoint.py        # PostgreSQL-backed checkpointer
│   │   ├── reducer.py           # Domain state reduction & append-only ledgers
│   │   ├── question_contract.py # R9 question contract fold & residual chains
│   │   ├── question_rubric.py   # R9 per-dimension Rubrics (11 ten-question dims)
│   │   ├── coverage_semantics.py# R9 semantic sufficiency wordlists
│   │   ├── contract_projection.py # R9 contract → dimension projection
│   │   ├── intake_verifier.py   # Intake output verification chain
│   │   ├── intake_grounding.py  # Evidence grounding & privacy mask handling
│   │   ├── completeness_policy.py # Required-dimension completeness evaluation
│   │   ├── async_command*.py    # Durable async command worker & lifecycle
│   │   ├── sandbox_*.py         # Sandboxed evaluation modules
│   │   └── verifiers.py         # Shared output validation chain
│   ├── agents/                   # LLM agent prompts & logic
│   │   ├── syndrome_draft.py    # 辨证草稿 Agent (L4-1)
│   │   ├── formula_draft.py     # 开方+加减 Agent (L4-2, two-stage)
│   │   ├── prompts/             # Jinja2 prompt templates (23)
│   │   │   └── manifest.yaml    # Prompt registry
│   │   └── prompt_loader.py     # Prompt template loader
│   ├── api/                      # REST API routes
│   │   ├── sessions.py           # Session CRUD
│   │   ├── messages.py           # Message management
│   │   ├── advance.py            # Stage advancement (idempotent)
│   │   ├── review.py             # Doctor review endpoints
│   │   ├── stream.py             # SSE streaming
│   │   ├── record.py             # Medical record operations
│   │   └── recovery.py           # Session recovery
│   ├── core/                     # Core configuration & infrastructure
│   │   ├── config.py             # Settings (pydantic-settings)
│   │   ├── gateway.py            # Main model gateway client
│   │   ├── rewrite_gateway.py    # Query rewrite gateway config
│   │   ├── reranker_gateway.py   # Reranker gateway config
│   │   ├── embedding_gateway.py  # Embedding gateway config
│   │   └── redis.py              # Redis client
│   ├── rag/                      # Retrieval-Augmented Generation
│   │   ├── retriever.py          # Hybrid search: Milvus + PG full-text
│   │   ├── reranker.py           # Multi-tier reranker (MVP/Cross-Encoder/LLM)
│   │   ├── embedding_cache.py    # Redis-backed embedding cache
│   │   ├── entity_index.py       # Entity-level indexing
│   │   ├── reasoning_retrieval.py # Agent-triggered RAG retrieval
│   │   └── schemas.py            # RAG data structures (Evidence, MergedHit)
│   ├── db/                       # Database session management
│   ├── models/                   # SQLAlchemy ORM models
│   ├── safety/                   # Deterministic safety engine
│   │   ├── engine.py             # Core rule engine (10+ rules)
│   │   ├── datasets.py           # Herb compatibility tables
│   │   ├── normalizer.py         # Herb name normalization
│   │   └── rule_version.py       # Rule version tracking
│   ├── schemas/                  # Pydantic models (API layer)
│   └── services/                 # Business logic service layer
├── frontend/                     # React SPA frontend
│   └── src/
│       ├── components/           # UI components
│       │   ├── ChatPanel.tsx     # Main consultation panel
│       │   ├── SessionSider.tsx  # Session navigation sidebar
│       │   ├── SessionList.tsx   # Session list
│       │   ├── MessageList.tsx   # Message display
│       │   ├── MessageInput.tsx  # Input area
│       │   ├── StepBar.tsx       # Workflow stage indicator
│       │   ├── StageResultsPanel.tsx  # Stage results (syndrome/formula/safety cards + P1 multi-alternative selection)
│       │   ├── ReviewActionsBar.tsx  # Doctor review controls
│       │   ├── FormulaEditModal.tsx   # Formula editing
│       │   ├── RecordPanel.tsx   # Medical record display
│       │   └── SafetyConfirmationPanel.tsx  # Safety confirmations
│       ├── hooks/                # React hooks
│       ├── api/                  # API client
│       ├── types/                # TypeScript type definitions
│       ├── utils/                # Utilities
│       └── styles/               # CSS & theme
├── data/                         # Knowledge base seed data
├── deploy/                       # Deployment configs
│   └── prometheus/               # Monitoring rules
├── docs/                         # Documentation, reorganized by topic (01–08/10)
├── scripts/                      # Utility scripts
│   ├── prewarm_embedding_cache.py # Embedding cache preheat CLI
│   ├── perf_benchmark.py         # Performance benchmark suite
│   ├── test_p2_rewrite_gateway.py    # Rewrite gateway E2E test
│   ├── test_reranker_conn.py     # Reranker connectivity test
│   └── seed_data.py              # Knowledge base seeding
├── tests/                        # Test suite (3200+ tests)
├── docker-compose.yml            # Middleware orchestration
├── pyproject.toml                # Python project config
└── .env.example                  # Environment template
```

### 📚 Documentation

The documentation set lives under `docs/`, reorganized by topic (each directory is self-contained with a numbered overview):

| Directory | Topic |
|---|---|
| [01_agent部分优化](docs/01_agent部分优化/) | Agent 架构演进、LangGraph 大修、收敛性重写 |
| [02_agent逻辑优化](docs/02_agent逻辑优化/) | 单一后端收敛、网关超时、柔性采集 |
| [03_agent性能优化](docs/03_agent性能优化/) | OP1–OP4 性能画像与优化、Embedding 缓存预热 |
| [04_生产环境加固](docs/04_生产环境加固/) | 认证授权、PHI 访问控制、网络边界、灰度上线 |
| [05_RAG效果评测](docs/05_RAG效果评测/) | RAG 评测实验设计、指标合同、消融实验 |
| [06_项目总结](docs/06_项目总结/) | 项目全貌：架构、Agent、RAG、性能、加固、测试 |
| [07_面试宝典](docs/07_面试宝典/) | 面试视角的系统设计、RAG、Agent、性能叙事 |
| [08_后续优化](docs/08_后续优化/) | R9 问题契约方案与实施记录 |
| [async-command.md](docs/async-command.md) | 持久化异步命令（R6/R7）设计 |

---

## ⚡ Performance

The agent runtime has been systematically profiled and optimized across four dimensions (OP1–OP4). All numbers below are measured on a local Docker (PG/Redis/Milvus) + cloud model gateway stack.

### Optimization Summary

| Category | Metric | Before | After | Improvement |
|---|---|---|---|---|
| **OP1 State Push-down** | Reasoning DB round-trips per claim | 10+ | 1–2 | ~60–70% reduction |
| **OP1 Intake** | `_compute_intake_from_claim` calls per finalize | 4× (once per route) | 1× (cached) | 4→1 |
| **OP1 Authority Cache** | Reasoning authority snapshot reads per turn | Cold DB query each time | Redis-cached, commit-invalidated | DB eliminated in hot path |
| **OP2 Gateway Pooling** | Health/LLM first vs reuse | ~5.0s vs ~2.0s | ~5.0s vs ~1.1s | Reuse ~4.7× faster |
| **OP2 Embedding Cache** | Cache hit rate (TCM consult scenario) | 0% (no cache) | **60.0%** | Gate: ≥40% ✅ |
| **OP2 Embedding Cache** | Miss (gateway RTT) vs hit (Redis) | ~570ms | ~4ms | **~89–209×** speedup |
| **OP2 Embedding Preheat** | Cold-start cache coverage | 0 entries | 3,979 entries (467 L1 + 3,512 L2) | ~350 MB Redis, ~539s preheat |
| **OP3 Milvus Async** | 8-way concurrent vector search wall-clock | Serial-blocked | 0.38–0.43s | **2.84–3.14×** speedup |
| **OP3 M1 Content** | PG backfill round-trip per chunk hit | 1 DB query | **~0ms** (Milvus direct) | Eliminated after v4 collection migration |
| **OP3 Reranker** | Evidence relevance ranking | MVP weighted sum only | Cross-Encoder / LLM reranker → top-8 | Deep semantic matching; graceful fallback |
| **OP3 Rewrite Gateway** | RAG query quality | Raw structured query | LLM-rewritten narrative query | Qwen3.5-2B-free @ dmxapi (304–845ms) |

### Observability

- `GET /api/v1/metrics` — 15 bounded `xuanhu_*` metric families (6 counters + 9 histograms), including R9 question-contract counters and the aspect-count histogram
- Histograms: `rag_vector_search`, `rag_fulltext_search`, `rag_backfill`, `rag_embed`, `gateway_chat`, `gateway_embed`, `graph_node`, `reasoning_get_state`, `question_contract_aspects`
- Prometheus alert rules: 15 total (outbox backlog 5, R5 safety 4, R9 question-contract 6), each with minimum-volume guards and `promtool` tested positive/negative scenarios

### Benchmarks

```bash
# Full performance benchmark suite (requires running API + infrastructure)
uv run python scripts/perf_benchmark.py

# Embedding cache preheat + hit-rate benchmark
uv run python scripts/prewarm_embedding_cache.py --all --benchmark

# Rewrite gateway E2E latency test
uv run python scripts/test_p2_rewrite_gateway.py
```

Results are written to `scripts/perf_results.json` and `scripts/prewarm_benchmark_result.json`.

### Regression Gates (CI)

- Full `pytest` suite (3200+ tests, incl. R9 question-contract unit tests) — no regressions
- `tests/golden/test_langgraph_performance_baseline.py` — P95 < 5000ms
- Embedding cache hit rate — ≥ 40%
- Structured parse success rate — no ≥1pp drop
- Milvus collection v4 with `content` field — backfill ~0ms verified
- Golden test assertions for real reasoning traffic — after-data validated
- `promtool check rules` / `test rules` — alert rules validated with firing & quiet scenarios
- Strict `mypy` on `app` + `scripts` — 0 issues across 229 source files

> 📊 Detailed methodology, before/after tables, and per-stage analysis: [Agent Performance Optimization](docs/03_agent性能优化/)

---

## 🧪 Development

### Running Tests

```bash
# Backend tests
cd xuanhu
uv run pytest

# Backend tests with integration markers
uv run pytest -m integration

# Frontend tests
cd frontend
npm test
npm run test:watch   # Watch mode
```

### Code Quality

```bash
# Backend
uv run ruff check .
uv run mypy app/

# Frontend
cd frontend
npm run lint
npm run typecheck
```

### Committing

This project uses conventional commit messages. Before committing, ensure:

- All linting checks pass (`ruff`, `mypy`, `oxlint`)
- Tests pass (`pytest`, `vitest`)
- No secrets are committed (`gitleaks`)

---

## 🔒 Safety & Compliance

### Clinical Safety Principles

1. **Doctor-in-the-loop** — Every prescription requires explicit practitioner confirmation before it becomes a formal medical record
2. **Deterministic safety first** — Drug interaction, dosage limit, and contraindication checks are pure rules, never delegated to an LLM
3. **Conservative pregnancy handling** — `pregnant` and `possible` statuses both trigger the full set of pregnancy contraindication rules
4. **No risk acceptance bypass** — `BLOCKER` and `HIGH` severity issues cannot be dismissed by the system; only a licensed practitioner can override after manual review
5. **Full audit trail** — Every safety check, agent action, and user interaction is persisted with trace IDs for complete accountability

### Data Privacy

- All model calls route through an internal model gateway — no patient data reaches external LLM providers
- **Identity masking before model input** — phone numbers, ID numbers and other identity sequences are replaced with equal-length `█` before any LLM sees them; evidence digests always cover the raw text and masked quotes still verify (R9)
- Session data is isolated per-practitioner
- MVP scope explicitly excludes HIS/EMR integration

---

## 🤝 Contributing

This project is in its early development phase. If you're interested in contributing:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'feat: add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

Please ensure your code passes all linting and tests before submitting.

---

## 📄 License

**UNLICENSED** — This project is currently not open for general use or distribution.

---

<p align="center">
  <sub>Built with ❤️ for the TCM community</sub>
</p>