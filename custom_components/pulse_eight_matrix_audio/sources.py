"""Source (input) modelling for the Pulse-Eight matrix.

Turns a model's per-type input counts into a flat, ordered list of selectable
sources, each carrying the Extended I/O source number to send to the switch and
a stable key used to store custom names.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import KIND_LABEL, KIND_MAX, KIND_ORDER, SOURCE_BASE


@dataclass(frozen=True)
class Source:
    """One selectable input."""

    kind: str  # analog | coax | optical
    index: int  # 1-based index within its kind
    number: int  # Extended I/O source number sent to the switch (SZ)

    @property
    def key(self) -> str:
        """Stable id for storing a custom name (e.g. ``analog_1``)."""
        return f"{self.kind}_{self.index}"

    @property
    def default_name(self) -> str:
        """Fallback label when the user hasn't renamed it (e.g. ``RCA 1``)."""
        return f"{KIND_LABEL[self.kind]} {self.index}"


def build_sources(counts: dict[str, int]) -> list[Source]:
    """Build the ordered source list for a model's input-type counts."""
    sources: list[Source] = []
    for kind in KIND_ORDER:
        count = min(counts.get(kind, 0), KIND_MAX[kind])
        for index in range(1, count + 1):
            sources.append(
                Source(kind=kind, index=index, number=SOURCE_BASE[kind] + index)
            )
    return sources
