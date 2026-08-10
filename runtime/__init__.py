"""PoEDex runtime.

Hosts modules and knows nothing about Path of Exile. Everything PoE-specific lives
in ``modules/``; nothing here may import from there (enforced by
``tests/test_boundaries.py``).
"""

from runtime.context import ModuleContext
from runtime.errors import PoedexError
from runtime.events import Event, EventBus
from runtime.log import get_logger, install_redaction, silence_noisy_loggers
from runtime.methods import MethodRegistry
from runtime.module import Module, ModuleKind
from runtime.registry import ModuleState, Registry, build_registry, discover
from runtime.secrets import Secret, redact, register_secret
from runtime.settings import SettingsStore, SettingsView
from runtime.storage import Storage, StorageRoot, cache_dir, config_dir

__all__ = [
    "Event",
    "EventBus",
    "MethodRegistry",
    "Module",
    "ModuleContext",
    "ModuleKind",
    "ModuleState",
    "PoedexError",
    "Registry",
    "Secret",
    "SettingsStore",
    "SettingsView",
    "Storage",
    "StorageRoot",
    "build_registry",
    "cache_dir",
    "config_dir",
    "discover",
    "get_logger",
    "install_redaction",
    "redact",
    "register_secret",
    "silence_noisy_loggers",
]
