"""Deterministic, zero-dependency LLM provider.

This is what makes the project runnable with no API key and what makes the test
suite hermetic. It is **not** a language model: it matches cue phrases and
labelled patterns against page text. Reports produced by it tag every claim with
``extractor="heuristic"`` so downstream consumers can tell inferred values from
model-generated ones, and its scores are capped low on purpose — a regex
should never be able to claim high confidence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ..observability import get_logger
from .base import ExtractedField, ExtractRequest, LLMUsage, PlanRequest

__all__ = ["ExtractiveLLM"]

log = get_logger("providers.extractive")

_HEURISTIC_CEILING = 0.55

_FIELD_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "and",
        "or",
        "to",
        "in",
        "at",
        "on",
        "for",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "by",
        "from",
        "as",
        "that",
        "this",
        "their",
        "his",
        "her",
        "its",
        "name",
        "value",
        "field",
        "about",
    }
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’&.\-]*")  # noqa: RUF001  (typographic apostrophe)

# Cue phrases keyed by a normalised field name. Ordered strongest-first, so the
# first match in a sentence wins and weaker cues only apply as fallbacks.
_CUES: dict[str, tuple[str, ...]] = {
    "name": ("full name", "goes by", "is named", "known as"),
    "city": ("is based in", "lives in", "based in", "resides in", "living in", "is from"),
    "location": ("is based in", "lives in", "located in", "headquartered in", "based in"),
    "country": ("is based in", "located in", "lives in"),
    "employer": ("works at", "works for", "joined", "employed by", "is employed at"),
    "role": ("works as", "is a", "works as a", "serves as", "role is"),
    "title": ("job title", "title is", "works as", "position is"),
    "bio": ("is a", "is an", "works as", "known for", "specialises in", "specializes in"),
    "email": ("email", "e-mail", "contact at", "reach him at", "reach her at"),
    "birth_date": ("was born on", "born on", "date of birth", "birthday is"),
    "founded": ("founded in", "was founded", "established in", "started in"),
    "price": ("costs", "priced at", "is priced", "starting at", "only"),
    "language": ("speaks", "fluent in", "programming languages"),
    "framework": ("uses", "built with", "built on", "stack includes", "tech stack"),
    "team_size": ("team of", "has a team", "employees", "headcount"),
}

_LABEL_RE = re.compile(
    r"(?P<label>[A-Za-z][A-Za-z ]{2,28}?)\s*[:：]\s*(?P<value>[^\n.;:]{2,80})"  # noqa: RUF001  (CJK colon)
)


def _cue_regex(cue: str) -> re.Pattern[str]:
    """Compile a cue into a word-boundary-aware matcher.

    Without boundaries the cue ``"is a"`` also matches inside ``"is an"``,
    which silently truncates the value to ``"n author"``.
    """
    return re.compile(rf"(?<!\w){re.escape(cue)}(?!\w)")


_GENERIC_QUERY_TEMPLATES = (
    "{subject} {fields}",
    "{subject} bio",
    '"{subject}" about',
    "{subject} interview profile",
)


@dataclass(frozen=True, slots=True)
class _Match:
    value: str
    quote: str
    start: int
    strength: float


class ExtractiveLLM:
    """Rule-based implementation of :class:`~awsa.providers.base.LLMProvider`."""

    name = "extractive"
    requires_key = False

    def __init__(self) -> None:
        self._usage = LLMUsage()

    def usage(self) -> LLMUsage:
        return self._usage

    async def plan_queries(self, request: PlanRequest) -> Sequence[str]:
        """Build diverse queries: templated, then field-targeted, de-duplicated."""
        self._usage.calls += 1
        subject = request.subject.strip()
        if not subject:
            return []

        uncovered = request.uncovered_fields or request.fields
        queries: list[str] = []
        seen: set[str] = set()

        def push(candidate: str) -> None:
            cleaned = _WS.sub(" ", candidate).strip()
            cleaned = re.sub(r'^"(?P<inner>.*)"$', r"\g<inner>", cleaned).strip()
            if not cleaned:
                return
            key = cleaned.lower()
            if key in seen:
                return
            seen.add(key)
            queries.append(cleaned)

        for template in _GENERIC_QUERY_TEMPLATES[: request.max_queries]:
            push(template.format(subject=subject, fields=" ".join(uncovered[:2])))

        for field_name in uncovered:
            hint = ""
            if field_name in request.fields:
                index = request.fields.index(field_name)
                if index < len(request.field_hints):
                    hint = request.field_hints[index]
            push(f"{subject} {hint}".strip())
            push(f"{subject} {field_name}")
            if len(queries) >= request.max_queries:
                break

        for existing in request.existing_queries:
            if len(queries) >= request.max_queries:
                break
            if existing.strip().lower() not in seen:
                seen.add(existing.strip().lower())
                queries.append(existing)

        log.debug("extractive planner produced %d queries", len(queries))
        return queries[: request.max_queries]

    async def extract_fields(self, request: ExtractRequest) -> Sequence[ExtractedField]:
        """Locate each field by cue phrase and labelled pattern."""
        self._usage.calls += 1
        text = request.page_text
        if not text.strip():
            return []

        out: list[ExtractedField] = []
        subject_tokens = {t.lower() for t in _TOKEN.findall(request.subject)}
        for field_name in request.fields:
            match = self._best_match(field_name, text, subject_tokens)
            key = _normalise_field(field_name)
            if match is None and key == "name":
                match = self._name_from_title(request, text)
            if match is None:
                continue
            value = match.value
            if not _is_plausible(value, request.subject, allow_subject=key == "name"):
                continue
            out.append(
                ExtractedField(
                    field=field_name,
                    value=value,
                    quote=_clip(match.quote, request.max_snippet_chars),
                    confidence=min(_HEURISTIC_CEILING, match.strength),
                    char_start=match.start,
                )
            )
        return out

    def _name_from_title(self, request: ExtractRequest, text: str) -> _Match | None:
        """A person's name is usually the page title or the first heading.

        Falls back to the subject itself when the title is the site name rather
        than the person, which is the common ``example.com | About`` shape.
        """
        subject = request.subject.strip()
        if not subject:
            return None
        candidates = [request.page_title]
        first_chunk = text[:400]
        head = first_chunk.split(". ", 1)[0].strip()
        if head and len(head) <= 60:
            candidates.append(head)

        for candidate in candidates:
            if not candidate:
                continue
            if subject.lower() in candidate.lower():
                return _Match(value=subject, quote=candidate, start=0, strength=0.5)
            if candidate.lower() in subject.lower() and len(candidate) >= 3:
                return _Match(value=candidate, quote=candidate, start=0, strength=0.45)
        return None

    def _best_match(self, field_name: str, text: str, subject_tokens: set[str]) -> _Match | None:
        key = _normalise_field(field_name)
        cues = _CUES.get(key, ())
        sentences = _split_sentences(text)
        best: _Match | None = None

        for sentence, offset in sentences:
            lowered = sentence.lower()
            for rank, cue in enumerate(cues):
                match = _cue_regex(cue).search(lowered)
                if match is None:
                    continue
                tail = sentence[match.end() :].strip()
                value = _clean_value(tail)
                if not value:
                    continue
                strength = 0.55 - (rank * 0.06) + _subject_bonus(value, subject_tokens)
                candidate = _Match(value=value, quote=sentence, start=offset, strength=strength)
                if best is None or candidate.strength > best.strength:
                    best = candidate

            labelled = _LABEL_RE.search(sentence)
            if labelled:
                label = labelled.group("label").strip().lower()
                if key and (key in label or label in key or any(c in label for c in cues)):
                    value = _clean_value(labelled.group("value"))
                    if value:
                        strength = 0.5 + _subject_bonus(value, subject_tokens)
                        candidate = _Match(
                            value=value, quote=sentence, start=offset, strength=strength
                        )
                        if best is None or candidate.strength > best.strength:
                            best = candidate
        return best


def _split_sentences(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    offset = 0
    for raw in _SENTENCE_SPLIT.split(text):
        chunk = raw.strip()
        if not chunk:
            offset += len(raw) + 1
            continue
        position = text.find(chunk, offset)
        if position < 0:
            position = offset
        out.append((chunk, position))
        offset = position + len(chunk)
    return out


_FIELD_ALIASES: dict[str, str] = {
    "cities": "city",
    "company": "employer",
    "companies": "employer",
    "job title": "title",
    "job": "role",
    "occupation": "role",
    "biography": "bio",
    "about": "bio",
    "summary": "bio",
    "twitter": "social",
    "github": "social",
    "founded": "founded",
    "year founded": "founded",
    "technologies": "framework",
    "tech stack": "framework",
    "languages": "language",
    "price": "price",
    "cost": "price",
    "born": "birth_date",
    "birth date": "birth_date",
    "date of birth": "birth_date",
    "birthday": "birth_date",
    "founding date": "founded",
    "location": "city",
    "where are they based": "city",
}


def _normalise_field(field_name: str) -> str:
    cleaned = _WS.sub(" ", re.sub(r"[^a-z ]+", " ", field_name.lower())).strip()
    if not cleaned:
        return ""
    if cleaned in _FIELD_ALIASES:
        return _FIELD_ALIASES[cleaned]
    if cleaned in _CUES:
        return cleaned
    singular = cleaned[:-1] if cleaned.endswith("s") and not cleaned.endswith("ss") else cleaned
    return singular if singular in _CUES else cleaned


def _clean_value(tail: str) -> str:
    """Trim a cue's tail down to a plausible short value.

    Cuts at the first clause boundary and caps the span at eight words, then
    strips leading copulas and determiners. Deliberately keeps whole phrases
    rather than collapsing to the first token, because fields like ``bio`` and
    ``role`` are phrase-valued.
    """
    value = tail.split("\n", 1)[0]
    value = re.split(r"[.;!?]", value, maxsplit=1)[0]
    words = value.split()
    if len(words) > 8:
        value = " ".join(words[:8])
    # Trailing dash punctuation includes em/en dashes, which real prose uses.
    value = value.strip().strip(",;:-—– \t")  # noqa: RUF001
    value = re.sub(r"^(?:is|was|are|were|be|been)\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(?:a|an|the)\s+", "", value, flags=re.IGNORECASE)
    value = value.strip().strip(",;:-—– \t")  # noqa: RUF001
    if not value or not _TOKEN.search(value):
        return ""
    return value


def _subject_bonus(value: str, subject_tokens: set[str]) -> float:
    tokens = {t.lower().strip(".,") for t in _TOKEN.findall(value)}
    if tokens & subject_tokens:
        return 0.08
    return 0.0


def _is_plausible(
    value: str,
    subject: str,
    *,
    allow_subject: bool = False,
) -> bool:
    if len(value) < 2 or len(value) > 80:
        return False
    lowered = value.lower()
    if lowered in _FIELD_STOPWORDS:
        return False
    tokens = _TOKEN.findall(value)
    if not tokens or len(tokens) > 8:
        return False
    if all(t.lower() in _FIELD_STOPWORDS for t in tokens):
        return False
    return allow_subject or lowered != subject.strip().lower()


def _clip(text: str, limit: int) -> str:
    flat = _WS.sub(" ", text).strip()
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0] + "…"


def iter_cue_phrases(field_name: str) -> Iterable[str]:
    """Expose the cue table so tests and docs can assert against it."""
    return _CUES.get(_normalise_field(field_name), ())
