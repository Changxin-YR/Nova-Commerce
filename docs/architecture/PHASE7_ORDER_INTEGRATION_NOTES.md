# Phase 7 — Order integration delta (checklist)

Phase 4 backend is complete; the frontend has not migrated. This is the delta only.
Every row: `现状 → 冻结值 → 要改的文件`. Unconfirmed items are marked **[UNVERIFIED]**.

Sources: `API_CONTRACT.md` §3/§6/§9/§14, `PROJECT_BASELINE.yaml`, live backend
(`create_app().openapi()`), live frontend source.

**[VERIFIED-LIVE]** = observed by running the backend or reading committed source.
**[CONTRACT]** = frozen in the contract, not yet exercised end-to-end over HTTP.

---

## 1. Consumer paths — `/orders/orders/*` → `/orders*`

**[VERIFIED-LIVE]** the live OpenAPI document contains exactly these, and nothing else,
under `/api/v1/orders`:

```
POST /api/v1/orders/preview
POST /api/v1/orders
GET  /api/v1/orders
GET  /api/v1/orders/{order_no}
POST /api/v1/orders/{order_no}/cancel
POST /api/v1/orders/{order_no}/confirm-receipt
```

Base URL is `/api/v1` (`src/config/constants.ts`, `API_BASE_URL`), so the paths in
`endpoints.ts` are relative to it.

| 现状 (`src/api/endpoints.ts`) | 冻结值 | 要改的文件 |
|---|---|---|
| `preview: '/orders/orders/preview'` | `/orders/preview` | `src/api/endpoints.ts` |
| `create: '/orders/orders'` | `/orders` | `src/api/endpoints.ts` |
| `list: '/orders/orders'` | `/orders` | `src/api/endpoints.ts` |
| `detail: '/orders/orders/{order_no}'` | `/orders/{order_no}` | `src/api/endpoints.ts` |

**Four calls 404 today.** The doubled `/orders/orders` segment is the whole bug; the
other four order paths in that file are already correct.

## 2. Task endpoints — already correct, do not change

**[VERIFIED-LIVE]** these already match §99 / `PROJECT_BASELINE.yaml` `task_endpoints`:

| 现状 | 冻结值 | 要改的文件 |
|---|---|---|
| `cancel: '/orders/{no}/cancel'` | `/orders/{order_no}/cancel` | none |
| `confirmReceipt: '/orders/{no}/confirm-receipt'` | `/orders/{order_no}/confirm-receipt` | none |

Both are task verbs (`POST`), never `PATCH {status}`. Nothing to migrate.

## 3. Admin endpoints — already correct, do not change

**[VERIFIED-LIVE]** present in the live OpenAPI at exactly these paths:

| 现状 | 冻结值 | 要改的文件 |
|---|---|---|
| `adminList: '/orders/admin'` | `GET /orders/admin` | none |
| `adminDetail: '/orders/admin/{no}'` | `GET /orders/admin/{order_no}` | none |

Route *ordering* is load-bearing and already handled: `backend/app/api/v1/router.py`
registers the order module as `("admin", "customer")` so `/orders/admin` is not captured
by `/orders/{order_no}`. **[VERIFIED-LIVE]** confirmed via OpenAPI. Do not reorder.

## 4. `api-contract.ts` shapes — OrderPreview / CreateOrderRequest / cart

**[CONTRACT]** §14.2. The request schema is `extra="forbid"`, so a leftover field is a
**422**, not an ignored field. No price field exists in any request body (§38).

### `OrderPreviewRequest` / `CreateOrderRequest`

| 现状 | 冻结值 | 要改的文件 |
|---|---|---|
| `source: 'cart' \| 'direct'` | *absent* | `src/types/api-contract.ts` |
| `item_ids?: string[]` | *absent* | same |
| `direct_items?: {sku_id: string, quantity}[]` | `items: {sku_id: number, quantity: number}[]` (**required**) | same |
| `address_id?: string` | `address_id: number` (required on create) | same |
| `coupon_code?: string` | `coupon_id: number \| null` | same |
| `remark` (create only) | `remark?: string \| null` | same |
| `client_request_id: string` | unchanged | none |
| — | `extra="forbid"` | any caller still sending a removed field |

Ids are **numbers**, not strings (§2). `sku_id: string` appears throughout the current
types and must become `number`.

### `OrderPreview` (response)

