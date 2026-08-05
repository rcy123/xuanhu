<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="">
    <img alt="Xuanhu" src="" width="128" height="128" style="border-radius: 24px;">
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
| **Inquiry** (问诊) | AI-assisted structured patient interview with completeness tracking |
| **Syndrome Differentiation** (辨证) | Automated syndrome pattern analysis based on collected symptoms |
| **Formula Drafting** (开方) | AI-generated prescription with herb selection and dosage |
| **Safety Review** (安全审核) | Deterministic rule engine checks 10+ safety dimensions |
| **Doctor Review** (医师确认) | Mandatory human-in-the-loop confirmation before finalization |
| **Medical Record** (病历生成) | Structured consultation documentation export |

### 🤖 Multi-Agent LangGraph Runtime

Built on **LangGraph**, the agent orchestration layer manages:

- **Intake Agent** — Structured symptom & sign collection, completeness evaluation
- **Reasoning Agent** — Syndrome differentiation and formula generation
- **Review Agent** — Prescription safety verification gate
- **Recovery Agent** — Graceful error handling and state recovery
- **Triage Policy** — Automated collection sufficiency determination

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
- **Interactive formula editing** with safety validation
- **Doctor review panel** — accept, modify, or reject prescriptions
- **Safety confirmation** workflow for unresolved assertions
- **Medical record preview** with structured TCM documentation

---

## 🏗 Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Frontend (React)                  │
│            xuanhu-ui · Vite · Ant Design             │
└──────────────────────────┬──────────────────────────┘
                           │ REST + SSE
┌──────────────────────────▼──────────────────────────┐
│                Backend API (FastAPI)                  │
│              app/api · 10+ route modules              │
└──────────────────────────┬──────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────┐
│            Agent Runtime (LangGraph)                  │
│  ┌────────┐ ┌───────────┐ ┌──────┐ ┌──────────┐    │
│  │ Intake │ │ Reasoning │ │Review│ │ Recovery │    │
│  │Subgraph│ │ Subgraph  │ │  /   │ │ Subgraph │    │
│  │        │ │           │ │Safety│ │          │    │
│  └────────┘ └───────────┘ └──────┘ └──────────┘    │
└──────────────────────────┬──────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────┐
│                    Infrastructure                      │
│  ┌──────────┐ ┌─────┐ ┌──────────┐ ┌──────────┐    │
│  │PostgreSQL│ │Redis│ │  Milvus  │ │  Model   │    │
│  │   16     │ │  7  │ │ (Vector) │ │ Gateway  │    │
│  └──────────┘ └─────┘ └──────────┘ └──────────┘    │
└─────────────────────────────────────────────────────┘
```

### Key Design Decisions

- **Domain-Driven Design** with Command/Query Responsibility Segregation (CQRS) and outbox event pattern for reliable event publishing
- **Idempotent HTTP Commands** — all state-mutating operations use idempotency keys with conflict detection
- **Snapshot-based Read Models** — session state is projected into read-optimized materialized views
- **Shared LangGraph Runtime** — a lifespan-managed compiled graph instance shared across requests
- **Safety-first** — deterministic rules are always enforced before any LLM output reaches the user

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
│   │   └── sandbox_*.py         # Sandboxed evaluation modules
│   ├── agents/                   # LLM agent prompts & logic
│   ├── api/                      # REST API routes
│   │   ├── sessions.py           # Session CRUD
│   │   ├── messages.py           # Message management
│   │   ├── advance.py            # Stage advancement (idiompotent)
│   │   ├── review.py             # Doctor review endpoints
│   │   ├── stream.py             # SSE streaming
│   │   ├── record.py             # Medical record operations
│   │   └── recovery.py           # Session recovery
│   ├── core/                     # Core configuration & infrastructure
│   │   ├── config.py             # Settings (pydantic-settings)
│   │   ├── gateway.py            # Model gateway client
│   │   └── redis.py              # Redis client
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
│       │   ├── ReviewActionsBar.tsx  # Doctor review controls
│       │   ├── FormulaEditModal.tsx   # Formula editing
│       │   ├── RecordPanel.tsx   # Medical record display
│       │   └── SafetyConfirmationPanel.tsx  # Safety confirmations
│       ├── hooks/                # React hooks
│       ├── api/                  # API client
│       ├── utils/                # Utilities
│       └── styles/               # CSS & theme
├── data/                         # Knowledge base seed data
├── deploy/                       # Deployment configs
│   └── prometheus/               # Monitoring rules
├── docs/                         # Comprehensive documentation
├── scripts/                      # Utility scripts
├── tests/                        # Test suite
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