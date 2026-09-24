# Analytics metric definitions

These are the V1 operational definitions behind the five metric names in
`API_CONTRACT.md` §8. All windows use UTC calendar dates. `from` and `to` are
inclusive; the API reads `[from 00:00, to + 1 day 00:00)` and accepts at most
366 days. The default is the latest 30 UTC dates. A week bucket starts Monday;
month buckets use `YYYY-MM`.

| Metric | Unit | Numerator / value | Boundary |
|---|---|---|---|
| `sales.gmv` | integer minor currency | Sum of `orders.paid_amount` | The order's `paid_at` is in the window; gross before later refunds. |
| `sales.order_count` | count | Number of orders with positive paid amount | Same `paid_at` window. |
| `product.performance` | integer minor currency | Sum of paid `order_items.payable_amount` | Uses the order snapshot and its parent's `paid_at`. The highest-revenue SKU is dimension metadata. Shipping is excluded. |
| `inventory.turnover` | ratio | Units on paid order lines ÷ average opening/closing on-hand units | On-hand is `available_qty + locked_qty`, reconstructed at each boundary from the append-only inventory movement ledger. This is unit turnover, not a cost-valued accounting ratio. |
| `refund.rate` | ratio | Amount of successful refunds completed in the window ÷ gross money collected in the same window | This is a period cash-flow ratio, not a same-order cohort rate. Refunds of older orders can make it exceed 1. The response also exposes numerator and denominator in `dimensions`. |

For each metric, `summary.total` applies the metric to the whole requested
window. `summary.average` is the arithmetic average of returned bucket values;
money averages are rounded to integer minor units. `summary.change_ratio`
compares the total to the immediately preceding window of equal length. When
there is no positive previous-window denominator, it is `0` by the frozen
numeric response shape. A ratio with no positive denominator also evaluates to
`0`; consumers should inspect the numerator and denominator dimensions before
interpreting a zero refund rate.

The endpoint requires a merchant staff principal with `analytics:read`. Every
query filters by the principal's merchant; no metric accepts a merchant ID from
the request.
