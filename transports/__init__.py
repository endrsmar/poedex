"""Transports — thin adapters over the method registry and the event bus.

A transport knows about :mod:`runtime.methods` and :mod:`runtime.events` and about
nothing else. It does not know what a verdict is, which is why the same registry
serves the HTTP surface here and the Decky RPC surface in Phase 7.
"""
