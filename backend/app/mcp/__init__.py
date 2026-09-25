"""Nova MCP resource server (spec sections 86-93, ADR-013).

This package is the *external* tool surface of the platform: an MCP resource
server that exposes a frozen set of sixteen read-heavy commerce tools and one
approval-gated write proposal to an authorised MCP client.

Boundaries that shape every module in here:

* **The tool surface is not the internal tool surface.** ``app.modules.agent``
  owns the internal agent's three tools. The MCP server deliberately exposes a
  different, larger, externally-versioned set (``policy.TOOL_SPECS``) and shares
  no registry with it, because the two have different audiences and different
  change-control rules.
* **No SQL from a tool.** Every business read goes through the owning module's
  *service*, which is where permissions and data scope are enforced. The MCP
  layer adds the OAuth-scope and tool-policy dimensions on top; it never
  substitutes for them.
* **Nothing here mutates commerce state.** The single write tool
  (``nova.promotion.propose``) creates a ``pending_actions`` row and stops: the
  actual promotion is created later, by a human approval, through the existing
  governance path (REQ-MCP-008).
"""

from __future__ import annotations

__all__ = ["auth", "config", "policy", "server", "tools"]
