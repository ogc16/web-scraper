"""Pluggable providers for search, page retrieval and language modelling.

Import a concrete provider only when its credentials are present; the neutral
Protocols in :mod:`awsa.providers.base` are the contract the agent depends on.
"""

from __future__ import annotations

from .base import (
    ExtractedField,
    ExtractRequest,
    LLMProvider,
    LLMUsage,
    PageProvider,
    PlanRequest,
    SearchProvider,
)
from .llm_extractive import ExtractiveLLM
from .registry import LLMStack, Registry, SearchStack, merge_usage
from .search_ddg import DuckDuckGoSearch

__all__ = [
    "DuckDuckGoSearch",
    "ExtractRequest",
    "ExtractedField",
    "ExtractiveLLM",
    "LLMProvider",
    "LLMStack",
    "LLMUsage",
    "PageProvider",
    "PlanRequest",
    "Registry",
    "SearchProvider",
    "SearchStack",
    "merge_usage",
]
