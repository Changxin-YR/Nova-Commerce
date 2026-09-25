# Phase 6 DeepSeek Harness 执行交接文档

交接日期：2026-09-25  
仓库：C:\Users\27363\Desktop\store  
目标：完成剩余 Phase 6 修复、真实测试、Evidence 重建、Final Gate 和 Git 提交，严禁降低门禁。

## 一、冻结规则

以 docs/handoff/phase6-repair-plan.md 为最高执行依据。不得删除测试、跳过失败、修改测试期望、手工改 Evidence、把 SKIPPED 记为 PASS，或用大面积 # type: ignore、Any、关闭规则刷 mypy。不得执行 git reset --hard、git clean -fd、删除数据库 volume 或无范围清理业务表。所有会写 MySQL 的完整测试必须串行运行。

必须保持这些业务约束：

- 单 SKU 单次计算最多一个商品 Promotion，Promotion 候选必须逐个经过完整 Pricing Policy 计算后择优，之后最多一张符合条件 Coupon；订单创建后价格和优惠快照冻结。
- PENDING_PAYMENT CancelOrder 必须幂等释放库存锁、LOCKED Coupon、未完成 Payment Intent，并写 InventoryMovement、CouponUsageRecord、OrderStatusLog、Audit；重复取消不得产生第二次副作用。
- CLOSED/超时订单收到真实成功晚到支付时记录 Payment 资金事实，Order 保持 CLOSED，不重新锁/扣库存；进入幂等补偿退款，退款失败进入 RECONCILIATION_REQUIRED；重复 Callback 不得重复退款。
- PendingAction Approve 必须在数据库锁内原子验证 status=PENDING AND expires_at>now；过期后 EXPIRED，旧 checkpoint 不得执行。interrupt 前无不可重复副作用，Resume 后重新验证。
- Analytics 默认相邻等长比较周期；真实零返回 0，无数据或不适用返回 null，统计口径统一。
- Knowledge、Agent、Governance、Audit 必须是真实 Router → Permission/DataScope → Service → Schema → Error Mapping → Test 链路。
- mypy app 必须 0 errors；真实 HTTP/Browser E2E 必须核验 Order、Payment、InventoryMovement、PaymentCallback、Fulfillment、Audit、Outbox 数据事实。
- Final Gate 只能由真实重新执行生成，STALE/MISSING 不能手改为 PASS。

## 二、启动前核对

先确认状态，不要重置分支：

~~~powershell
Set-Location C:\Users\27363\Desktop\store
git status --short
git log -8 --oneline
Get-Content -Raw docs\handoff\phase6-repair-plan.md
Get-Content -Raw FINAL_GATE.md
Get-Content -Raw PROJECT_BASELINE.yaml
Get-Content -Raw scripts\final_gate.py
~~~

已知主要提交：

~~~text
25f371b fix: make safety gates and fixture cleanup reliable
59bc18d test: enable real phase6 safety gate execution
b16e0f9 test: refresh phase6 gate evidence
2b595a4 feat: complete phase6 commerce agent and governance flows
~~~

当前 Final Gate 中已记录过 PASS 的主要 Gate：FG-03、FG-07、FG-09、FG-12、FG-14、FG-15、FG-16、FG-17、FG-20、FG-21、FG-26；它们也必须在最终 revision 下重新核验。主要待完成项：FG-01、02、04、05、06、18、19、22、23、24、25，以及 STALE 的 FG-08、10、11、13。

## 三、恢复 Docker 和清理残留进程

先检查 Docker API、容器和端口：

~~~powershell
Set-Location C:\Users\27363\Desktop\store
docker version
docker compose --env-file .env -f ops/docker-compose.yml ps
docker compose --env-file .env -f ops/docker-compose.yml up -d
~~~

确认服务：MySQL 127.0.0.1:13306、Redis 127.0.0.1:16379、MinIO 127.0.0.1:19000、Qdrant 127.0.0.1:16333。

~~~powershell
Set-Location C:\Users\27363\Desktop\store\backend
..\.venv\Scripts\python.exe -c "from app.core.config import get_settings; from app.shared.db.session import configure_database,ping_database; from app.shared.redis_client import configure_redis,ping_redis; s=get_settings(); configure_database(s); configure_redis(s); print(ping_database()); print(ping_redis())"
~~~

必须得到两个成功结果，形式应类似 (True, 'ok')。若出现 WinError 10061、Docker API 500、容器反复重启或 MySQL 锁等待：停止残留 pytest/python 进程，查看容器日志，恢复并再次 ping；环境仍不稳定时必须报告“环境阻塞”，不得宣称完成。

## 四、静态检查和回归

~~~powershell
Set-Location C:\Users\27363\Desktop\store\backend
..\.venv\Scripts\python.exe -m ruff check app tests
..\.venv\Scripts\python.exe -m mypy app
..\.venv\Scripts\python.exe -m alembic check
..\.venv\Scripts\python.exe -m pytest -q
~~~

历史稳定后端完整结果为 1180 passed，Phase 6 目标回归为 50 passed。任何失败先定位根因再修复。前端使用 package.json 中的真实脚本，至少执行 test、typecheck、build、lint 和 e2e。

## 五、串行重建已有 Evidence

每条命令独立执行，检查退出码和文件后再执行下一条：

