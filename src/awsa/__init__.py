"""autonomous-web-scraper-agent (``awsa``).

A small, honest, evidence-grounded research agent. It plans search queries,
fetches pages politely, extracts structured claims with verbatim quotes, and
reconciles them across independent sources.

Runs with zero configuration and zero API keys: the default stack is DuckDuckGo
search, direct HTTP fetching and a deterministic extractive reader. Set
``AWSA_OPENAI_API_KEY`` to swap in any OpenAI-compatible model.
"""

from __future__ import annotations

from .config import Config, NetworkSettings, ProviderSettings
from .errors import (
    AwsaError,
    BudgetExhausted,
    ConfigError,
    FetchError,
    NetworkBlocked,
    ProviderError,
    RobotsDenied,
    UnsafeURLError,
)
from .models import (
    Budget,
    Claim,
    Evidence,
    FieldCandidate,
    ResearchReport,
    SearchHit,
    Source,
    Usage,
)

__version__ = "0.1.0"

__all__ = [
    "AwsaError",
    "Budget",
    "BudgetExhausted",
    "Claim",
    "Config",
    "ConfigError",
    "Evidence",
    "FetchError",
    "FieldCandidate",
    "NetworkBlocked",
    "NetworkSettings",
    "ProviderError",
    "ProviderSettings",
    "ResearchReport",
    "RobotsDenied",
    "SearchHit",
    "Source",
    "UnsafeURLError",
    "Usage",
    "__version__",
]
