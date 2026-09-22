"""Bounded contexts of the modular monolith (spec section 13).

Each context owns its models, repository, service and router, and may only
reach another context through its *service* - never its repository or its
models. That boundary is enforced by tests/architecture, not by convention,
because a boundary that relies on discipline is a boundary that erodes.
"""
