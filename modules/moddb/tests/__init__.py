"""Tests for the `moddb` module.

They live in the module because the module is the unit (IMPLEMENTATION-PLAN §1.1),
and they are collected by ``testpaths = ["tests", "modules"]``.

**Everything here runs against the committed artifact and recorded fixtures.**
Nothing opens a socket; the only thing in the project that fetches game data is
``scripts/build_moddb.py``, which is a build step and is never imported by a test.
The mod texts the bridge tests use come from ``tests/fixtures/poeapi/``, which was
read off a live account and scrubbed — see that directory's README for what "read
off" does and does not mean, because two of the numbers in it were invented during
scrubbing and this suite is honest about which.
"""
