# Phase 6 修复计划（冻结版）

本文件记录 2026-09-24 批准的 Phase 6 修复要求。实现、测试和 Final Gate
Evidence 必须以本文件为准；不得通过删除测试、降低规则或手工改写 Evidence
绕过失败。

## 执行顺序

1. 业务规则冻结
2. Phase 6 功能修复
3. Knowledge / Agent / Governance / Audit Router 与强制门禁
4. 真实环境 HTTP / Browser E2E
5. 完整回归与 `mypy app`
6. 根据真实执行结果重建 Evidence
7. 重新生成 `FINAL_GATE.md`

## 冻结要求

- 一个 SKU 的一次价格计算最多应用一个商品级 Promotion，之后最多应用一张符合条件的 Coupon。
- Promotion 候选必须逐个经过完整 `PricingService` 计算后再选择，不能只按 `priority` 选择。
- 订单创建后保存价格、优惠分配和规则标识快照，后续读取不得重新解析实时规则。
- 取消 `PENDING_PAYMENT` 订单必须幂等释放库存锁定、`LOCKED` Coupon，关闭未完成 Payment Intent，并写入 `InventoryMovement`、`CouponUsageRecord`、`OrderStatusLog` 和 Audit。
- 取消重复请求不得重复释放资源、写重复流水或产生第二次业务副作用。
- 已验签且真实成功的晚到支付必须记录 Payment 资金事实；Order 保持 `CLOSED`，不得重新锁定或扣减库存。
- 晚到支付进入幂等补偿退款；退款失败进入 `RECONCILIATION_REQUIRED`，重复 Callback 不得重复退款。
- PendingAction 的批准必须在数据库锁内原子验证 `status=PENDING AND expires_at>now`；过期后为 `EXPIRED`，旧 Checkpoint 永远不能恢复执行。
- `AwaitApproval` 在 `interrupt` 前不得产生不可重复副作用，Resume 后必须重新读取并验证业务事实。
- Analytics 默认比较周期为当前周期紧邻之前的等长周期；真实零返回 `0`，无数据或不适用返回 `null`。
- Knowledge / Agent / Governance / Audit 必须具备真实的 Router → Permission/DataScope → Service → Schema → Error Mapping → Test 链路。
- `mypy app` 目标为零错误；不得使用大面积 `# type: ignore`、扩大 `Any` 或关闭规则刷零。
- 真实 HTTP / Browser E2E 必须核对订单、Payment、库存流水、PaymentCallback、Fulfillment、Audit 和 Outbox 数据事实。
- Final Gate 只能由重新执行生成的真实 Evidence 更新；`STALE/MISSING` 不得直接改成 `PASS`。

## 完成条件

所有不可豁免 Gate 通过，真实支付浏览器流程完成结算，晚到支付和取消路径的数据库事实可重放，
`mypy app`、前端 typecheck/build、完整测试和 E2E 全部通过，工作区干净后才生成最终 Gate。
