"""Schemas for the one genuinely untrusted input: what a model sends back.

The rest of the pipeline is typed by construction -- dataclasses built by our
own code, checked in ``__post_init__``. The exception is an LLM response, which
is text from a system we do not control, parsed at
:mod:`awsa.providers.llm_openai`.

Validating it explicitly is worth a dependency. The previous duck-typed parsing
called ``str(row.get("value", ""))``, so a model returning ``{"value": null}``
produced the string ``"None"`` -- which reads as a real extracted value, gets a
confidence score, and lands in a report as evidence for a claim. Models emit
nulls, nested objects and numbers-as-strings routinely; a schema that rejects
them is what turns a confident wrong answer into a dropped row.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "ExtractedFieldRow",
    "ExtractionEnvelope",
    "QueryEnvelope",
]


class _Strict(BaseModel):
    """Validate the fields we asked for, ignore anything else.

    Extra keys are ignored rather than rejected on purpose. A model that returns
    the requested fields plus ``char_start`` is answering correctly, and
    dropping the whole row for one unrecognised key would throw away a good
    extraction. The defence against a model asserting something unearned -- a
    ``verified: true`` flag, say -- is the grounding check that requires the
    quote to actually appear in the page text, not the shape of the response.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class QueryEnvelope(_Strict):
    """The planned search queries."""

    queries: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("queries")
    @classmethod
    def _non_empty_strings(cls, value: list[str]) -> list[str]:
        cleaned = [q for q in value if q.strip()]
        if not cleaned:
            msg = "no usable queries in the response"
            raise ValueError(msg)
        return cleaned


class ExtractedFieldRow(_Strict):
    """One field/value/quote/confidence row from an extraction response."""

    field: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1)
    quote: str = Field(default="", max_length=2000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    """0.0-1.0 after validation; a 0-10 score is rescaled, not clamped."""

    @field_validator("value")
    @classmethod
    def _not_a_stringified_none(cls, value: str) -> str:
        # The specific corruption the old ``str(...)`` coercion allowed.
        if value.strip().lower() in {"none", "null", "undefined", "n/a"}:
            msg = "value is a placeholder, not an extracted value"
            raise ValueError(msg)
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _numeric(cls, value: Any) -> Any:
        """Accept the shapes models actually emit, refuse the ones that lie.

        Handled here rather than by coercion at the call site: ``"0.9"``, ``90%``
        and ``"high"`` are all routine model output and all carry real
        information, so rejecting them would throw away good extractions.
        Anything else is refused rather than defaulted, because a silent 0.5
        reads as measured uncertainty when it is really a parse failure.
        """
        if isinstance(value, bool):
            # `True` is not a confidence. Returning it would let pydantic coerce
            # it to 1.0, which is the worst possible failure here: maximum
            # confidence fabricated from a field that carried none.
            msg = "confidence is a boolean, not a score"
            raise ValueError(msg)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in _QUALITATIVE:
                return _QUALITATIVE[text]
            try:
                if text.endswith("%"):
                    return float(text[:-1]) / 100.0
                return float(text)
            except ValueError:
                return value
        if isinstance(value, (int, float)) and value > 1.0:
            # A model returning 8 or 9 means 8/10. Rescaled rather than clamped
            # so a "9" does not silently become 1.0 -- overstating confidence is
            # the one error this field must not make.
            return value / _SCORE_SCALE_MAX if value <= _SCORE_SCALE_MAX else 1.0
        return value


#: Words models use for a confidence band. Preserved from the previous coercer
#: so schema validation does not reject common, well-formed output.
_QUALITATIVE: dict[str, float] = {
    "high": 0.85,
    "medium": 0.6,
    "moderate": 0.6,
    "low": 0.35,
}

#: A model returning 8 or 9 means 8/10, not 800%.
_SCORE_SCALE_MAX = 10.0


class ExtractionEnvelope(_Strict):
    """The envelope wrapping extraction rows, under any of the usual key names."""

    fields: list[ExtractedFieldRow] = Field(default_factory=list, max_length=200)
