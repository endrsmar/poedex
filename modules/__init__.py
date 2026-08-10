"""PoEDex modules.

One directory per module, each a vertical slice: ``backend/`` is Python, ``ui/`` is
TypeScript (later phases), ``tests/`` belongs to the module. The registry discovers
modules by looking for ``<id>/backend/module.py`` and reading its ``MODULE``.

A module may import only the ``api.py`` of a module it declares in ``requires``
(IMPLEMENTATION-PLAN §1.4, enforced by ``tests/test_boundaries.py``).
"""
