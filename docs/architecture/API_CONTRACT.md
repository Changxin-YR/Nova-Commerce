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

`refundable_amount` sits on the **base** shape, so `OrderSummary` and `OrderDetail`
both carry it. It was added to `OrderDetail` alone in the Phase 4 batch and extended
to the list rows in Phase 5 (see section 15.8 for why that is a contract change and
not a tidy-up).

One further field on **`OrderDetail`**:

| Field | Type | Why it is server-owned |
|---|---|---|
| `cancel_reason` | `string \| null` | Null unless `order_status` is `CANCELLED`/`CLOSED`. The reason is recorded by the cancel workflow, so the client cannot infer it. |
| `refundable_amount` | integer minor units | `paid_amount - refunded_amount`, bounded below by 0. **The server owns this because INV-005 does.** Present on list rows as well as detail: the consumer order list renders its refund control from it, and `undefined > 0` is `false`, so a missing field hides the control instead of throwing. |

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

---

## 14. Response shapes for Phase 4 — order preview and creation (2026-09-23, fourth batch)

§96 froze the order **paths**, and §38 froze the **input rule** ("business inputs only,
never a client price"). Neither froze the wire shape of the two request bodies, and the
frontend had guessed one (`source: cart|direct`, nested `items_amount`, `coupon_code`).
Freezing it here before the backend implements it is the same move as §11–§13.

### 14.1 The cart is not a server resource in V1

§29 says the cart price is preview-only; §105 keeps `cart` in Pinia. There is therefore
no `/cart` resource, no cart table and no cart item id in V1. The client holds the
selection and sends it to the preview endpoint. A server-persisted cart would create a
second place where a price exists, which is exactly what §37 forbids.

### 14.2 `POST /api/v1/orders/preview` and `POST /api/v1/orders`

Both accept the same business inputs. `client_request_id` is required on create only.

```json
{
  "items": [ { "sku_id": 3, "quantity": 1 } ],
  "address_id": 9,
  "coupon_id": null,
  "remark": null,
  "client_request_id": "8f3a4c1e-..." 
}
```

* `items` carries **SKU and quantity only**. There is deliberately no `unit_price`,
  `discount_amount` or `payable_amount` field anywhere in the request: the schema is
  `extra="forbid"`, so a client that sends one is rejected with 422 rather than ignored
  (§38, §110).
* `coupon_id` is accepted because §38 freezes it as a business input. Coupon
  *resolution and locking* is marketing's job and lands in Phase 6; until then a
  non-null `coupon_id` is refused with `COUPON_NOT_FOUND (90004)` — it is never
  silently dropped, because a coupon the customer selected must not vanish without a
  message.
* `address_id` must belong to the caller. The address is snapshotted onto the order at
  creation; later edits to the address never change a historical order.
* Duplicate `sku_id` lines are merged (quantities summed) before pricing, so one order
  has at most one line per SKU.

**Preview response `data`** (200; one pricing authority, so the create path recomputes
the same numbers rather than trusting this response):

```json
{
  "items": [
    {
      "sku_id": 3,
      "product_id": 7,
      "product_name": "Nova Phone 15 Pro",
      "sku_name": "原色钛金属 256GB",
      "image_url": null,
      "unit_price": 299900,
      "quantity": 1,
      "original_amount": 299900,
      "promotion_discount_amount": 0,
      "coupon_discount_amount": 0,
      "allocated_discount_amount": 0,
      "payable_amount": 299900
    }
  ],
  "original_amount": 299900,
  "promotion_discount_amount": 0,
  "coupon_discount_amount": 0,
  "shipping_amount": 0,
  "payable_amount": 299900,
  "warnings": []
}
```

**Create response `data`** is the frozen `OrderDetail` of §6, returned with HTTP 200.
200 rather than 201 because an idempotent replay returns the *same* order: a client that
cannot tell "created" from "replayed" would have to guess whether its retry was safe.

### 14.3 Idempotency: header + body, two guards

`POST /api/v1/orders` requires **both** an `Idempotency-Key` header and a
`client_request_id` body field (§96).

| Situation | Result |
|---|---|
| Missing `Idempotency-Key` | `IDEMPOTENCY_KEY_REQUIRED (10010)` |
| Same key, same body | 200, the original order (no second order, no second stock movement) |
| Same key, different body | `IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD (10011)` |
| Different key, same `client_request_id`, same body | 200, the original order (the body field is the second guard for a client that lost its header) |
| Different key, same `client_request_id`, different body | `IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD (10011)` |

`client_request_id` is unique per customer in the database, so the second guard is a
constraint rather than a convention. The record in `idempotency_records` commits in the
same transaction as the order: a rolled-back create leaves no key behind, so a genuine
retry after a failure is allowed.

### 14.4 Money: one allocation algorithm, one exact sum

`payable_amount = original_amount − promotion_discount_amount − coupon_discount_amount + shipping_amount`.

Order-level discounts are allocated **pro-rata by each item's original amount, with the
remainder absorbed by the last eligible item** (§41) — the remainder rule is what makes
INV-006 exact rather than exact-to-the-cent. `allocated_discount_amount` is the sum of
the two allocations, and `item.payable_amount = item.original_amount − item.allocated_discount_amount`.

**`shipping_amount` is 0 in V1.** `PricingService.calculate_shipping` exists because §37
freezes the method, and the V1 policy is a free-shipping policy object. A non-zero
order-level shipping charge has no per-item field to allocate to, so including it in
`payable_amount` would break `SUM(item.payable_amount) == order.payable_amount` (INV-006)
by construction; the pricing service therefore refuses to build a snapshot in which that
equality does not hold. Enabling paid shipping requires a decision about allocation (or
about the invariant) and is **not frozen here** — do not switch it on silently.

### 14.5 Order status machine (Phase 4 subset)

* create → `PENDING_PAYMENT` (`payment_status=UNPAID`, `fulfillment_status=UNFULFILLED`,
  `after_sale_status=NONE`)
* `PENDING_PAYMENT` → `CANCELLED` via `POST /orders/{order_no}/cancel`; releases the
  reserved stock (coupon release is Phase 6)
* `PROCESSING` → `COMPLETED` via `POST /orders/{order_no}/confirm-receipt`
* `PENDING_PAYMENT` → `PROCESSING` is written by `PaymentSuccessWorkflow` (Phase 5)
* `PENDING_PAYMENT` → `CLOSED` is written by the expiry reconciliation (Phase 6)
* **Shipping never changes `order_status`** (§31); the four status fields are never
  inferred from one another.

`POST /orders/{order_no}/cancel` body (both fields optional):

```json
{ "reason": "用户主动取消" }
```

`cancel_reason` is null unless `order_status` is `CANCELLED`/`CLOSED`, and is
server-owned: the client cannot infer it. Cancelling anything other than a
`PENDING_PAYMENT` order is `ORDER_NOT_CANCELLABLE (50010)`.

### 14.6 Reads, filters and masking

* Consumer list `GET /api/v1/orders` — paged envelope (§3), own orders only. Filters:
  `order_status`. Default order: `created_at DESC`.
* Consumer detail `GET /api/v1/orders/{order_no}` — `OrderDetail` of §6, own order only.
* Console list `GET /api/v1/orders/admin` — filters `order_status`, `payment_status`,
  `fulfillment_status`, `order_no`.
* Console detail `GET /api/v1/orders/admin/{order_no}`.
* A consumer asking for an order that is not theirs gets `ORDER_NOT_FOUND (50003)`, never
  403: distinguishing the two would turn the endpoint into an existence oracle (IDOR).

`receiver_name` and `receiver_phone` are already masked (§94) on both surfaces. The
`full_address` on detail is the masked address snapshot; the client must not attempt to
un-mask it.

### 14.7 What is still unfrozen after this batch

* Sort/order parameter names (`sort=-created_at` is still only a plan).
* Promotion/coupon **application on the live order path** (Phase 6 resolves the rules;
  Phase 4 implements and unit-tests the arithmetic against the §13.1/§13.3 shapes).
* The paid-shipping rule and its interaction with INV-006 (see §14.4).
* Return/refund shapes (Phase 5).

---

## 16. Addenda — the two system list endpoints (from HANDOFF §15.1, 2026-09-23)

§13.4 froze `PUT /api/v1/system/roles/{id}/permissions` and
`POST /api/v1/system/users/{user_id}/roles` without freezing any way to
**discover** the ids those writes take. That is the same gap §5.2 closed for
fulfillments (`ship` needs an id, so `GET /fulfillments/admin` was frozen). The
ruling in HANDOFF §15.1 is transcribed here so the frontend role editor can be
built against a contract instead of a guess.

Both routes use the paged envelope of §3, always, never a bare array.

### 16.1 `GET /api/v1/system/roles`

Row shape = the Role shape of §13.4 (`id`, `code`, `name`, `description`,
`is_system`, `data_scope`, `permissions[]`, `user_count`, `created_at`,
`updated_at`). `permissions[]` is included in the list rows deliberately: the
role editor is a **review flow, not a toggle grid** (HANDOFF §15.1 condition 3),
so it must be able to render the current set without an N+1 fetch per row.

Server obligations:

1. **Merchant visibility filter:** `merchant_id = :current OR merchant_id IS NULL`
   — system roles are global. This is a server-side authorization decision, not a
   client convenience; a client that asks for another merchant's role gets it
   filtered out, not a 403 that confirms it exists.
2. Default order: `is_system DESC, id ASC` (system roles first, then
   stable by id).
3. Requires the caller to hold `user:read` **or** `role:assign`. No dedicated
   `role:read` code exists in the frozen vocabulary; if one is added later it
   supersedes this line, but until then "may read the user directory" and "may
   assign roles" are the two capabilities that imply reading role definitions.

### 16.2 `GET /api/v1/system/users`

Row shape = the User shape of §13.4, with `email` and `phone` already **masked**
(§94) on list rows exactly as on detail. A staff directory is an exfiltration
target and the operator rarely needs the full value.

Query parameters: `page`, `page_size`, plus optional `status`, `role` (role code)
and `search` (matches username / display name / email prefix). Those three are
frozen because a directory without them cannot be operated; they are filters, not
new fields.

Server obligations:

1. Merchant scope: only users whose `merchant_id = :current`. A platform-scoped
   administrator may pass an explicit merchant filter; there is no "all
   merchants" mode for a merchant-scoped caller.
2. Requires `user:read`.
3. Default order: `created_at DESC`.

### 16.3 What is still unfrozen here

`GET /api/v1/system/users/{id}` (a single-user read), role creation/deletion,
and any user create/deactivate route. The §13.2 rule stands: refuse to lower an
`is_write` tool's `risk_level` without a separate audited approval, and audit the
before/after set of every permission change.


---

## 15. Addenda - Phase 5: payment, fulfillment, after-sales (2026-09-23, fifth batch)

Phase 5 implements `REQ-PAY-*`, `REQ-FUL-*` and `REQ-AFS-*`. Two of its shapes were
already frozen before the code existed - the Fulfillment object (section 5), which
`POST /fulfillments/{id}/ship` takes, and `refundable_amount` (section 6), which the
frontend refuses to derive. This section freezes the rest, and it does so by
**transcribing what the frontend already calls**
(`frontend/src/api/endpoints.ts`, `frontend/src/types/domain.ts`) rather than by
inventing a parallel surface: the frontend built against the frozen contract and a
mock at the API boundary, so where its call shape is already written down, the
backend conforms to it. Where the two disagreed, the contract says which won and why.

### 15.1 `payment_status` vs the payment record's own status

Two different things live behind the word "status", and conflating them is how a UI
ends up showing "PAID" for an attempt that was never settled:

* `orders.payment_status` is the **order's** view (PAYMENT_STATUSES of section 6:
  `UNPAID`/`PAYING`/`PAID`/`PARTIAL_REFUNDED`/`REFUNDED`).
