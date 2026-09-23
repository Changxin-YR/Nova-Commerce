# API Contract (frozen)

> **Status:** FROZEN. Owner: backend.
> **Why this document exists:** spec §96 and §99 freeze the *verb pattern* of the
> API and a handful of exact paths, and §98 fixes the *coverage* of the admin
> surface — but neither fixes the response **shapes**. The frontend was left
> guessing, and guessing at a wire format is how a UI silently rots.
>
> Per §150 (facts first, smallest fix, record it, carry on) the missing half is
> frozen here rather than left implicit. Where this document and a guess disagree,
> this document wins.
>
> **Rule for implementers:** if you need something this document does not define,
> ask — do not invent. An invented shape becomes a real defect the moment the two
> sides meet.

---

## 1. Envelope (already frozen by §95)

Every `/api/v1` response, success or failure:

```json
{ "code": 0, "message": "OK", "data": {}, "trace_id": "8f3a..." }
```

`code == 0` means success. Non-zero codes are the stable business codes in
`app/core/errors.py`; the frontend's `domain-mirror.spec.ts` fails the build if
they drift. `trace_id` is also returned as the `X-Trace-Id` header.

## 2. Scalar encodings (frozen)

These four rules remove most of the ambiguity that was blocking frontend work.

| Kind | Encoding | Note |
|---|---|---|
| **Money** | JSON **integer**, minor units (cents) | Never a string, never a float. `299900` means ¥2,999.00. |
| **Identifiers** | JSON **number** | BIGINT UNSIGNED. Exact in JS below 2^53; ids will not approach that. A string id would force `String(id)` at every call site for no benefit at this scale. |
| **Timestamps** | ISO-8601 **UTC with milliseconds** | `"2026-09-22T23:31:07.507Z"`. Never a local time, never epoch seconds. |
| **Enums** | SCREAMING_SNAKE **string** | Exactly the frozen vocabularies of §31–§34. Never an ordinal. |
| **null vs absent** | `null` is explicit; absent means "not requested" | An omitted field is never a silent `null`. |

## 3. List shape (frozen) — the highest-risk assumption

**Every list endpoint returns the same paged envelope. Never a bare array.**

```json
{
  "items": [ /* ... */ ],
  "meta": { "page": 1, "page_size": 20, "total": 137, "total_pages": 7 }
}
```

Rationale, because this one is worth defending: a bare array cannot carry a total,
so the moment a list grows past one page the client must either drop pagination or
the server must make a breaking change. Deciding now costs nothing; deciding later
costs a migration on both sides. `meta` is **always present**, even for a single
page, so the client never branches on its existence.

Query parameters, uniformly: `page` (1-based, default 1), `page_size`
(default 20, max 100), plus endpoint-specific filters. `total_pages` is
`ceil(total / page_size)` and is `0` when `total` is `0`.

Empty result is `{"items": [], "meta": {"page": 1, "page_size": 20, "total": 0, "total_pages": 0}}`
— **not** a 404, and not `null`. The §108 Empty state is driven by
`items.length === 0`, never by a missing field.

## 4. Task endpoints (frozen)

Spec §99: state changes are **task verbs**, never `PATCH {status}`.

| Action | Method + path | `data` on success |
|---|---|---|
| Publish product | `POST /api/v1/products/{id}/publish` | Product |
| Unpublish product | `POST /api/v1/products/{id}/unpublish` | Product |
| Cancel order | `POST /api/v1/orders/{order_no}/cancel` | Order |
| Confirm receipt | `POST /api/v1/orders/{order_no}/confirm-receipt` | Order |
| Ship fulfillment | `POST /api/v1/fulfillments/{id}/ship` | Fulfillment |
| Approve pending action | `POST /api/v1/pending-actions/{id}/approve` | PendingAction |
| Reject pending action | `POST /api/v1/pending-actions/{id}/reject` | PendingAction |
| Create inventory adjustment | `POST /api/v1/inventory/adjustments` | Inventory |
| Preview inventory adjustment | `POST /api/v1/inventory/adjustments/preview` | AdjustmentPreview |
| Publish promotion | `POST /api/v1/marketing/promotions/{id}/publish` | Promotion |
| Unpublish promotion | `POST /api/v1/marketing/promotions/{id}/unpublish` | Promotion |
| Reprocess document | `POST /api/v1/knowledge/documents/{id}/reprocess` | KnowledgeDocument |
| Archive document | `POST /api/v1/knowledge/documents/{id}/archive` | KnowledgeDocument |
| Cancel agent run | `POST /api/v1/agent/runs/{run_id}/cancel` | AgentRun |
| Preview a promotion | `POST /api/v1/marketing/promotions/preview` | PromotionPreview |
| Create a promotion | `POST /api/v1/marketing/promotions` | Promotion |
| Preview a coupon template | `POST /api/v1/marketing/coupons/preview` | CouponPreview |
| Create a coupon template | `POST /api/v1/marketing/coupons` | CouponTemplate |
| Update a role's permissions | `PUT /api/v1/system/roles/{id}/permissions` | Role |
| Assign a role to a user | `POST /api/v1/system/users/{user_id}/roles` | User |
| Log out everywhere | `POST /api/v1/auth/logout-all` | `{ "revoked_sessions": 3 }` |

