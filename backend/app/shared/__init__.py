"""Infrastructure abstractions shared across bounded contexts.

Nothing in here knows about a specific business domain, and no domain module may
be imported from here (an architecture test enforces the direction).
"""
