"""Catalog context: categories, brands, products, SKUs, attributes.

Spec section 25. Owns the sellable definition and the price of a SKU - the only
place a price lives. Historical orders never read from here (INV-014); they carry
their own snapshot.
"""
