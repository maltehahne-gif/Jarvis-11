# PROJECT J.A.R.V.I.S.

Persönliches AI-Operating-System. Kein Chatbot, keine Film-Demo: ein
installierbares, privates, geräteübergreifendes System mit eigenem Core,
langfristigem Memory, kontrollierter Tool- und Geräteausführung und
Multi-Agent-Orchestrierung.

**Source of Truth:** [`docs/JARVIS_Master_Blueprint_1.0.pdf`](docs/JARVIS_Master_Blueprint_1.0.pdf).
Wo Code und Blueprint sich widersprechen, gewinnt das Blueprint.

**Aktueller Stand: Core 0.1** — der Meilenstein aus Blueprint 5.4. Das Gehirn
steht, ohne Voice, HUD oder 3D. Architektur und getroffene Entscheidungen:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Die fünf nicht verhandelbaren Prinzipien

1. **JARVIS Core ist das Produkt.** Claude ist ein austauschbarer Intelligence
   Provider, nicht das gesamte System.
2. **Rechte, Sicherheit, Memory, Geräteidentität und Tool-Ausführung** werden
   deterministisch von unserer Software kontrolliert — niemals nur durch einen
   Prompt.
3. **Local-first.** Persönliche Daten, Event-State und Memory bleiben zuhause.
4. **Fluid-first.** Wake Word, HUD, lokale Aktionen und Statusfeedback warten
   nie auf langsames Cloud-Reasoning.
5. **Build core before spectacle.**

## Start

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

.venv/bin/python -m pytest          # 131 Tests
.venv/bin/python -m jarvis          # http://127.0.0.1:8765
```

Das Debug-Dashboard unter `/` zeigt Kommandoeingabe, Live-Event-Stream,
Missionsstatus, offene Freigaben und die Capability-Tabelle mit Risiko-Leveln.

## Ausprobieren

```bash
J=http://127.0.0.1:8765
post() { curl -s -X POST $J/command -H 'Content-Type: application/json' -d "$1"; }

post '{"text":"Licht im Office an"}'                        # P1 → läuft sofort
post '{"text":"mach einen factory reset"}'                  # P6 → nie ausführbar
post '{"text":"Nachricht an anna: hi"}'                     # P3 → Scope fehlt
post '{"text":"Nachricht an anna: hi","grants":["comms"]}'  # P3 → Freigabe nötig
post '{"text":"Jarvis, stopp alles"}'                       # Kill Switch
```

Weitere Endpunkte: `/status`, `/capabilities`, `/missions`, `/events`, `/audit`,
`/routing`, WebSocket auf `/ws`.

## Permission-Level (Blueprint 7.1)

| Level | Klasse | Standard |
|---|---|---|
| P0 | Observe | automatisch, protokolliert |
| P1 | Safe | automatisch |
| P2 | Reversible | automatisch + Undo/Log |
| P3 | Sensitive | Bestätigung je Kontext |
| P4 | Critical | starke Bestätigung / biometrisch |
| P5 | Restricted | spezielle Policies, device-bound |
| P6 | Forbidden | **nie ausführen** |

## Konfiguration

| Variable | Standard | Zweck |
|---|---|---|
| `JARVIS_DB` | `data/jarvis.db` | Speicherort (local-first) |
| `JARVIS_HOST` | `127.0.0.1` | Bind-Adresse — Loopback, siehe Blueprint 7.2 |
| `JARVIS_PORT` | `8765` | Port |
| `JARVIS_TRUSTED_DEVICES` | leer | Device-IDs für P5-Aktionen |
| `JARVIS_BLOCKED_CAPABILITIES` | leer | zusätzliche Deny-Liste |
