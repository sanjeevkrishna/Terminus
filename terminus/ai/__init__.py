"""Optional AI feature package.

Layout:
    providers.py  Model providers, normalised streaming events, redaction
    policy.py     Command risk classification — the hard safety boundary
    executor.py   Runs approved commands against live session channels
    agent.py      Tool-calling conversation loop with human approval

Attributes are resolved lazily so that importing this package does not pull in
a provider SDK until something actually needs one.

File path: terminus/ai/__init__.py
"""

import importlib

__all__ = [
    "DISCLAIMER_VERSION",
    "PROVIDER_SCHEMA",
    "AIError",
    "Capabilities",
    "Done",
    "TextChunk",
    "ToolCall",
    "active_provider",
    "build_provider",
    "get_ai_store",
    "provider_capabilities",
    "public_schema",
    "redact",
    "secret_fields",
    "test_provider",
]

_SOURCES = {
    "AIError": "providers",
    "Capabilities": "providers",
    "Done": "providers",
    "PROVIDER_SCHEMA": "providers",
    "TextChunk": "providers",
    "ToolCall": "providers",
    "active_provider": "providers",
    "build_provider": "providers",
    "provider_capabilities": "providers",
    "public_schema": "providers",
    "redact": "providers",
    "secret_fields": "providers",
    "test_provider": "providers",
    "DISCLAIMER_VERSION": "settings",
    "get_ai_store": "settings",
}


def __getattr__(name):
    module = _SOURCES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f".{module}", __name__), name)


def __dir__():
    return sorted(__all__)
