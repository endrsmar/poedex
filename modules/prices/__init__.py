"""`prices` — the first **feature** module.

Pricing carries policy (IMPLEMENTATION-PLAN §1.3): which source, which league,
median-of-cheapest-N versus minimum, what counts as `unpriceable`, how stale a table
may be. Opinions in the core are how a contained core stops being contained, so this
lives outside it — and no core module may depend on it.
"""