**Task endpoints return the updated entity, not `204`.** A state transition that
returns nothing leaves the client unable to confirm *what changed*, so it either
refetches (an extra round trip on every click) or optimistically guesses (which
drifts from the server). Returning the entity removes both problems.

**Ship is keyed by fulfillment id, not order number** (§99). An order may ship in
several packages, so "ship this order" is not a well-formed instruction.

## 5. Fulfillment discovery — closing the gap the frontend correctly identified

`POST /fulfillments/{id}/ship` needs an id. Nothing in the frozen spec said how a
client learns one. Both of the following are therefore frozen:

**5.1 A fulfillment appears inside its order.** `GET /api/v1/orders/{order_no}`
returns, among the order fields:

```json
{
  "order_no": "NV20260922000001",
  "order_status": "PROCESSING",
  "payment_status": "PAID",
  "fulfillment_status": "PARTIAL_SHIPPED",
  "after_sale_status": "NONE",
  "shipments": [ /* Fulfillment objects, possibly empty */ ]
}
```

**5.2 The console can list fulfillments.**
`GET /api/v1/fulfillments/admin` — paged envelope, filters `order_no`,
`fulfillment_status`. This exists because an operator works from a fulfillment
queue, not by opening orders one at a time.

### Fulfillment object (frozen shape)

```json
{
  "id": 123,
  "order_id": 456,
  "order_no": "NV20260922000001",
  "fulfillment_no": "NVF20260922000001",
  "fulfillment_status": "UNFULFILLED",
  "carrier": "SF",
  "tracking_no": "SF1234567890",
  "shipped_at": null,
  "delivered_at": null,
  "created_at": "2026-09-22T23:31:07.507Z",
  "items": [
    {
      "id": 1,
      "order_item_id": 9,
      "sku_id": 3,
      "product_name": "Nova Phone 15 Pro",
      "sku_name": "原色钛金属 256GB",
      "quantity": 1
    }
  ]
}
```

`id` is the identifier that `POST /fulfillments/{id}/ship` takes. `carrier` and
`tracking_no` are `null` until shipped.

`POST /fulfillments/{id}/ship` request body — **exactly three fields**, nothing
else is accepted (§110 mass-assignment guard):

```json
{ "carrier": "SF", "tracking_no": "SF1234567890", "item_quantities": [ {"order_item_id": 9, "quantity": 1} ] }
```

## 6. Order shapes

`OrderSummary` (list rows) and `OrderDetail` (single order) differ only in that
`OrderDetail` adds `items[]`, `shipments[]` and the address snapshot.

`OrderSummary` deliberately carries **no line items** - a list endpoint that hydrated
every order's goods would fetch far more than it renders. But an order list that
cannot name a single product is useless to an operator, and leaving it out forced
either an N+1 detail fetch per row or a blank column. Two summary fields resolve
that without shipping the whole basket:

```json
{ "item_count": 3, "first_item_name": "Nova Phone 15 Pro 原色钛金属 256GB" }
```

`item_count` is the total number of units; `first_item_name` is the display name of
the first line (`product_name` + `sku_name`), truncated to 200 characters. Both are
backend-owned: computing them client-side is impossible, which is exactly why the
frontend correctly refused to guess. Both carry
all four status fields (§31–§34), because the UI must never infer one from
another — most importantly, **shipping never changes `order_status`**, so
"can this be shipped" reads `fulfillment_status`.