| 现状 | 冻结值 | 要改的文件 |
|---|---|---|
| `items[].product_title` | `items[].product_name` | `src/types/api-contract.ts` |
| `items[].sku_specs` | *absent* | same |
| `items[].cover_url?` | `items[].image_url` (`string \| null`) | same |
| `items[].unit_price_amount` | `items[].unit_price` | same |
| `items[].subtotal_amount` | `items[].original_amount` | same |
| `items[].sku_id: string` | `items[].sku_id: number` | same |
| `items_amount` | `original_amount` | same |
| `discount_amount` | `promotion_discount_amount` + `coupon_discount_amount` | same |
| `shipping_amount` | unchanged (`0` in V1) | none |
| `payable_amount` | unchanged | none |
| `coupon?`, `address?` | *absent* — resolve from your own `address_id`/`coupon_id` | same |
| `warnings?: string[]` | `warnings: string[]` (always present) | same |
| — | add `items[].product_id`, `items[].quantity`, `items[].allocated_discount_amount`, `items[].payable_amount` | same |

### Payloads that are already correct

**[VERIFIED-LIVE]** `src/types/frozen-contract.ts` mirrors §6 field-for-field
(`OrderBase`, `OrderSummary` with `item_count`/`first_item_name`, `OrderDetail` with
`items[]`/`shipments[]`/`cancel_reason`/`refundable_amount`, and `OrderItem`), and the
status vocabularies in `src/types/domain.ts` are identical to
`backend/app/modules/order/enums.py`. **The types are not the work — the request shapes
in `api-contract.ts` are.**

Also: `POST /orders` returns **HTTP 200**, not 201, because an idempotent replay returns
the same order and the client must not be able to tell "created" from "replayed".

## 5. `cartApi` is dead code — no `/cart` resource exists in V1

**[VERIFIED-LIVE]** `backend/app/api/v1/router.py` lists a `cart` module at `/cart`, but
`backend/app/modules/cart/` **does not exist** and the live OpenAPI contains **no**
`/cart` path. The router imports module routers defensively and skips absent modules, so
nothing crashes — the endpoints simply are not there, and every call 404s.

**[CONTRACT]** §14.1/§29/§105: the cart is a **client-side selection plus a server
preview**. No cart table, no cart item id, no `/cart` endpoints — deliberately, so that a
second place where a price exists cannot come into being.

| 现状 | 冻结值 | 要改的文件 |
|---|---|---|
| `cartApi` with 6 endpoints; docstring says "the cart is server-side state" | delete the module; selection lives in Pinia and posts to `/orders/preview` | `src/api/cart.ts` (delete), `src/api/index.ts` (drop the export), `src/stores/cart.ts` (rework off `cartApi`), `src/types/api-contract.ts` (`AddCartItemRequest` / `UpdateCartItemRequest` / `SelectCartItemsRequest` / `Cart` payloads), any consumer of the `Cart` type |

**[UNVERIFIED]** I confirmed the six `cartApi` calls and that `src/stores/cart.ts` is the
only non-API consumer, but did not enumerate every view/store reading the resulting
`Cart` type. Grep `cartApi` and `Cart` before deleting.

## 6. `refundable_amount` bridge — removal condition

**[VERIFIED-LIVE]** `src/domain/orders/availability.ts::refundableAmount()` reads
`order.refundable_amount` first and **falls back** to `paid_amount - refunded_amount`,
with a dev-only `warnBridgeHit()` and a `TODO(phase-5)` naming its own removal trigger:
*"a real `GET /orders/...` response carries the field"*.

**[CONTRACT]** §6 makes the field server-owned because
`undefined > 0 === false` would silently disable every refund affordance. The backend now
supplies it (`Order.refundable_amount`, bounded below by 0) and the consumer endpoints
are live.

| 现状 | 冻结值 | 要改的文件 |
|---|---|---|
| `refundableAmount()` falls back to client arithmetic; `refundable_amount?` optional on its picked type | server value only; field required | `src/domain/orders/availability.ts` (delete the fallback branch, `warnBridgeHit()`, `refundableBridgeState`), `src/domain/orders/__tests__/availability.spec.ts` (delete the fallback spec cases) |

**[UNVERIFIED]** the trigger condition is *met* — the field exists on the backend and the
endpoints are live (§6) — but I have not observed a real HTTP detail response carrying it,
and the frontend's `@deprecated` note says "Phase 5" while the backend shipped the field in
Phase 4. Confirm with one authenticated `GET /api/v1/orders/{order_no}`, then delete.

---

## Not in this cut (deliberately)

A first draft also covered idempotency semantics, error-branching, list-envelope and
money/status rules. Those were **not requested for this checklist** and are omitted; the
interfaces they rest on are frozen in `API_CONTRACT.md` §3/§9/§14 and
`docs/architecture/PHASE4_DESIGN.md`.
