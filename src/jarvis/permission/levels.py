"""Permission levels P0-P6 - Blueprint 7.1.

The level is a property of the *action*, declared by the capability that
implements it. It is never inferred from a model's output, and never supplied
by the caller: that is the whole point of "Rechte ... werden deterministisch von
unserer Software kontrolliert; niemals nur durch einen Prompt" (Principle 2).
"""

from __future__ import annotations

from enum import IntEnum


class PermissionLevel(IntEnum):
    """Ordered by risk, so policies can express "at or above" thresholds."""

    P0_OBSERVE = 0
    P1_SAFE = 1
    P2_REVERSIBLE = 2
    P3_SENSITIVE = 3
    P4_CRITICAL = 4
    P5_RESTRICTED = 5
    P6_FORBIDDEN = 6

    @property
    def label(self) -> str:
        return {
            PermissionLevel.P0_OBSERVE: "observe",
            PermissionLevel.P1_SAFE: "safe",
            PermissionLevel.P2_REVERSIBLE: "reversible",
            PermissionLevel.P3_SENSITIVE: "sensitive",
            PermissionLevel.P4_CRITICAL: "critical",
            PermissionLevel.P5_RESTRICTED: "restricted",
            PermissionLevel.P6_FORBIDDEN: "forbidden",
        }[self]

    @property
    def code(self) -> str:
        return f"P{int(self)}"

    def __str__(self) -> str:
        return self.code


#: Human-readable examples straight from the blueprint table, used by the debug
#: dashboard and by error messages so a denial explains itself.
LEVEL_EXAMPLES: dict[PermissionLevel, str] = {
    PermissionLevel.P0_OBSERVE: "Bildschirmstatus, Gerätezustand, read-only Sensoren",
    PermissionLevel.P1_SAFE: "App öffnen, Licht schalten, lokale Suche",
    PermissionLevel.P2_REVERSIBLE: "Datei verschieben, Fenster anordnen",
    PermissionLevel.P3_SENSITIVE: "E-Mail senden, Kalender ändern, Nachricht senden",
    PermissionLevel.P4_CRITICAL: "Software installieren, Deploy, Shutdown mit laufenden Jobs",
    PermissionLevel.P5_RESTRICTED: "Secrets, Admin-/Security-Einstellungen",
    PermissionLevel.P6_FORBIDDEN: "explizit gesperrte Aktionen",
}