```json
{
  "id": 456,
  "order_no": "NV20260922000001",
  "order_status": "PROCESSING",
  "payment_status": "PAID",
  "fulfillment_status": "UNFULFILLED",
  "after_sale_status": "NONE",
  "original_amount": 299900,
  "promotion_discount_amount": 20000,
  "coupon_discount_amount": 0,
  "shipping_amount": 0,
  "payable_amount": 279900,
  "paid_amount": 279900,
  "refunded_amount": 0,
  "receiver_name": "张**",
  "receiver_phone": "138****5678",
  "created_at": "2026-09-22T23:31:07.507Z",
  "paid_at": "2026-09-22T23:32:10.000Z",
  "expires_at": "2026-09-22T23:46:07.507Z",
  "items": [
    {
      "id": 9,
      "product_id": 3,
      "sku_id": 3,
      "product_name": "Nova Phone 15 Pro",
      "sku_name": "原色钛金属 256GB",
      "image_url": "https://.../signed",
      "unit_price": 299900,
      "quantity": 1,
      "original_amount": 299900,
      "promotion_discount_amount": 20000,
      "coupon_discount_amount": 0,
      "allocated_discount_amount": 20000,
      "payable_amount": 279900,
      "refunded_amount": 0,
      "after_sale_status": "NONE"
    }
  ],
  "shipments": []
}
```

Two further fields on **`OrderDetail`**:

| Field | Type | Why it is server-owned |
|---|---|---|
| `cancel_reason` | `string \| null` | Null unless `order_status` is `CANCELLED`/`CLOSED`. The reason is recorded by the cancel workflow, so the client cannot infer it. |
| `refundable_amount` | integer minor units | `paid_amount - refunded_amount`, bounded below by 0. **The server owns this because INV-005 does.** |

`refundable_amount` is not decorative. The frontend discovered that deriving it as
`paid_amount - refunded_amount` client-side is a silent failure mode: on a payload
that lacks the field the expression yields `undefined`, and `undefined > 0` is
`false` - which would silently disable **every refund affordance in the UI** without
throwing anything. A money figure that decides whether a control is even rendered
belongs on the server, next to the rule it enforces.

The cancel endpoint accepts an optional reason, so the field has a writer:

```json
POST /api/v1/orders/{order_no}/cancel
{ "reason": "用户主动取消" }
```

`receiver_name` and `receiver_phone` arrive already masked (§94). The client must
not attempt to un-mask them. This is deliberate: an order list is a common
exfiltration target and the operator rarely needs the full value.

**Admin order reads:** `GET /api/v1/orders/admin` and
`GET /api/v1/orders/admin/{order_no}`. The consumer paths stay exactly as §96
froze them. The admin segment sits under the same `/orders` prefix so the two
surfaces cannot drift apart into unrelated namespaces.

## 7. Inventory

```json
{
  "id": 11,
  "warehouse_id": 1,
  "sku_id": 3,
  "sku_no": "NV-SKU-0003",
  "product_name": "Nova Phone 15 Pro",
  "sku_name": "原色钛金属 256GB",
  "available_qty": 42,
  "locked_qty": 3,
  "safety_stock": 0,
  "sellable_qty": 42,
  "on_hand_qty": 45,
  "version": 7,
  "updated_at": "2026-09-22T23:31:07.507Z"
}
```

`POST /api/v1/inventory/adjustments` request — **`version` is required** (§27
optimistic lock for low-contention background edits):

```json
{ "warehouse_id": 1, "sku_id": 3, "version": 7, "delta_available": -5, "reason": "盘点差异" }
```

A stale `version` returns **409** with code `INVENTORY_CONFLICT_STALE_VERSION
(40002)` and `data` containing the current Inventory, so the UI can show the
conflict and offer a refresh instead of silently overwriting. `MANUAL_ADJUST`
movements carry an operator id; the endpoint is never reachable from the agent
path without approval (§79).

## 8. Analytics (shapes now frozen; §98 required the surface, not the form)

Every analytics endpoint returns the same envelope, so one chart component and one
table component can render all of them:

```json
{
  "metric": "sales.gmv",
  "unit": "minor_currency",
  "period": { "from": "2026-08-24", "to": "2026-09-22", "granularity": "day" },
  "series": [ { "bucket": "2026-09-01", "value": 1289900 } ],
  "summary": { "total": 28410000, "average": 947000, "change_ratio": 0.12 },
  "dimensions": [ { "key": "sku_no", "label": "SKU", "value": "NV-SKU-0003" } ]
}
```

`unit` is one of `minor_currency`, `count`, `ratio`. The client must read `unit`
rather than assume — a ratio rendered as ¥ would be a silent, plausible-looking
lie. `bucket` is `YYYY-MM-DD` for day granularity, `YYYY-MM` for month. Empty
result is `series: []` with a zeroed `summary`, never `null`.

