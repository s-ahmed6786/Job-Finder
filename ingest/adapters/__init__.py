"""ATS adapter registry. Adapters register by their `ats` name (matches companies.ats)."""

from __future__ import annotations

from .base import (
    Adapter,
    AdapterError,
    BoardNotFound,
    Company,
    ConfigError,
    EmptyBoard,
    JobDetail,
    JobNotFound,
    JobSummary,
    RateLimited,
    SchemaError,
)

_REGISTRY: dict[str, type[Adapter]] = {}


def register(cls: type[Adapter]) -> type[Adapter]:
    if cls.ats in _REGISTRY:
        raise ValueError(f"adapter for {cls.ats!r} already registered")
    _REGISTRY[cls.ats] = cls
    return cls


def adapter_for(ats: str) -> type[Adapter]:
    try:
        return _REGISTRY[ats]
    except KeyError:
        raise ConfigError(f"no adapter registered for ats={ats!r}") from None


__all__ = [
    "Adapter", "AdapterError", "BoardNotFound", "Company", "ConfigError",
    "EmptyBoard", "JobDetail", "JobNotFound", "JobSummary", "RateLimited", "SchemaError",
    "adapter_for", "register",
]

# Import adapter modules so they register themselves.
from . import greenhouse  # noqa: E402,F401
