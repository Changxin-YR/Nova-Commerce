"""After-sales HTTP submodules: customer, admin, refunds (read-only).

Each submodule exposes a ``router``; ``app/api/v1/router.py`` composes them under the
``/after-sales`` prefix. The names are the frozen ones from PHASE5_DESIGN section 7, and
the aggregate router skips a missing submodule defensively - so this package can be
delivered piecewise without breaking application startup.
"""