The five required metrics (§120, §124): `sales.gmv`, `sales.order_count`,
`inventory.turnover`, `product.performance`, `refund.rate`.

## 9. Error responses the UI must handle explicitly

| HTTP | code | Meaning | UI behaviour |
|---|---|---|---|
| 401 | `20002` `TOKEN_EXPIRED` | access token expired | refresh once, then retry; on failure sign out |
| 401 | `20004` `REFRESH_TOKEN_REUSED` | session family revoked | sign out and say so — **do not silently retry** |
| 403 | `20008`/`20009`/`20010` | forbidden / missing permission / data scope | `PermissionDenied` state, never a generic error |
| 409 | `40002` | stale inventory version | show the conflict, offer refresh |
| 409 | `90008` | coupon already locked | re-preview the cart |
| 202 | `110007` `AGENT_ACTION_REQUIRES_APPROVAL` | write needs human approval | show the approval card — **not** an error |
| 503 | `100009` | retrieval unavailable | "knowledge base unavailable"; commerce keeps working |

## 10. What is still NOT frozen

Named explicitly so nobody mistakes absence for permission:

- Pagination `sort`/`order` parameter names (planned: `sort=-created_at`).
- The `AgentRun` and `PendingAction` full shapes (frozen in Phase 10/13 with the
  rest of the agent contract).
- `item_count` / `first_item_name` ordering when an order has no items (there will
  always be at least one; stated so the edge case is not discovered at runtime).
- SSE payload internals — only the **12 event names** are frozen (§85).
- Report/export file formats.


---

## 11. Addenda — 2026-09-23

Three fields were added after the first freeze, each because the frontend hit a real
edge rather than a hypothetical one. They are listed together so a reader can see
what changed and why, instead of discovering the delta by diffing.

| Field | Home | Trigger |
|---|---|---|
| `item_count`, `first_item_name` | `OrderSummary` | Order lists could not name a product without an N+1 detail fetch per row. |
| `cancel_reason` | `OrderDetail` | The cancel affordance had a reason input the server never sends or accepts. |
| `refundable_amount` | `OrderDetail` | Client-derived `paid - refunded` yields `undefined` on a missing field, and `undefined > 0` is false - silently hiding every refund control. |

Two process notes worth keeping:

1. **The frontend reported these instead of inventing values.** It removed the cancel
   reason UI rather than leave decoration over a field the server does not send, and
   it routed order lists to the detail payload rather than fabricate a summary shape.
   Both were the correct call: a guessed shape becomes a real defect the moment the
   two sides meet.
2. **A money field that gates a UI control must be server-owned.** `refundable_amount`
   is not a display convenience; it is INV-005 exposed to the client. Deriving it
   locally means the client enforces an accounting rule, which is exactly the
   arrangement §15 forbids.

---

## 12. Addenda — promotion creation and system management (2026-09-23, second batch)

Two gaps the frontend reported rather than filled. Both were real omissions from the
first freeze, and the second one is the more interesting.

### 12.1 Promotion and coupon creation

§47 requires promotion creation to be **previewed first**, and
`PROMOTION_PREVIEW_REQUIRED` (90003) already exists as a business code — which means
the rule was frozen while the endpoints that implement it were not. The frontend
correctly shipped status transitions only and said so in the UI rather than build a
create flow against a guessed endpoint.

Frozen now: `POST /marketing/promotions/preview` then `POST /marketing/promotions`,
and the coupon equivalents.

**The preview response is the exact payload the create call will accept.** That is
deliberate: if preview returned a display-only rendering, the two would drift and an
operator would approve one thing and submit another. The frontend builds the payload
once and renders it, so preview and submit cannot diverge — which is also why
`PROMOTION_PREVIEW_REQUIRED` can be enforced server-side by requiring the create call
to carry the same `preview_token` the preview returned.

### 12.2 System management, and why the obvious endpoint is the wrong one

The frontend was asked for "user/role/permission management" and deliberately built
only the read half, stating the boundary in the UI instead of shipping a control it
could not honour. The reason it gave is the right one, and it deserves to be frozen
explicitly:

> §65 forbids a normal console user from downgrading a `CRITICAL` write tool to
> `READ`. A role editor is exactly that write. A generic
> `PUT /system/roles/{id}` that accepts a permission blob makes the forbidden
> operation one checkbox away.

So the endpoint is **not** a generic role update. It is:

