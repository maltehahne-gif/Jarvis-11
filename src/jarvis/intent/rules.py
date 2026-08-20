"""Deterministic local intent rules.

One table, two consumers: the Intent Router uses it to dispatch fast commands
without waking a model at all, and `RuleBasedProvider` uses it as its planning
rules when no cloud provider is reachable. Sharing the table keeps the offline
path and the fast path from drifting apart - a phrase that works on the couch
must keep working on a plane.

Matching is shallow keyword work over German and English phrasing. That is the
point: Principle 4 (fluid-first) says wake feedback and local actions must not
wait on cloud reasoning, and the only way to guarantee that is to not need
reasoning at all for the common cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Normalises German/English on-off vocabulary onto the capability schema.
STATE_ALIASES: dict[str, str] = {
    "an": "on",
    "ein": "on",
    "on": "on",
    "aus": "off",
    "off": "off",
}


@dataclass(frozen=True, slots=True)
class LocalRule:
    """A pattern that maps a phrase directly onto a capability call."""

    name: str
    pattern: re.Pattern[str]
    capability: str
    rationale: str
    confidence: float = 0.9

    def params(self, match: re.Match[str]) -> dict[str, Any]:
        raw = {k: v for k, v in match.groupdict().items() if v is not None}
        if "state" in raw:
            raw["state"] = STATE_ALIASES.get(raw["state"].lower(), raw["state"].lower())
        if "room" in raw:
            raw["room"] = raw["room"].lower()
        return raw


LOCAL_RULES: tuple[LocalRule, ...] = (
    LocalRule(
        name="light",
        pattern=re.compile(
            r"\b(?:licht|light|lampe|lamp)\b\s*(?:im|in|in der|in the)?\s*"
            r"(?P<room>[a-zA-ZäöüÄÖÜß]+)?\s*"
            r"\b(?P<state>an|aus|ein|on|off)\b",
            re.IGNORECASE,
        ),
        capability="home.set_light",
        rationale="phrase names a light and a target state",
    ),
    LocalRule(
        name="move",
        pattern=re.compile(
            r"\b(?:verschiebe|move)\b\s+(?P<source>\S+)\s+(?:nach|to)\s+(?P<target>\S+)",
            re.IGNORECASE,
        ),
        capability="files.move",
        rationale="phrase names a source and a destination path",
    ),
    LocalRule(
        name="message",
        pattern=re.compile(
            r"\b(?:nachricht|message)\b.*?\b(?:an|to)\s+(?P<to>\w+)\s*[:,]\s*(?P<body>.+)",
            re.IGNORECASE,
        ),
        capability="comms.send_message",
        rationale="phrase asks for a message to a named recipient",
    ),
    LocalRule(
        name="install",
        pattern=re.compile(r"\b(?:installiere|install)\b\s+(?P<package>\S+)", re.IGNORECASE),
        capability="system.install_software",
        rationale="phrase asks to install a package",
    ),
    LocalRule(
        name="secret",
        pattern=re.compile(
            r"\b(?:secret|geheimnis|passwort|password)\b\s+(?:für|for)?\s*(?P<name>\w+)",
            re.IGNORECASE,
        ),
        capability="security.read_secret",
        rationale="phrase asks for a credential",
    ),
    LocalRule(
        name="factory_reset",
        pattern=re.compile(
            r"\b(?:factory\s*reset|werkseinstellungen|wipe\s+everything)\b", re.IGNORECASE
        ),
        capability="system.factory_reset",
        rationale="phrase asks for a destructive reset",
    ),
    LocalRule(
        name="status",
        pattern=re.compile(
            r"\b(?:status|zustand|übersicht|overview|systemstatus)\b", re.IGNORECASE
        ),
        capability="system.status",
        rationale="phrase asks for current status",
        confidence=0.8,
    ),
)


def match_local(text: str) -> tuple[LocalRule, dict[str, Any]] | None:
    """First matching rule wins. Returns the rule and its extracted params."""
    for rule in LOCAL_RULES:
        match = rule.pattern.search(text)
        if match is not None:
            return rule, rule.params(match)
    return None
