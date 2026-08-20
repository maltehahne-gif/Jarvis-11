"""JARVIS Core.

A persistent, permission-controlled personal AI operating system core, built to
the specification in `docs/JARVIS_Master_Blueprint_1.0.pdf`. That document is
the source of truth; where this code and the blueprint disagree, the blueprint
wins.

The five non-negotiable principles it opens with shape every module here:

1. JARVIS Core is the product; Claude is a swappable intelligence provider.
2. Rights, security, memory, device identity and tool execution are controlled
   deterministically by our software - never by a prompt alone.
3. Local-first: personal data and state stay on the owner's devices.
4. Fluid-first: wake word, HUD, local actions and status feedback never wait on
   slow cloud reasoning.
5. Build core before spectacle.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