- `PUT /api/v1/system/roles/{id}/permissions` — body carries the **complete, explicit**
  permission set, not a delta. A delta invites a caller to omit a permission it did
  not know about and silently revoke it.
- The server must refuse any change that lowers the `risk_level` of a tool marked
  `is_write` without a separate, audited approval. Encoding this as a blanket
  permission edit is precisely the mistake §65 anticipates.
- `POST /api/v1/system/users/{user_id}/roles` — role assignment, likewise explicit and
  audited, because a role change is an authorization change and every one of them
  should be reconstructable from the audit log (§133).

Two rules that follow from the above and apply to the whole system surface:

1. **A permission change is an audit event, not a settings save.** It must carry the
   actor, the before/after permission set, and a reason.
2. **Deny by default.** A role's permissions are what the row says, never
   "everything except". A wildcard grant cannot be audited, which is why §90's MCP
   scope intersection needs a concrete set to intersect against.

### 12.3 The pattern worth naming

Both gaps in this batch, and all three in §11, share a shape: **the rule was frozen and
the interface was not.** The frontend kept finding them because it tried to build the
UI and discovered there was nothing to call.

That is a useful signal in the other direction too — a page that cannot be built from
the frozen contract is evidence the contract is incomplete, not evidence the page is
unnecessary.

---

## 13. Response shapes for §12 — the gap §12.3 itself created (2026-09-23, third batch)

§12.3 named the pattern "the rule was frozen and the interface was not" — and then §12
repeated it one level down. It froze six **paths** (§4 names `PromotionPreview`,
`Promotion`, `CouponPreview`, `CouponTemplate`, `Role`, `User`) while defining **no
shape for any of them**. The frontend reported this rather than inventing the six, which
is why it is being fixed here instead of discovered later.

### 13.1 Promotion

```json
{
  "id": 12,
  "promotion_no": "NVP2026092300001",
  "merchant_id": 1,
  "name": "秋季数码焕新",
  "description": "手机品类满 3000 减 300",
  "promotion_type": "FULL_REDUCTION",
  "status": "DRAFT",
  "priority": 100,
  "stackable": false,
  "rule_config": {
    "threshold_amount": 300000,
    "reduction_amount": 30000,
    "max_discount_amount": null
  },
  "scope": {
    "all_products": false,
    "product_ids": [3, 7, 11],
    "category_ids": [],
    "brand_ids": [2]
  },
  "starts_at": "2026-09-25T00:00:00.000Z",
  "ends_at": "2026-10-08T23:59:59.000Z",
  "total_quota": 1000,
  "used_quota": 37,
  "created_at": "2026-09-23T04:00:00.000Z",
  "updated_at": "2026-09-23T04:00:00.000Z"
}
```

`promotion_type` is one of `DIRECT_DISCOUNT` | `PERCENT_DISCOUNT` | `FULL_REDUCTION`
(§39). **`rule_config` is discriminated by `promotion_type`**, and the three variants are:

| `promotion_type` | `rule_config` fields |
|---|---|
| `DIRECT_DISCOUNT` | `discount_amount` (minor units, per unit) |
| `PERCENT_DISCOUNT` | `discount_bps` (basis points — 1250 = 12.5%), `max_discount_amount` (nullable cap) |
| `FULL_REDUCTION` | `threshold_amount`, `reduction_amount`, `max_discount_amount` (nullable) |

Two deliberate choices, both about correctness rather than taste:

- **Rates are basis points, never floats.** 12.5% is exactly 1250 bps; `0.125` is not
  exactly representable, and a discount computed from a float is a ledger that does not
  add up. This is the same reason money is integer minor units.
- **`scope` is explicit rather than an implicit "everything".** A promotion with no
  scope is an all-products promotion, and that should be *stated* (`all_products: true`)
  rather than inferred from empty arrays. Inferring it is how a promotion accidentally
  covers the whole catalogue.

### 13.2 PromotionPreview

The preview response **is the exact body `POST /marketing/promotions` accepts**, plus:

```json
{
  "preview_token": "pv_9f2c...",
  "expires_at": "2026-09-23T04:15:00.000Z",
  "promotion": { "...": "the Promotion shape above, with id/promotion_no null" },
  "estimated_impact": {
    "affected_sku_count": 3,
    "affected_order_count_30d": 142,
    "estimated_discount_amount_30d": 4260000
  },
  "conflicts": [
    { "promotion_id": 9, "promotion_no": "NVP2026080100007", "reason": "OVERLAPPING_WINDOW_AND_SCOPE" }
  ],
  "warnings": ["该促销将与现有活动叠加，实际折扣可能高于预估"]
}
```