* a payment row has its own lifecycle, frozen here as
  `INITIATED -> PAYING -> SUCCESS | FAILED | CLOSED`, plus `PARTIAL_REFUNDED` /
  `REFUNDED` once money comes back.

Creating a payment attempt moves the order's axis `UNPAID -> PAYING`. It does **not**
move it to `PAID`, and no endpoint a JWT user can reach moves it to `PAID`: that is
`REQ-PAY-005`/`REQ-PAY-006` (INV-008), and the only writer is a verified provider
callback (15.3).

### 15.2 The Payment object

```json
{
  "id": 41,
  "payment_no": "NVPAY20260923000041",
  "order_id": 456,
  "order_no": "NV20260922000001",
  "channel": "MOCK",
  "status": "PAYING",
  "amount": 279900,
  "paid_amount": 0,
  "refunded_amount": 0,
  "external_transaction_no": null,
  "pay_url": "https://.../mock-pay/NVPAY20260923000041",
  "expires_at": "2026-09-22T23:46:07.507Z",
  "paid_at": null,
  "created_at": "2026-09-22T23:31:07.507Z"
}
```

`pay_url` is present only for `channel = "MOCK"` (section 100's MockPay) and is
`null` otherwise - a real provider's redirect URL is a client-side concern and is
never echoed by this API. `refunded_amount` is the payment record's own cumulative
figure; it exists so a payment detail page does not have to sum the refund history
to answer "how much of this can still be refunded".

Requests:

| Method + path | Body | Notes |
|---|---|---|
| `POST /api/v1/payments/customer/payments` | `{order_no, channel, client_request_id}` | `Idempotency-Key` header **required** |
| `GET /api/v1/payments/customer/payments/{payment_id}` | - | owner only |
| `GET /api/v1/payments/customer/payments/by-order/{order_no}` | - | owner only; one payment per order in V1 |
| `POST /api/v1/payments/customer/payments/{payment_id}/mock-pay` | `{client_request_id}` | DEV/DEMO only, see 15.3 |
| `GET /api/v1/payments/admin/payments` | - | console, paged; filters `status`, `order_no`, `channel` |

A payment belonging to somebody else is `PAYMENT_NOT_FOUND (60000)`, never `403` -
the section 109 rule already applied to orders.

### 15.3 The callback contract, and why it is not a JWT route

`POST /api/v1/payments/callbacks/{provider}` carries the provider's own
authentication model (`REQ-PAY-005`: *providers use a separate auth model*), because
a payment provider cannot hold a user's bearer token and must not be able to act as
one. Three headers are mandatory:

```
X-Provider-Event-Id: <provider's own event id>     # the idempotency key
X-Provider-Timestamp: <unix seconds>
X-Provider-Signature: <hex HMAC-SHA256>
```

The signature is `HMAC-SHA256(secret, f"{timestamp}.{raw_body}")`, hex-encoded, where
the secret comes from `PAYMENT_CALLBACK_SECRET` (per provider via
`PAYMENT_PROVIDER_SECRETS` when a real provider is configured). A body older than
`PAYMENT_CALLBACK_MAX_SKEW_SECONDS` is refused even when the signature verifies,
because a captured body replayed later would otherwise settle a payment twice.

Outcomes:

| Situation | Result |
|---|---|
| verified, first delivery | **200** with the updated Payment; one settlement |
| verified, duplicate `(provider, provider_event_id)` | **200** with `code = 60004 PAYMENT_CALLBACK_DUPLICATE`; no second effect |
| bad / missing / stale signature | **401** `60003`; no callback row claims success |
| amount mismatch | **409** `60002`; the payment is NOT marked successful |
| unknown `payment_no` | **404** `60000`; recorded as an IGNORED callback for triage |

The duplicate case answers **200, not 409**, deliberately: a provider retries until
it sees success, so answering an error to a delivery that was in fact already
applied would make it retry forever. This is the one honest use of "200 = your
request was already handled".

The mock surface (`/payments/callbacks/mock/{payment_id}`) exists so the demo proves
the invariants without a PSP. It is refused with `PAYMENT_MOCK_DISABLED (60006)`
unless `PAYMENT_MOCK_ENABLED` is true **and** `APP_ENV` is `dev`, `test` or `demo`.
The same check runs twice - once as a settings validator and once at the edge -
because a single guard that somebody bypasses is not a guard (`INV-008`).

### 15.4 The Fulfillment queue (closes section 5.2)

`GET /api/v1/fulfillments/admin` - paged envelope (section 3), filters `order_no`
and `fulfillment_status`, newest first. The row shape is the Fulfillment object of
section 5, `items[]` included: the console's ship dialog needs the lines and their
ids, and a queue that forces one detail fetch per row is why queues stop being used.

`GET /api/v1/fulfillments/customer/orders/{order_no}/shipments` returns the same
objects for the buyer - the same shape, because two shapes for one concept is how a
consumer page and a console page start disagreeing about whether a parcel shipped.

Shipping is the frozen task endpoint of section 4:

```
POST /api/v1/fulfillments/{id}/ship
{ "carrier": "SF", "tracking_no": "SF1234567890",
  "item_quantities": [ { "order_item_id": 9, "quantity": 1 } ] }
```

**Exactly three fields** (section 110 mass-assignment guard) and no
`idempotency_key`: the guard is the fulfillment's own state
(`FULFILLMENT_ALREADY_SHIPPED`, 70003), because "ship this package twice" is not a
retry - the second call is a mistake and must be named as one.

Consequences frozen with it:

1. **Shipping never changes `order_status`** (section 31). A shipped order is still
   `PROCESSING` until the customer confirms receipt. "Can this be shipped?" reads
   `fulfillment_status`, never `order_status`.
2. `fulfillment_status` is a **rollup over all packages**: some lines shipped ->
   `PARTIAL_SHIPPED`; every line's total shipped quantity >= its ordered quantity ->
   `SHIPPED`; nothing shipped -> `UNFULFILLED`.
3. Shipping **less** than the package's line quantity is legal and creates a
   residual package in the same transaction. The remainder is not silently dropped:
   a dropped remainder is stock the customer paid for and will never receive.
4. The cumulative shipped quantity per `order_item_id` across all packages can never
   exceed that line's ordered quantity (`FULFILLMENT_QUANTITY_EXCEEDS_ORDER`, 70001).
   A per-package check does not enforce this, and that gap is the defect.

### 15.5 After-sale (claim) and Refund (money fact) - two objects

`REQ-AFS-001` makes the separation explicit, and it is not a nomenclature preference:
the claim is what the **customer asks for**, the refund is what the **business pays
out**. They have different owners, different failure modes and different statuses. A
single table would have to say "the customer wants 200 back and we paid 150" in one
row, which is exactly the row two parties will read differently during a dispute.

```json
{
  "id": 12,
  "after_sale_no": "NVAS20260923000012",
  "order_no": "NV20260922000001",
  "type": "REFUND_ONLY",
  "status": "APPROVED",
  "requested_amount": 279900,
  "approved_amount": 200000,
  "refunded_amount": 0,
  "reason": "商品质量问题",
  "description": "...",
  "evidence_urls": [],
  "items": [ { "order_item_id": "9", "quantity": 1 } ],
  "refunds": [],
  "reject_reason": null,
  "created_at": "2026-09-23T10:00:00.000Z",
  "processed_at": "2026-09-23T11:00:00.000Z"
}
```

`items[].order_item_id` is a **string**, matching `frontend/src/types/domain.ts`
(AfterSale). It is deliberately left as the frontend typed it: the ids come back
from the order detail payload the UI already holds, so re-typing them to a number on
this one surface would introduce a coercion on a path that has never needed one.

Claim status vocabulary: `PENDING`, `APPROVED`, `REJECTED`, `CANCELLED`, `COMPLETED`.
It is **not** the order's `after_sale_status` axis. Only the refund path moves that
axis (`NONE -> PROCESSING -> PARTIAL_REFUNDED -> REFUNDED`), which is why an approved
claim sits next to an order whose `after_sale_status` is still `PROCESSING` - and
that is correct, because nothing has been paid back yet.

```json
{
  "id": 31,
  "refund_no": "NVR20260923000031",
  "after_sale_no": "NVAS20260923000012",
  "order_no": "NV20260922000001",
  "amount": 150000,
  "status": "SUCCEEDED",
  "reason": "部分退款",
  "operator": "staff:7",
  "created_at": "2026-09-23T11:05:00.000Z",
  "completed_at": "2026-09-23T11:05:00.000Z"
}
```

V1 refunds are **synchronous**: the row is written `SUCCEEDED` in the same
transaction that moves the money figures. A `PENDING` refund that no worker ever
completes would be a state that lies about money, and there is no worker until
Phase 6. The `PENDING`/`FAILED` members exist in the vocabulary because a real
provider's asynchronous refund will need them, and widening a `CHECK` later is a
migration.

Requests:

| Method + path | Body | Notes |
|---|---|---|
| `POST /api/v1/after-sales/customer/after-sales` | `{order_no, type, items[], requested_amount, reason, description?, evidence_urls?, client_request_id}` | `Idempotency-Key` required |
| `GET /api/v1/after-sales/customer/after-sales` | - | owner only, paged |
| `GET /api/v1/after-sales/customer/after-sales/{after_sale_no}` | - | owner only |
| `POST /api/v1/after-sales/customer/after-sales/{after_sale_no}/cancel` | `{}` | `PENDING -> CANCELLED` only |
| `GET /api/v1/after-sales/admin/after-sales` | - | console queue, paged; filters `status`, `order_no` |
| `GET /api/v1/after-sales/admin/after-sales/{after_sale_no}` | - | console detail |
| `POST /api/v1/after-sales/admin/after-sales/{after_sale_no}/approve` | `{approved_amount, remark?}` | `approved_amount <= requested_amount` in V1 |
| `POST /api/v1/after-sales/admin/after-sales/{after_sale_no}/reject` | `{reject_reason}` | `PENDING -> REJECTED` |
| `POST /api/v1/after-sales/admin/after-sales/{after_sale_no}/refund` | `{amount, reason?, idempotency_key}` | executes the refund; `Idempotency-Key` header required and must equal the body's `idempotency_key` |

`requested_amount` is validated against a **server-computed** cap (what is left of
`paid_amount - refunded_amount`, plus the per-line `payable_amount` when the claim
names items). The client's number is a request, never a basis. An ineligible claim
(unpaid order, cancelled/closed order, nothing left to claim, a line not on the
order) is `AFTER_SALE_NOT_ELIGIBLE (80001)`.

### 15.6 The refund caps (FG-12), and where they are enforced

Already stated in section 6 for `refundable_amount`; repeated here because Phase 5 is
where it becomes falsifiable:

1. cumulative refunds on a payment/order <= the amount actually paid
   (`REFUND_EXCEEDS_PAID_AMOUNT`, 80004);
2. cumulative refunds attributed to one order line <= that line's `payable_amount`
   (`REFUND_EXCEEDS_ITEM_AMOUNT`, 80005).

Both are re-validated **inside the row lock against freshly read values**, not
against the numbers the client sent or the ones read before the lock. The client-side
`max` on the refund form is UX only: a form limit is not a guard (section 104), and
the gate test proves the guard by driving the over-refund straight at the service.

`refundable_amount` on `OrderDetail` (section 6) is the same figure the cap uses, so
the number the UI renders and the number the server enforces cannot drift. The
frontend's temporary `paid - refunded` fallback exists only because a payload
predating the field reads `undefined`; with the field live on a real HTTP response
the fallback and its test branch are deleted in the same change (HANDOFF section 17.5
obligation 1).

### 15.7 What is still not frozen, after this batch

Named so that absence is not mistaken for permission (the section 10 rule):

* the provider-specific `pay_url` redirect flow and any provider SDK callback body
  beyond the four outcome rows of 15.3;
* refund reversal / cancellation (a refund that fails after being written
  `SUCCEEDED`), which needs the Phase 6 worker;
* partial-quantity claims across multiple claims of the same line - the V1 rule is
  "cumulative claimed quantity per line <= ordered quantity", enforced in the
  service, and the *shape* of a future per-line claim ledger is not frozen;
* the analytics view of refunds (`refund.rate`, section 8) and the console's refund
  dashboard, which are Phase 6;
* `GET /api/v1/fulfillments/{id}` (a single-package read) - the queue and the order
  detail are the frozen discovery paths and are sufficient;
* carrier codes beyond the V1 allowlist, and any integration with a carrier's own
  tracking API.

### 15.8 `refundable_amount` on the order list rows (a Phase 5 contract change)

Section 6 gained `refundable_amount` on the base order shape, which means the order
**list** rows carry it and not just the detail payload. Recorded as its own
subsection because it edits a shape frozen in the Phase 4 batch, and a frozen shape
must never be edited without the edit being visible.

Why it changed: the consumer order list renders its refund control from this number.
The frontend had a labelled `paid_amount - refunded_amount` fallback covering the
window before the field existed, and Phase 5 deletes that fallback - the deletion is
the standing proof the backend field is real (HANDOFF section 17.5, obligation 1).
Deleting it while the field was present only on the detail payload would have traded
one silent failure (a missing field reads as `undefined`, and `undefined > 0` is
`false`) for the same failure one screen over. So the order list gained the field in
the same change.

Why the list is the right place for it: it is the same server-owned figure, computed
by the same `Order.refundable_amount` property, and both surfaces already carried
`paid_amount` and `refunded_amount`. A list endpoint that carried the two operands
but not the consumed value was forcing the client to do arithmetic that section 15
forbids it from owning.

Server obligation (unchanged in substance): `refundable_amount == max(paid_amount -
refunded_amount, 0)` on **every** order payload, list rows included.

