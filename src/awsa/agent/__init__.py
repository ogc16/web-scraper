"""The agent: spec, planner, extractors, reconciler and the autonomous loop."""

from __future__ import annotations

from .loop import AgentResult, AutonomousScraperAgent, FetchedPage, run_research
from .spec import FieldSpec, ResearchSpec
from .verifier import Reconciler, reconcile

__all__ = [
    "AgentResult",
    "AutonomousScraperAgent",
    "FetchedPage",
    "FieldSpec",
    "Reconciler",
    "ResearchSpec",
    "reconcile",
    "run_research",
]
