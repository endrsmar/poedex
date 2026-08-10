"""`poeapi` — core module. Endpoints, normalization, cache.

Turns GGG's wire JSON into the normalized item model of SPEC §4.5, which is the
boundary every feature module consumes. Nothing downstream ever sees raw API JSON.
"""
