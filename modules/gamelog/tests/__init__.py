"""Tests for the `gamelog` module.

They live in the module because the module is the unit (IMPLEMENTATION-PLAN §1.1),
and they are collected by ``testpaths = ["tests", "modules"]``. Everything here is
offline and builds its own synthetic Steam library and log files under ``tmp_path``:
no test requires a Path of Exile install, a Steam install, or a Deck.
"""
