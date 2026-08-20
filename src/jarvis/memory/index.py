"""Knowledge graph and retrieval - Blueprint 8, figure 3.

Figure 3 puts "Knowledge Graph + Vector Search" between the stores and the
Context Builder. Both halves are real work, and only one of them can be built
honestly right now:

* **The graph is here.** Entries are subject-predicate-value triples, so the
  graph already exists in the data - `neighbours()` walks it. Asking "what do
  we know about Projekt Atlas" is a traversal, not a similarity search, and
  traversal gives exact answers rather than plausible ones.

* **Vector search is not.** It needs embeddings, which need either a local
  embedding model or a cloud one - and the cloud option would send memory
  off-device, which Principle 3 forbids without a deliberate decision. So
  `MemoryIndex` is a port, `LexicalIndex` implements it deterministically
  today, and a pgvector-backed sibling (Blueprint 4.3) drops in behind the
  same interface when that decision is made.

The lexical ranker is deliberately simple and explainable: term overlap,
weighted by how much JARVIS believes the entry and how recently it was
confirmed. Every ranked result can say *why* it ranked, which matters for a
control surface whose whole job is showing the owner what the system thinks it
knows.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from jarvis.events.envelope import utc_now
from jarvis.memory.models import MemoryEntry
from jarvis.persistence.ports import MemoryStore

_TOKEN = re.compile(r"[a-z0-9äöüß_./-]{2,}")

#: Common German and English function words. Small on purpose - an aggressive
#: stopword list would drop terms like "aus" or "an" that are meaningful in
#: this system's own command vocabulary.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "der",
        "die",
        "das",
        "den",
        "dem",
        "des",
        "ein",
        "eine",
        "einen",
        "einem",
        "und",
        "oder",
        "aber",
        "ist",
        "sind",
        "war",
        "wird",
        "werden",
        "hat",
        "haben",
        "ich",
        "du",
        "wir",
        "mir",
        "mich",
        "bitte",
        "mal",
        "nicht",
        "the",
        "and",
        "or",
        "but",
        "is",
        "are",
        "was",
        "were",
        "will",
        "has",
        "have",
        "for",
        "with",
        "that",
        "this",
        "please",
        "not",
        "you",
        "your",
    }
)

#: How fast an unconfirmed belief loses ranking weight. Chosen so a belief
#: confirmed a month ago still ranks at roughly half the weight of one
#: confirmed today - old knowledge fades, it does not vanish.
RECENCY_HALF_LIFE_DAYS = 30.0


#: Separators inside compound identifiers. This system's own vocabulary is
#: full of them - `preferred_editor`, `home.set_light`, `time_pattern:...` -
#: and a query says "editor" or "light", not the whole compound.
_COMPOUND = re.compile(r"[_./:-]")


def tokenize(text: str) -> set[str]:
    """Split into search terms, keeping compounds *and* their parts.

    Emitting both means `preferred_editor` matches a query for "editor"
    without losing the precision of the full identifier when that is what was
    asked for.
    """
    tokens: set[str] = set()
    for raw in _TOKEN.findall(text.lower()):
        if raw not in _STOPWORDS:
            tokens.add(raw)
        for part in _COMPOUND.split(raw):
            if len(part) >= 2 and part not in _STOPWORDS:
                tokens.add(part)
    return tokens


def entry_text(entry: MemoryEntry) -> str:
    return f"{entry.subject} {entry.predicate} {entry.value}"


def recency_weight(last_confirmed: datetime, *, now: datetime | None = None) -> float:
    age_days = ((now or utc_now()) - last_confirmed).total_seconds() / 86400.0
    return 1.0 / (1.0 + max(age_days, 0.0) / RECENCY_HALF_LIFE_DAYS)


@dataclass(frozen=True, slots=True)
class ScoredMemory:
    """One retrieval hit, with its reasoning attached."""

    entry: MemoryEntry
    score: float
    matched_terms: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.entry.to_dict(),
            "score": round(self.score, 4),
            "matched_terms": list(self.matched_terms),
        }


@runtime_checkable
class MemoryIndex(Protocol):
    """Relevance retrieval over stored memories."""

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        project_scope: str | None = None,
        min_confidence: float = 0.0,
    ) -> list[ScoredMemory]: ...


class LexicalIndex:
    """Deterministic term-overlap retrieval. Implements `MemoryIndex`.

    Candidates come from the store; ranking happens here. A vector-backed
    sibling would instead push both steps down into the database, which is why
    the port covers retrieval rather than only scoring.
    """

    def __init__(self, store: MemoryStore, *, candidate_limit: int = 500) -> None:
        self._store = store
        self._candidate_limit = candidate_limit

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        project_scope: str | None = None,
        min_confidence: float = 0.0,
    ) -> list[ScoredMemory]:
        terms = tokenize(query)
        if not terms:
            return []

        records = await self._store.list_memories(
            project_scope=project_scope, limit=self._candidate_limit
        )
        now = utc_now()
        scored: list[ScoredMemory] = []

        for record in records:
            entry = MemoryEntry.from_dict(record)
            if entry.confidence < min_confidence or entry.is_expired(now):
                continue

            tokens = tokenize(entry_text(entry))
            matched = terms & tokens
            if not matched:
                continue

            # Normalising by sqrt(len) keeps a long, vague entry from
            # outranking a short, precise one just by having more words.
            overlap = len(matched) / math.sqrt(len(tokens) or 1)
            score = overlap * entry.confidence * recency_weight(entry.last_confirmed_at, now=now)
            scored.append(
                ScoredMemory(entry=entry, score=score, matched_terms=tuple(sorted(matched)))
            )

        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[:limit]


class KnowledgeGraph:
    """Traversal over the subject-predicate-value triples.

    Exact structural questions belong here rather than in the ranker: "what do
    we know about X" has a right answer, and guessing at it with similarity
    scoring would be strictly worse.
    """

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def about(self, subject: str, *, limit: int = 100) -> list[MemoryEntry]:
        """Everything stored with `subject` as its subject."""
        records = await self._store.list_memories(subject=subject, limit=limit)
        return [MemoryEntry.from_dict(r) for r in records]

    async def neighbours(self, node: str, *, limit: int = 100) -> list[MemoryEntry]:
        """Edges touching `node` from either end.

        A triple is a directed edge, but relatedness is not: "Atlas uses
        PostgreSQL" is just as much a fact about PostgreSQL as about Atlas.
        """
        records = await self._store.list_memories(limit=self._scan_limit(limit))
        node_lower = node.lower()
        hits = [
            entry
            for entry in (MemoryEntry.from_dict(r) for r in records)
            if entry.subject.lower() == node_lower or str(entry.value).lower() == node_lower
        ]
        return hits[:limit]

    @staticmethod
    def _scan_limit(limit: int) -> int:
        return max(limit * 5, 200)