`preview_token` is what makes §47 enforceable rather than advisory: the create call must
carry it, and the server rejects a token whose underlying payload has changed. Without it
"preview first" is a convention the client can skip; with it, a single-submit create flow
**cannot be written** — which is exactly how the frontend implemented the coupon
equivalent, making it a compile-time property rather than a discipline.

`conflicts` is non-empty rather than a 409 because a conflict is information the
operator needs in order to decide, not an error that stops them looking.

### 13.3 CouponTemplate and CouponPreview

```json
{
  "id": 5,
  "template_no": "NVC2026092300001",
  "merchant_id": 1,
  "name": "新客立减 50",
  "coupon_type": "FIXED_AMOUNT",
  "status": "DRAFT",
  "face_value_amount": 5000,
  "discount_bps": null,
  "threshold_amount": 19900,
  "max_discount_amount": null,
  "total_quota": 10000,
  "issued_count": 0,
  "per_user_limit": 1,
  "validity_type": "RELATIVE",
  "valid_days": 30,
  "valid_from": null,
  "valid_to": null,
  "applicable_scope": { "all_products": true, "product_ids": [], "category_ids": [] },
  "created_at": "2026-09-23T04:00:00.000Z"
}
```

`coupon_type` is `FIXED_AMOUNT` | `PERCENT_DISCOUNT`. For `FIXED_AMOUNT`,
`face_value_amount` is set and `discount_bps` is null; for `PERCENT_DISCOUNT` the
reverse. A discriminator the client can switch on, rather than a nullable pair it has to
guess about.

`CouponPreview` mirrors `PromotionPreview`: `preview_token`, `expires_at`, `coupon`
(the shape above), `estimated_impact`, `warnings`.

### 13.4 Role and User, and the §12.2 constraint made concrete

```json
{
  "id": 4,
  "code": "OPERATOR",
  "name": "运营",
  "description": "商品、库存、促销与订单运营",
  "is_system": true,
  "data_scope": "MERCHANT",
  "permissions": [
    { "code": "product:write", "resource": "product", "action": "write", "is_grantable": true },
    { "code": "inventory:adjust", "resource": "inventory", "action": "adjust", "is_grantable": true }
  ],
  "user_count": 6,
  "created_at": "2026-09-23T04:00:00.000Z",
  "updated_at": "2026-09-23T04:00:00.000Z"
}
```

```json
{
  "id": 42,
  "username": "operator01",
  "email": "op01@example.com",
  "phone": "138****5678",
  "display_name": "运营一号",
  "user_type": "STAFF",
  "status": "ACTIVE",
  "merchant_id": 1,
  "data_scope": "MERCHANT",
  "data_scope_override": null,
  "roles": [ { "id": 4, "code": "OPERATOR", "name": "运营" } ],
  "last_login_at": "2026-09-23T03:12:00.000Z",
  "created_at": "2026-06-01T00:00:00.000Z"
}
```

`User.email` and `User.phone` arrive **masked** (§94) on list and detail alike. A staff
directory is an exfiltration target and an operator rarely needs the full value; the
client must not attempt to un-mask.

**The §12.2 rule now has a shape to attach to.** A permission entry carries
`is_grantable`, and the write is:

```json
PUT /api/v1/system/roles/{id}/permissions
{
  "permissions": ["product:read", "order:read", "analytics:read"],
  "reason": "调整运营职责范围"
}
```

Server-side obligations, stated here because a checkbox UI cannot express them:

1. Replace the set wholesale. A delta invites a caller to omit a permission it did not
   know about, silently revoking it.
2. **Refuse any change that lowers the `risk_level` of a tool marked `is_write`** unless a
   separate, audited approval accompanies it (§65). This is the rule the whole section
   exists for: with a generic blob update it is one unchecked box away.
3. Reject a permission whose `is_grantable` is false, regardless of what was sent.
4. Audit the before/after set with actor and reason — a permission change is an audit
   event, not a settings save.

### 13.5 What is still unfrozen, after this batch

Named so absence is not read as permission:

- Employee/warehouse/brand/category CRUD shapes (Phase 3 modules still lack routers).
- The full `AgentRun` / `PendingAction` shapes (Phase 10/13).
- Promotion lifecycle beyond `DRAFT` | `ACTIVE` | `ENDED`, and coupon issuance to users.
- Sort parameters and export formats.
