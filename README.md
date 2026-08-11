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
| **Inquiry** (问诊) | AI-assisted structured patient interview with completeness tracking & clarification recovery |
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
| Main LLM Gateway | Mimo-v2.5 (internal) |
| Rewrite Gateway | Qwen3.5-2B-free @ dmxapi |
| Reranker | Jina Reranker M0 (Cross-Encoder) + LLM fallback |
| Embedding | BGE-M3 via dedicated embedding gateway |
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
| Prometheus | Monitoring (outbox alert rules) |
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
│   │   ├── repository.py        # Domain repository (CQRS + outbox)
│   │   ├── checkpoint.py        # PostgreSQL-backed checkpointer
│   │   ├── syndrome_verifier.py # Syndrome output validation chain
│   │   ├── formula_verifier.py  # Formula output validation chain
│   │   └── sandbox_*.py         # Sandboxed evaluation modules
│   ├── agents/                   # LLM agent prompts & logic
│   │   ├── syndrome_draft.py    # 辨证草稿 Agent (L4-1)
│   │   ├── formula_draft.py     # 开方+加减 Agent (L4-2, two-stage)
│   │   ├── prompts/             # Jinja2 prompt templates (30+)
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
├── docs/                         # Comprehensive documentation
├── scripts/                      # Utility scripts
│   ├── prewarm_embedding_cache.py # Embedding cache preheat CLI
│   ├── perf_benchmark.py         # Performance benchmark suite
│   ├── test_p2_rewrite_gateway.py    # Rewrite gateway E2E test
│   ├── test_reranker_conn.py     # Reranker connectivity test
│   └── seed_data.py              # Knowledge base seeding
├── tests/                        # Test suite (2460+ tests)
├── docker-compose.yml            # Middleware orchestration
├── pyproject.toml                # Python project config
└── .env.example                  # Environment template
```

### 📚 Documentation

| Document | Purpose |
|---|---|
| [Product Design](docs/产品设计文档.md) | Product positioning, MVP scope, delivery checklist |
| [PRD](docs/prds/xuanhu/PRD.md) | Phase plan, user stories, acceptance strategy |
| [System Architecture](docs/系统概设.md) | Overall architecture, module boundaries, deployment |
| [Multi-Agent Design](docs/多Agent架构设计.md) | Agent responsibilities, state management, fallback |
| [API Design](docs/接口设计文档.md) | REST/SSE endpoints, error codes, internal interfaces |
| [Detailed Design](docs/详细设计文档.md) | Code structure, data models, core flows |
| [Database Design](docs/数据库设计文档.md) | PostgreSQL/Milvus/Redis schemas |
| [Safety Rules](docs/安全审核规则设计文档.md) | Incompatibilities, dosage, pregnancy, blocking rules |
| [UI Design](docs/UI设计文档.md) | Workspace pages, stage display, review area |
| [Deployment Guide](docs/部署指南.md) | Environment variables, Docker Compose, health checks |
| [Perf: Diagnosis](docs/03_agent性能优化/01-性能诊断报告.md) | Initial profiling, bottleneck identification |
| [Perf: Gateway & Cache](docs/03_agent性能优化/阶段优化记录-OP2网关池化与embedding缓存-2026-08-06.md) | OP2 optimization: gateway pooling, embedding cache, preheat design |
| [Perf: Milvus & State Cache](docs/03_agent性能优化/阶段优化记录-OP3Milvus异步化与状态缓存-2026-08-06.md) | OP3 optimization: Milvus async, shared retriever, authority cache |
| [Perf: 三Agent Pipeline](docs/03_agent性能优化/06-辨证开方加减方Agent逻辑优化方案.md) | Three-agent reasoning pipeline design: Syndrome → Formula → Modification |
| [Perf: Implementation Report](docs/03_agent性能优化/06-实施评估报告-2026-08-06.md) | Post-optimization evaluation, before/after tables, acceptance report |
| [Async Commands (R6/R7)](docs/async-command.md) | Durable 202 async path: substrate, worker, status API, R7 default async admission & handlers |

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

- `GET /api/v1/metrics` — 84 custom `xuanhu_*` metric lines, 8 Prometheus histograms
- Histograms: `rag_vector_search`, `rag_fulltext_search`, `rag_backfill`, `rag_embed`, `gateway_chat`, `gateway_embed`, `graph_node`, `reasoning_get_state`

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

- Full `pytest` suite (2460+ tests) — no regressions
- `tests/golden/test_langgraph_performance_baseline.py` — P95 < 5000ms
- Embedding cache hit rate — ≥ 40%
- Structured parse success rate — no ≥1pp drop
- Milvus collection v4 with `content` field — backfill ~0ms verified
- Golden test assertions for real reasoning traffic — after-data validated

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