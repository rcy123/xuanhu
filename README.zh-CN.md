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
  <a href="#-核心功能">功能</a> •
  <a href="#-系统架构">架构</a> •
  <a href="#-技术栈">技术栈</a> •
  <a href="#-快速开始">快速开始</a> •
  <a href="#-项目结构">项目结构</a> •
  <a href="#-开发指南">开发指南</a> •
  <a href="#-安全合规">安全合规</a>
</p>

<p align="center">
  <a href="README.md"><strong>English</strong></a> •
  <a href="#"><strong>中文</strong></a>
</p>

---

**悬壶（Xuanhu）** 是一款面向注册中医师的 AI 辅助诊疗工作台。系统覆盖从问诊采集、辨证开方、安全审核到病历生成的完整临床工作流，以清晰、可追溯的方式辅助医师决策。

> ⚕️ **辅助决策工具** — 所有结论仅供参考，需经执业中医师确认后使用。

---

## ✨ 核心功能

### 🔄 端到端临床工作流

一个工作台走完中医问诊全流程：

| 阶段 | 说明 |
|---|---|
| **问诊** | AI 辅助结构化问诊，自动追踪信息完备性 |
| **辨证** | 基于采集的四诊信息进行证候分析 |
| **开方** | 智能生成处方，含药味选择与剂量建议 |
| **安全审核** | 确定性规则引擎检查 10+ 个安全维度 |
| **医师确认** | 强制的"人机环"确认节点，未经确认不进病历 |
| **病历生成** | 结构化诊疗记录导出 |

### 🤖 多 Agent 运行时（LangGraph）

基于 **LangGraph** 构建的 Agent 编排层：

- **采集 Agent** — 结构化四诊信息收集与完备性评估
- **推理 Agent** — 证候辨证与处方生成
- **审核 Agent** — 处方安全审核门控
- **恢复 Agent** — 异常处理与会话恢复
- **分诊策略** — 自动判断问诊是否充分

### 🛡️ 确定性安全引擎

不依赖 LLM 的纯函数式安全审核系统，每条处方自动执行以下检查：

| 规则 | 严重度 |
|---|---|
| **十八反** | 🔴 阻断 |
| **十九畏** | 🔴 阻断 |
| **妊娠禁忌** | 🔴 阻断 / 🟠 高危 |
| **配伍禁忌** | 🔴 阻断 |
| **剂量上限**（依据《中国药典》） | 🟠 高危 / 🔴 阻断 |
| **过敏检查** | 🔴 阻断 |
| **剂量单位换算** | 🟡 警告 |
| **未知药名检测** | 🟠 高危 |

> ✅ 安全审核全程不涉及 LLM 调用——所有规则为纯确定性函数，基于结构化中药知识库执行。

### 🖥️ 现代化临床工作台

- **响应式侧栏** — 会话列表，可收起为姓名首字母缩略
- **实时流式传输** — 通过 SSE 实时展示智能体推理过程
- **阶段进度条** — 清晰展示工作流当前阶段
- **交互式处方编辑** — 支持安全验证的处方修改
- **医师审核面板** — 支持通过、修改或驳回处方
- **安全确认流程** — 对未解决的安全断言进行人工确认
- **病历预览** — 结构化中医诊疗记录导出

---

## 🏗 系统架构

```
┌─────────────────────────────────────────────────────┐
│                    前端 (React)                       │
│            xuanhu-ui · Vite · Ant Design             │
└──────────────────────────┬──────────────────────────┘
                           │ REST + SSE
┌──────────────────────────▼──────────────────────────┐
│                 后端 API (FastAPI)                    │
│              app/api · 10+ 路由模块                   │
└──────────────────────────┬──────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────┐
│            智能体运行时 (LangGraph)                    │
│  ┌────────┐ ┌───────────┐ ┌──────┐ ┌──────────┐    │
│  │ 采集    │ │   推理     │ │ 审核  │ │  恢复    │    │
│  │ 子图    │ │   子图     │ │/安全 │ │  子图    │    │
│  └────────┘ └───────────┘ └──────┘ └──────────┘    │
└──────────────────────────┬──────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────┐
│                     基础设施                          │
│  ┌──────────┐ ┌─────┐ ┌──────────┐ ┌──────────┐    │
│  │PostgreSQL│ │Redis│ │  Milvus  │ │   模型    │    │
│  │   16     │ │  7  │ │ (向量库)  │ │   网关    │    │
│  └──────────┘ └─────┘ └──────────┘ └──────────┘    │
└─────────────────────────────────────────────────────┘
```

### 关键设计决策

