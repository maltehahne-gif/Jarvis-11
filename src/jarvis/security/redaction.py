"""Credential detection - Blueprint 7.2 and 7.3.

"Secrets werden über Credential Broker/OS Keychain/Vault genutzt; das Modell
soll Schlüssel möglichst nie im Klartext sehen" and, as the countermeasure to
prompt injection, "keine secrets im prompt".

Two call sites need the same answer to the same question, and they must never
drift apart:

* the `SecretsInParamsGate` (`permission/policy.py`), which refuses to pass
  credential material into a tool call, and
* the memory Privacy Filter (`memory/privacy.py`), which refuses to *store*
  credential material in the first place.

Detection is prefix-based on purpose. A heuristic that guesses from entropy
would fire on hashes, UUIDs and base64 payloads that are perfectly safe to
keep, and every false positive here silently drops something the owner wanted.
Known prefixes are boring, explainable, and wrong in only one direction: a
novel key format slips through, which is why this is one layer among several
rather than the only one.
"""

from __future__ import annotations

#: Prefixes and framing that reliably indicate key material rather than prose.
CREDENTIAL_MARKERS: tuple[str, ...] = (
    "-----BEGIN",  # PEM private keys and certificates
    "sk-ant-",  # Anthropic API keys
    "sk-",  # OpenAI-style API keys
    "AKIA",  # AWS access key ids
    "ASIA",  # AWS temporary access key ids
    "ghp_",  # GitHub personal access tokens
    "gho_",  # GitHub OAuth tokens
    "github_pat_",  # GitHub fine-grained tokens
    "xoxb-",  # Slack bot tokens
    "xoxp-",  # Slack user tokens
    "AIza",  # Google API keys
)


def find_credential_marker(text: str) -> str | None:
    """Return the first marker found in `text`, or `None`."""
    for marker in CREDENTIAL_MARKERS:
        if marker in text:
            return marker
    return None


def looks_like_credential(value: object) -> bool:
    """True when `value` is a string carrying recognisable key material.

    Non-strings are never credentials by this test; callers that accept nested
    structures should walk them and test the leaves.
    """
    return isinstance(value, str) and find_credential_marker(value) is not None


def contains_credential(value: object, *, _depth: int = 0) -> bool:
    """Recursively test a value, including inside dicts and sequences.

    Depth is bounded so a pathological or cyclic structure cannot turn a
    security check into a hang.
    """
    if _depth > 6:
        return False
    if isinstance(value, str):
        return find_credential_marker(value) is not None
    if isinstance(value, dict):
        return any(contains_credential(v, _depth=_depth + 1) for v in value.values()) or any(
            contains_credential(k, _depth=_depth + 1) for k in value
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(contains_credential(v, _depth=_depth + 1) for v in value)
    return False
