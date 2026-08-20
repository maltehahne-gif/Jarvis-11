"""Provider selection - Blueprint 6.1's "Empfohlenes Modell/Setting" table
starts here, at the one place that decides which `IntelligenceProvider`
implementation the Core actually runs.

Kept separate from `core.py` so choosing a provider is a one-line, testable
decision rather than something buried in `JarvisCore.__init__`.
"""

from __future__ import annotations

from jarvis.agents.provider import IntelligenceProvider
from jarvis.agents.rule_provider import RuleBasedProvider

KNOWN_PROVIDERS = frozenset({"rules", "claude-agent-sdk"})


class UnknownProvider(ValueError):
    pass


def build_provider(name: str) -> IntelligenceProvider:
    if name == "rules":
        return RuleBasedProvider()
    if name == "claude-agent-sdk":
        # Imported lazily: pulls in the claude_agent_sdk package (and its CLI
        # subprocess transport) only for callers that actually asked for it.
        from jarvis.agents.claude_sdk_provider import ClaudeAgentSdkProvider

        return ClaudeAgentSdkProvider()
    raise UnknownProvider(f"unknown provider {name!r}; known: {sorted(KNOWN_PROVIDERS)}")
