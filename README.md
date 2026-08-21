# PROJECT J.A.R.V.I.S.

Persönliches AI-Operating-System. Kein Chatbot, keine Film-Demo: ein
installierbares, privates, geräteübergreifendes System mit eigenem Core,
langfristigem Memory, kontrollierter Tool- und Geräteausführung und
Multi-Agent-Orchestrierung.

**Source of Truth:** [`docs/JARVIS_Master_Blueprint_1.0.pdf`](docs/JARVIS_Master_Blueprint_1.0.pdf).
Wo Code und Blueprint sich widersprechen, gewinnt das Blueprint.

**Aktueller Stand:** alle Core-Module aus Blueprint 5.1 sind gebaut — inklusive
Memory und Personalisierung (8), Context Builder, Planner, Mission Runner mit
Checkpoints, Scheduler und Watchdog. Dazu die Voice Engine (9) als
Streaming-Pipeline mit Personality Contract, Latenz-Budget und
Presence-Routing; echte Wake-/STT-/TTS-Engines treten hinter die Ports, sobald
ein Gerät mit Mikrofon dran ist. Claude-Agent-SDK-Provider steht hinter dem
Intelligence-Port (6.2). Dazu ein HUD-Frontend ([`hud/`](hud/)) mit Idle-,
Mission- und System-Modus; fünf weitere Modi und der 3D-Globus folgen. Architektur und
getroffene Entscheidungen: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

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

.venv/bin/python -m pytest          # 413 Tests
.venv/bin/python -m jarvis          # http://127.0.0.1:8765
```

Das Debug-Dashboard unter `/` zeigt Kommandoeingabe, Live-Event-Stream,
Missionsstatus, offene Freigaben, die Capability-Tabelle mit Risiko-Leveln und
„What JARVIS Knows" mit Privacy-Schaltern und Routine-Vorschlägen.

Für das HUD (siehe [`hud/README.md`](hud/README.md)) zusätzlich, bei
laufendem Core:

```bash
cd hud && npm install && npm run dev   # http://localhost:5173
```

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

## Memory kontrollieren

```bash
curl -s $J/memory                       # alles, was JARVIS glaubt
curl -s "$J/memory/search?q=licht"      # Retrieval mit Score
curl -s $J/memory/routines              # vorgeschlagene Routinen
curl -s $J/memory/privacy               # die drei Privacy-Schalter

# "Jarvis, vergiss die letzten 30 Minuten" — gepinnte Einträge bleiben
curl -s -X POST $J/memory/forget-window -H 'Content-Type: application/json' \
     -d '{"minutes":30}'
```

Pro Eintrag: `/memory/{id}/correct`, `/pin`, `/forget`, `/make-temporary`.
Blockieren mit `/memory/dont-learn`.

## Planen und Zeitsteuerung

```bash
# Trockenlauf: Plan, Risiko und Budget prüfen, ohne etwas auszuführen
curl -s -X POST $J/plan -H 'Content-Type: application/json' \
     -d '{"text":"installiere Docker"}'

# Ein zeitgesteuerter Job
curl -s -X POST $J/scheduler/jobs -H 'Content-Type: application/json' \
     -d '{"name":"Abendlicht","goal":"Licht im Living an",
          "capability":"home.set_light","params":{"room":"living","state":"on"},
          "kind":"daily","daily_at":"20h"}'

curl -s $J/scheduler                     # Jobs, Versuche, nächster Lauf
curl -s -X POST $J/scheduler/tick        # jetzt fällige ausführen
curl -s -X POST $J/watchdog/sweep        # hängende Missionen beenden
curl -s $J/missions/{id}/progress        # Fortschritt und Checkpoints
curl -s -X POST $J/missions/{id}/resume  # ab Checkpoint fortsetzen
```

**Unbeaufsichtigt gilt eine engere Regel als beaufsichtigt.** Ein Job ab P3
aufwärts führt nichts aus, sondern parkt: die Mission wartet auf deine
Freigabe, und ein dringendes Event sagt es dir. In einem unbeaufsichtigten
Kontext kann niemand bestätigen — also bestätigt auch niemand.

## Sprechen

```bash
# Geräte registrieren; private_audio ist eine Privatsphäre-Grenze,
# keine Komforteinstellung
curl -s -X POST $J/devices -H 'Content-Type: application/json' \
     -d '{"device_id":"kitchen-speaker","trusted":true,"has_speaker":true,
          "room":"kitchen","audio_quality":70}'
curl -s -X POST $J/devices -H 'Content-Type: application/json' \
     -d '{"device_id":"phone","trusted":true,"has_speaker":true,"private_audio":true}'

# Eine Sprachrunde (echte Wake-Erkennung liegt auf dem Gerät)
curl -s -X POST $J/voice/wake -H 'Content-Type: application/json' \
     -d '{"text":"Licht im Office an","device_id":"desk-01"}'

curl -s -X POST $J/voice/barge-in                      # Sprachausgabe sofort stoppen
curl -s -X POST $J/voice/mode -d '{"mode":"night"}' \
     -H 'Content-Type: application/json'               # normal|night|whisper|silent
curl -s $J/voice/latency                               # Zielwerte aus 9.2, mit Miss-Rate
curl -s $J/presence                                    # wer könnte antworten
```

**Sensible Antworten landen nie laut im Raum.** Sie brauchen ein privates
Ausgabegerät — Kopfhörer oder Handy. Ist keins online, bleibt JARVIS still und
zeigt es auf einem Bildschirm; ein Raumlautsprecher ist kein Fallback.

**Sprache ist eine Eingabe, keine Vollmacht.** Ein gesprochenes „installiere
Docker" durchläuft dieselbe Rechteprüfung wie ein getipptes.

Was JARVIS *nicht* lernt: Credential-Material (nie, unabhängig von
Einstellungen), alles auf der Don't-Learn-Liste, und bei abgeschaltetem Privacy
Mode das jeweils Betroffene. Screen- und Kamera-Lernen ist standardmäßig aus.

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
| `JARVIS_PROVIDER` | `rules` | `rules` (lokal, offline) oder `claude-agent-sdk` |