- **领域驱动设计（DDD）** — 采用 CQRS + Outbox 事件模式，确保事件可靠发布
- **幂等 HTTP 命令** — 所有状态变更操作使用幂等键，自带冲突检测
- **快照读模型** — 会话状态实时投影为只读物化视图，查询性能优化
- **共享 LangGraph 运行时** — 应用生命周期内维护一个编译后的图实例，跨请求复用
- **安全优先** — 确定性规则始终先于任何 LLM 输出执行

---

## 🛠 技术栈

### 后端

| 类别 | 技术选型 |
|---|---|
| Web 框架 | [FastAPI](https://fastapi.tiangolo.com/) |
| 运行环境 | Python 3.12 |
| Agent 编排 | [LangGraph](https://www.langchain.com/langgraph) |
| 数据库 ORM | SQLAlchemy 2.0 + Alembic |
| 业务数据库 | PostgreSQL 16 |
| 缓存/锁 | Redis 7 |
| 向量数据库 | Milvus 2.5（etcd + MinIO） |
| 数据校验 | Pydantic v2 |
| 代码质量 | Ruff + mypy（严格模式） |

### 前端

| 类别 | 技术选型 |
|---|---|
| 框架 | React 19 |
| 构建工具 | Vite 8 |
| UI 组件库 | Ant Design 6 |
| 语言 | TypeScript 6 |
| 路由 | React Router 7 |
| 测试 | Vitest + Testing Library + Playwright |
| 代码检查 | oxlint |

### DevOps

| 工具 | 用途 |
|---|---|
| Docker Compose | 本地中间件编排 |
| Prometheus | 监控（outbox 告警规则） |
| gitleaks | 密钥扫描 |

---

## 🚀 快速开始

### 前置依赖

- Python 3.12+
- Node.js 20+
- Docker & Docker Compose
- [uv](https://docs.astral.sh/uv/)（Python 包管理器）

### 1. 克隆与配置

```bash
git clone https://github.com/yourusername/xuanhu.git
cd xuanhu

# 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填入模型网关地址、数据库凭证等配置
```

### 2. 启动基础设施

```bash
docker compose up -d
```

将启动 PostgreSQL 16、Redis 7、Milvus（含 etcd + MinIO）。

### 3. 后端启动

```bash
# 创建虚拟环境并安装依赖
uv sync

# 执行数据库迁移
uv run alembic upgrade head

# 导入知识库数据（可选）
uv run python scripts/seed_data.py

# 启动 API 服务
uv run xuanhu-api
```

API 服务地址：`http://localhost:8000`，交互式文档：`/docs`。

### 4. 前端启动

```bash
cd frontend
npm install
npm run dev
```

前端地址：`http://localhost:5173`。

### 5. 验证

```bash
# 健康检查
curl http://localhost:8000/api/v1/health

# 就绪检查
curl http://localhost:8000/api/v1/health/ready
```

---

## 📁 项目结构

```
xuanhu/
├── app/                          # 后端应用
│   ├── agent_runtime/            # LangGraph 智能体编排
│   │   ├── graph.py              # 主状态图构建
│   │   ├── intake_subgraph.py    # 患者问诊采集子图
│   │   ├── reasoning_subgraph.py # 辨证开方子图
│   │   ├── review_node.py        # 审核门控子图
│   │   ├── recovery_node.py      # 异常恢复子图
│   │   ├── routing.py            # 命令路由逻辑
│   │   ├── runner.py             # 图执行器
│   │   ├── state.py             # 全局状态定义
│   │   ├── context_builder.py   # LLM 上下文组装
│   │   ├── repository.py        # 领域仓储（CQRS + outbox）
│   │   ├── checkpoint.py        # PostgreSQL 检查点持久化
│   │   └── sandbox_*.py         # 沙箱评估模块
│   ├── agents/                   # LLM Agent 提示词与逻辑
│   ├── api/                      # REST API 路由
│   │   ├── sessions.py           # 会话 CRUD
│   │   ├── messages.py           # 消息管理
│   │   ├── advance.py            # 阶段推进（幂等）
│   │   ├── review.py             # 医师审核
│   │   ├── stream.py             # SSE 流式传输
│   │   ├── record.py             # 病历操作
│   │   └── recovery.py           # 会话恢复
│   ├── core/                     # 核心配置与基础设施
│   │   ├── config.py             # 应用配置（pydantic-settings）
│   │   ├── gateway.py            # 模型网关客户端
│   │   └── redis.py              # Redis 客户端
│   ├── db/                       # 数据库会话管理
│   ├── models/                   # SQLAlchemy ORM 模型
│   ├── safety/                   # 确定性安全引擎
│   │   ├── engine.py             # 核心规则引擎（10+ 规则）
│   │   ├── datasets.py           # 中药配伍禁忌表
│   │   ├── normalizer.py         # 中药名标准化
│   │   └── rule_version.py       # 规则版本追踪
│   ├── schemas/                  # Pydantic 数据模型（API 层）
│   └── services/                 # 业务逻辑服务层
├── frontend/                     # React SPA 前端
│   └── src/
│       ├── components/           # UI 组件
│       │   ├── ChatPanel.tsx     # 问诊对话主面板
│       │   ├── SessionSider.tsx  # 会话导航侧栏
│       │   ├── SessionList.tsx   # 会话列表
│       │   ├── MessageList.tsx   # 消息列表
│       │   ├── MessageInput.tsx  # 输入栏
│       │   ├── StepBar.tsx       # 工作流阶段指示器
│       │   ├── ReviewActionsBar.tsx  # 医师审核操作栏
│       │   ├── FormulaEditModal.tsx   # 处方编辑弹窗
│       │   ├── RecordPanel.tsx   # 病历展示面板
│       │   └── SafetyConfirmationPanel.tsx  # 安全确认面板
│       ├── hooks/                # React Hooks
│       ├── api/                  # API 客户端
│       ├── utils/                # 工具函数
│       └── styles/               # 样式与主题
├── data/                         # 知识库初始数据
├── deploy/                       # 部署配置
│   └── prometheus/               # 监控告警规则
├── docs/                         # 完整技术文档
├── scripts/                      # 工具脚本
├── tests/                        # 测试套件
├── docker-compose.yml            # 中间件编排
├── pyproject.toml                # Python 项目配置
└── .env.example                  # 环境变量模板
```

### 📚 文档导航

| 文档 | 用途 |
|---|---|
| [产品设计文档](docs/产品设计文档.md) | 产品定位、MVP 范围、交付清单 |
| [PRD](docs/prds/xuanhu/PRD.md) | 阶段计划、用户故事、验收策略 |
| [系统概设](docs/系统概设.md) | 总体架构、模块边界、部署形态 |
| [多 Agent 架构设计](docs/多Agent架构设计.md) | Agent 职责、State、回退机制 |
| [接口设计文档](docs/接口设计文档.md) | REST / SSE / 错误码 / 内部接口 |
| [详细设计文档](docs/详细设计文档.md) | 代码结构、数据模型、核心流程 |
| [数据库设计文档](docs/数据库设计文档.md) | PostgreSQL / Milvus / Redis 设计 |
| [安全审核规则设计文档](docs/安全审核规则设计文档.md) | 禁忌、剂量、妊娠、阻断规则 |
| [UI 设计文档](docs/UI设计文档.md) | 工作台页面、阶段展示、确认区 |
| [部署指南](docs/部署指南.md) | 环境变量、Docker Compose、健康检查 |
| [使用指南](docs/使用指南.md) | 医师使用流程与安全提示 |
| [知识库数据说明](docs/知识库数据说明.md) | 数据文件、字段规范、导入校验 |

---

## 🧪 开发指南

### 运行测试

```bash
# 后端测试
uv run pytest

# 运行含集成标记的测试
uv run pytest -m integration

# 前端测试
cd frontend
npm test
npm run test:watch   # 监听模式
```

### 代码质量

```bash
# 后端
uv run ruff check .
uv run mypy app/

# 前端
cd frontend
npm run lint
npm run typecheck
```

### 提交规范

本项目使用约定式提交（Conventional Commits）。提交前请确保：

- 所有代码检查通过（`ruff`、`mypy`、`oxlint`）
- 测试全部通过（`pytest`、`vitest`）
- 无密钥泄露（`gitleaks`）

---

## 🔒 安全合规

### 临床安全原则

1. **医师在环** — 每条处方必须经执业医师确认才能生成正式病历
2. **确定性安全优先** — 配伍禁忌、剂量上限、妊娠禁忌等检查为纯规则引擎，绝不委托给 LLM
3. **保守妊娠处理** — `pregnant` 和 `possible` 状态均触发完整的妊娠禁忌规则
4. **无"接受风险继续"** — `BLOCKER` 和 `HIGH` 级别问题系统不予放行，仅可由执业医师审慎判断后手动处理
5. **完整审计追溯** — 每次安全检查、Agent 动作和用户操作均以 trace_id 记录，全链路可追溯

### 数据安全

- 所有模型调用均通过内网模型网关，患者数据不接触外部 LLM 供应商
- 会话数据按医师隔离
- MVP 范围明确不接入 HIS/EMR

---

## 🤝 参与贡献

本项目处于早期开发阶段。如果你想参与：

1. Fork 本仓库
2. 创建功能分支（`git checkout -b feature/amazing-feature`）
3. 提交变更（`git commit -m 'feat: add amazing feature'`）
4. 推送到分支（`git push origin feature/amazing-feature`）
5. 发起 Pull Request

请确保代码通过所有代码检查和测试后再提交。

---

## 📄 许可证

**UNLICENSED** — 本项目目前不开放公开使用或分发。

---

<p align="center">
  <sub>为中医社区而建 ❤️</sub>
</p>