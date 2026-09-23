# Nova Commerce

**Nova AI 原生智能商业运营平台** · AI-Native Commerce Operations Platform

---

## 这个系统解决什么问题

大多数电商系统的现实是：**交易链路和 AI 能力是两套互不相干的东西**。

一边是一个能下单、能支付、能退款的传统商城；另一边挂着一个聊天窗口，它对真实业务一无所知，或者更糟——它能"看到"数据，却没人能保证它不会把库存改错、不会把不该退的钱退出去。

Nova Commerce V1 要解决的是这两者之间的**可信连接**问题：

> 让 AI 真正参与商业运营，同时让每一个由 AI 触发的业务动作，都受真实数据、真实权限、真实风控和真实审计的约束。

它由两个同等重要的部分组成：

### 1. 一个完整、真实、经得起追问的电商交易系统

不是教学用的 CRUD 示例。它包含完整的交易链路与业务事实：

- **商品域** —— 类目、品牌、SPU/SKU、属性、图片对象存储
- **库存域** —— 仓库、可用/锁定库存分离、只追加的库存流水、并发下的不超卖保证
- **交易域** —— 购物车、统一价格服务、订单、成交快照、订单状态机
- **资金域** —— 支付、回调幂等、履约多包裹、售后、退款额度约束
- **增长域** —— 促销、优惠券、优惠分摊、经营分析

其中最核心的几条承诺，是**用真实数据库上的并发测试证明**的，而不是写在文档里的：

| 承诺 | 如何被证明 |
|---|---|
| 库存永不为负、永不超卖 | 真实 MySQL 上 20 并发抢 1 件库存，只允许成功 1 单 |
| 同一个支付回调重复 10 次只产生一次业务效果 | 真实 MySQL 唯一约束 + 幂等记录 + 重复投递测试 |
| 退款永远不超过实付金额 | 订单行级 / 订单级双重额度校验 |
| 历史订单快照不受商品修改影响 | 成交快照字段 + 快照隔离测试 |
| 订单金额恒等于各订单行金额之和 | 分摊算法 + 尾差处理 + 数据库不变量校验 |

### 2. 一套受控的 AI 商业运营能力

AI 在这里不是一个聊天窗口，而是一个**有边界、有预算、需要人类批准的运营执行者**：

- **AI 不是业务事实的来源。** 订单状态、支付状态、退款事实、库存事实永远来自业务服务与数据库。AI 只能读取事实、做分析、生成 Proposal。
- **AI 不能直接碰数据库。** 它只能通过受控的 Tool 走完整的业务服务链路，且每个 Tool 都要经过权限、数据范围、风控、审批的完整管线。
- **任何写操作都需要人类批准。** 流程固定为：预览 → 提案 → 风控 → 生成待审批动作 → 中断等待 → 人类批准 → **重新校验真实业务事实** → 才执行。
- **AI 的权限永远不超过调用它的那个人。** 有效权限是「Agent 白名单 ∩ 当前用户权限 ∩ 运行时启用的 Tool」的交集。
- **分析型 Agent 在架构上就没有写能力。**
- **每一次成功声明都必须有执行回执。** 没有回执，AI 不允许说"已执行成功"。

### 它刻意不是这些东西

为了守住上面那些承诺，V1 明确**不做**：真实微信/支付宝生产支付、多商户平台结算、复杂 Saga、Kubernetes、秒杀拼团直播、推荐模型训练、任意 SQL 执行能力、以及大面积自治的多 Agent 协作。

> 设计原则的优先级是严格的：
> **业务正确性 > 安全 > 数据一致性 > 权限边界 > 可恢复性 > 可测试性 > 可观测性 > 用户体验 > 技术炫技。**

---

## 快速开始