~~~powershell
Set-Location C:\Users\27363\Desktop\store
.\.venv\Scripts\python.exe scripts/emit_fg07_evidence.py
.\.venv\Scripts\python.exe scripts/emit_fg08_evidence.py
.\.venv\Scripts\python.exe scripts/emit_fg09_evidence.py
.\.venv\Scripts\python.exe scripts/emit_fg10_evidence.py
.\.venv\Scripts\python.exe scripts/emit_fg11_evidence.py
.\.venv\Scripts\python.exe scripts/emit_fg12_evidence.py
.\.venv\Scripts\python.exe scripts/emit_fg13_evidence.py
.\.venv\Scripts\python.exe scripts/emit_phase6_agent_gates.py
.\.venv\Scripts\python.exe scripts/emit_frontend_evidence.py FG-03
.\.venv\Scripts\python.exe scripts/emit_frontend_evidence.py FG-20
.\.venv\Scripts\python.exe scripts/emit_frontend_evidence.py FG-21
~~~

FG-08 曾发生单测通过但全量 Evidence 偶发失败、JUnit testcase 数与 assertions 不一致、残留进程造成 MySQL 锁等待。必须停止残留进程、恢复 Docker、单独复现失败测试，再在干净环境完整重跑 emit_fg08_evidence.py。检查 junit_report.testcase_count == len(assertions)。不能用单测通过替代完整 Gate。

## 六、补齐基础 Gate

读取 PROJECT_BASELINE.yaml 的 proof 和 watched paths，按仓库已有脚本/格式生成真实 Evidence：

- FG-01：branch、HEAD、工作区、未跟踪文件、关键配置和一致性。
- FG-02：后端 import/startup/static，记录真实输出和退出码。
- FG-04：项目 Docker build。
- FG-05：Alembic check/upgrade，确认无 drift。
- FG-06：连续两次 seed，比较结果和关键数据计数，证明幂等。
- FG-19：真实 MinIO 上传、读取、checksum 和清理。
- FG-23：Docker healthcheck 和服务可用性。
- FG-24：仓库已有 secret scan。
- FG-25：真实架构约束检查，确认 Router → Permission/DataScope → Service → Schema → Error Mapping。

若缺生成器，先参考 scripts/gate_evidence.py 和现有 emitter，再新增最小真实执行脚本。禁止写死 PASS。

## 七、完成 FG-18 MCP

先搜索：

~~~powershell
Set-Location C:\Users\27363\Desktop\store
rg -n "MCP|mcpserver|FastMCP|OAuth|resource server|streamable" backend tests scripts pyproject.toml
~~~

当前 mcp 依赖使用：

~~~python
from mcp.server.mcpserver import MCPServer
~~~

不要假设旧版 mcp.server.fastmcp 存在。必须实现并测试真实 server/request 链路：

1. 使用当前 MCPServer 支持的 HTTP 或 stdio 入口。
2. 校验 token 的 issuer、签名、expiry、audience、scope。
3. 防护 Origin/Host。
4. 工具 allowlist、用户权限、DataScope。
5. 高风险工具创建真实 PendingAction proposal。
6. 稳定 schema 和错误码。
7. 覆盖有效 token、错误 issuer/签名/expiry/audience、缺 scope、越权 DataScope、来源防护、allowlist、PendingAction。
8. 生成 artifacts/evidence/mcp/fg18_mcp.json，包含真实 assertions、exit_code、git.revision、git.relevant_paths 和 JUnit 信息。

不能只注册空工具、测函数存在或 mock authorization。

## 八、完成 FG-22 Flagship Agent E2E

必须真实 HTTP/Browser。覆盖 Agent 请求、工具调用、权限/DataScope、PendingAction、数据库锁审批、过期阻断、interrupt 前无副作用、Resume 重新验证、重复 Resume 幂等。流程结束后必须查询并核对 Order、Payment、InventoryMovement、PaymentCallback、Fulfillment、Audit、Outbox 七类事实，并将摘要写入 artifacts/evidence/agent/fg22_flagship_e2e.json。页面文字不能替代数据库事实。

## 九、最终回归、Evidence 和提交

完成修复后串行执行后端 ruff、mypy、alembic、pytest，前端 test/typecheck/build/lint/e2e。所有失败修复后重新运行受影响完整 Gate。

~~~powershell
Set-Location C:\Users\27363\Desktop\store
.\.venv\Scripts\python.exe scripts/final_gate.py
Get-Content -Raw FINAL_GATE.md
git status --short
git diff --check
git diff --stat
~~~

final_gate.py 会把缺失记为 MISSING、无 assertions/无 revision/JUnit 不一致记为 UNVERIFIED 或 FAIL、watched paths 改变记为 STALE；只有真实成功且 revision 匹配才是 PASS。不得手改状态。

仅当所有不可豁免 Gate PASS、FINAL_GATE.md 为 PROJECT STATUS: PASS、所有 Evidence 可解析、完整回归和 E2E 通过、工作区只含本次文件时提交：

~~~powershell
git add FINAL_GATE.md artifacts/evidence docs/handoff scripts backend frontend
git diff --cached --check
git commit -m "test: complete phase6 final gate evidence"
git status --short
git log -2 --oneline
~~~

若仍有失败或环境阻塞，不提交伪造 PASS；报告阻塞 Gate、根因、最后命令和退出码。

## 十、最终回报模板

~~~text
工作区：clean / 非 clean
最终 HEAD：<commit>
Final Gate：PASS / IN PROGRESS / FAIL
PASS Gate：<列表>
失败或阻塞 Gate：<列表>
每个失败项：<根因、最后一次真实命令、退出码>
后端完整测试：<结果>
mypy app：<结果>
前端 test/typecheck/build/lint：<结果>
真实 HTTP/Browser E2E：<结果>
Evidence revision 校验：<结果>
提交：<commit 或未提交及原因>
~~~

