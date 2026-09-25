"""Analytics vocabulary: the metric names, the bucket granularity and their units.

These are the *interface* of the analytics module - ``GET /admin/metrics/{metric}``
binds ``metric`` straight from the path and the agent/MCP tool schemas bind the same
literals - so they live together in the module's schema surface rather than being
declared inside the service that computes them. ``service`` re-exports them, so both
import paths stay valid.

``METRIC_UNITS`` exists because a metric without a unit is not usable by a dashboard:
``refund.rate`` is a ratio in ``[0, 1]`` while ``sales.gmv`` is minor currency units,
and a client that renders the second as the first is wrong by a factor of a hundred.
"""

from __future__ import annotations

from typing import Literal

#: The five projections the reporting surface publishes. Frozen by the metric
#: contract: adding one is a contract change, because the console's metric picker and
#: the agent's tool schema are both generated from this list.
Metric = Literal[
    "sales.gmv",
    "sales.order_count",
    "inventory.turnover",
    "product.performance",
    "refund.rate",
]

#: Reporting bucket width. Date-aligned rather than a rolling window, so two clients
#: asking for "week" cannot disagree about where a bucket starts.
Granularity = Literal["day", "week", "month"]

#: Unit per metric. ``minor_currency`` means integer minor units (fen/cents).
METRIC_UNITS: dict[str, str] = {
    "sales.gmv": "minor_currency",
    "sales.order_count": "count",
    "inventory.turnover": "ratio",
    "product.performance": "minor_currency",
    "refund.rate": "ratio",
}


__all__ = ["METRIC_UNITS", "Granularity", "Metric"]