> 环境要求：Docker、Python 3.11、Node.js 22.12+。
> 以下为本地开发启动方式；已实现范围及门禁状态见 [`FINAL_GATE.md`](FINAL_GATE.md)。

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }  # 按需填写密钥；.env 不提交
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e .\backend
docker compose --env-file .env -f ops/docker-compose.yml up -d
Set-Location backend
& ..\.venv\Scripts\python.exe -m alembic upgrade head
& ..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

在另一个终端启动前端：

```powershell
Set-Location frontend
npm ci
npm run dev
```

然后打开本地页面：

| 入口 | 地址 |
|---|---|
| 消费者商城 | http://127.0.0.1:5173 |
| 商家控制台 | http://127.0.0.1:5173/console |
| API 文档 | http://127.0.0.1:8000/docs |

异步 outbox 的 worker 与 beat 启动命令见 [`backend/README.md`](backend/README.md)。
| API 文档 | http://localhost:18000/api/docs |

---

## 文档地图

| 文档 | 内容 |
|---|---|
| [`docs/architecture/SYSTEM_ARCHITECTURE.md`](docs/architecture/SYSTEM_ARCHITECTURE.md) | 系统分层与模块边界 |
| [`docs/architecture/DOMAIN_MODEL.md`](docs/architecture/DOMAIN_MODEL.md) | 领域模型与限界上下文 |
| [`docs/architecture/DATABASE.md`](docs/architecture/DATABASE.md) | 表结构、约束与不变量 |
| [`docs/architecture/ORDER_WORKFLOW.md`](docs/architecture/ORDER_WORKFLOW.md) | 订单状态机与交易工作流 |
| [`docs/architecture/AGENT_RUNTIME.md`](docs/architecture/AGENT_RUNTIME.md) | Agent 运行时、Tool 管线与 HITL |
| [`docs/architecture/RAG_ARCHITECTURE.md`](docs/architecture/RAG_ARCHITECTURE.md) | 混合检索与证据链 |
| [`docs/architecture/MCP_SECURITY.md`](docs/architecture/MCP_SECURITY.md) | MCP 授权模型与协议边界 |
| [`docs/architecture/SECURITY_MODEL.md`](docs/architecture/SECURITY_MODEL.md) | 威胁模型与安全边界 |
| [`docs/architecture/STORAGE.md`](docs/architecture/STORAGE.md) | 对象存储策略 |
| [`docs/architecture/OBSERVABILITY.md`](docs/architecture/OBSERVABILITY.md) | 日志、追踪与审计 |
| [`docs/adr/`](docs/adr/) | 架构决策记录 |
| [`docs/demo/`](docs/demo/) | 演示脚本 |
| [`PROJECT_BASELINE.yaml`](PROJECT_BASELINE.yaml) | **冻结基线**：需求、不变量、Gate、依赖版本 |
| [`FINAL_GATE.md`](FINAL_GATE.md) | 验收证据索引 |

---

## 验收

本项目的完成状态**只由真实执行证据决定**，不由文档声明决定。

`FINAL_GATE.md` 只是证据索引，它本身不是证据。所有 Gate 的证据位于
`artifacts/evidence/`，并且必须可以由 `scripts/run_evidence.ps1` 重新生成。

```bash
make evidence           # 重新生成全部证据
make gate               # 汇总判定 PROJECT STATUS
```

---

## 目录结构

```
backend/          FastAPI 模块化单体（18 个限界上下文）
  app/core/       配置、安全、日志、数据库、对象存储、可观测性
  app/shared/     跨上下文的基础设施抽象（UnitOfWork、Repository 基类、金额类型）
  app/modules/    各限界上下文，每个内部再分层
frontend/         Vue 3 + TypeScript 应用（消费者商城 + 商家控制台 + AI 工作台）
ops/              Docker Compose、Nginx、MySQL 初始化、Keycloak Realm
evals/            RAG 与 Agent 评测集
artifacts/        执行证据（构建、迁移、测试、并发、安全、E2E）
docs/             架构文档、ADR、演示脚本
scripts/          证据生成与质量检查脚本
```
