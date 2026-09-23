"""Inventory context: warehouses, stock positions, append-only movements.

Spec sections 26-28. Owns the one hot row in the system and the pessimistic
locking that keeps FG-09 honest.
"""
