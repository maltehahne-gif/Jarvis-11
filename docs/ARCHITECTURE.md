# JARVIS Core 0.1 — Architektur und Entscheidungen

Source of Truth ist `docs/JARVIS_Master_Blueprint_1.0.pdf`. Wo dieser Code und
das Blueprint sich widersprechen, gewinnt das Blueprint.

Dieses Dokument hält fest, **was in Core 0.1 gebaut wurde**, **welche
Entscheidungen dabei getroffen wurden** und **was bewusst noch fehlt**.

---

## 1. Was Core 0.1 ist

Der Meilenstein aus Blueprint 5.4 — das Gehirn, ohne Spektakel. Prinzip 5
("Build core before spectacle") heißt konkret: kein Voice, kein HUD, kein
3D-Globus, keine Tauri-App. Was existiert, ist die Kette aus Abbildung 2:

```
Intent/Event → Context → Plan/Route → Permission Check
  → Execute Tool/Agent → Verify Outcome → Update State → Notification
```

Alles läuft in einem Python-Prozess mit klar getrennten Modulen (Blueprint 4.2,
modularer Monolith).

---

## 2. Modul-Landkarte (Blueprint 5.1)

| Blueprint-Modul | Datei | Status |
|---|---|---|
| State Manager | `src/jarvis/state/manager.py` | Geräte, Missionen, Präsenz, Working Memory |
| Intent Router | `src/jarvis/intent/router.py` | vollständig, deterministisch |
| Context Builder | — | **fehlt**, braucht Memory (Blueprint 8, spätere Phase) |
| Mission Engine | `src/jarvis/mission/engine.py` | vollständig inkl. State Machine 5.3 |
| Planner | — | **fehlt**, in 0.1 plant der Provider einen Schritt pro Turn |
| Agent Coordinator | `src/jarvis/agents/coordinator.py` | vollständig inkl. Loop-Kontrolle |
| Capability Registry | `src/jarvis/capability/registry.py` | vollständig |
| Permission Engine | `src/jarvis/permission/engine.py` | vollständig, P0–P6 |
| Execution Gateway | `src/jarvis/execution/gateway.py` | vollständig |
| Verifier | `src/jarvis/verify/verifier.py` | vollständig |
| Event Bus | `src/jarvis/events/bus.py` | vollständig, in-process |
| Scheduler | — | **fehlt**, erst nötig für Background Missions |
| Model Router | `src/jarvis/routing/model_router.py` | Tabelle aus Blueprint 6.1 |
| Audit Logger | `src/jarvis/audit/logger.py` | vollständig, hash-verkettet |

Die fehlenden drei Module sind nicht vergessen, sondern außerhalb der Exit-
Kriterien von 5.4. Sie brauchen jeweils etwas, das es in 0.1 noch nicht gibt
(Memory, echte Mehrschritt-Pläne, Background Jobs).

---

## 3. Sicherheits-Invarianten

Diese Eigenschaften sind der Grund, warum der Core existiert. Jede ist durch
Tests abgedeckt.

1. **Nichts läuft ohne Registry-Eintrag.** Eine unbekannte Capability hat kein
   deklariertes Risiko-Level und wird abgelehnt, nicht geraten.
2. **P6 ist nie ausführbar.** Weder durch Policy-Override noch durch eine
   Freigabe des Besitzers. Der P6-Mock-Handler wirft eine `AssertionError`,
   falls er je aufgerufen würde — ein Regressionsfehler wäre damit laut, nicht
   still.
3. **Overrides können nur verschärfen.** `stricter()` in `permission/policy.py`
   sorgt dafür, dass ein Konfigurationsfehler Komfort kostet, nie Sicherheit.
4. **Das Modell schlägt vor, der Core entscheidet.** Provider bekommen
   Capability-*Beschreibungen*, nie Handler, nie Secrets. Jeder vorgeschlagene
   Aufruf durchläuft Schema-Validierung und Permission-Check erneut.
5. **Freigaben sind an einen Fingerprint gebunden und einmalig.** Zustimmung zu
   „Nachricht an Anna" ist keine Zustimmung zu „Nachricht an alle".
6. **Rechte laufen ab.** Capability Grants sind mission-scoped und haben eine
   TTL (Standard 15 Minuten).
7. **Der Kill Switch steht über allem.** Er wird vor jeder Permission-Prüfung
   und in jedem Agent-Turn geprüft, ohne Modell und ohne Netzwerk.
8. **Der Audit-Log ist manipulations-*erkennend*.** Hash-Kette, kein
   Manipulationsschutz — auf der Platte des Besitzers ist das die ehrliche
   Zusage. Append-only-Storage und Off-Box-Replikation sind spätere Härtung.
9. **Persistieren vor Verkünden.** Ein Crash zwischen beidem kann die
   Benachrichtigung verlieren, nie die Tatsache.
10. **Die API hört nur auf Loopback.** Remote-Zugriff kommt später über einen
    privaten Mesh-Tunnel (Blueprint 10.3), nicht durch Öffnen dieses Ports.

---

## 4. Getroffene Entscheidungen

### 4.1 SQLite statt PostgreSQL — für 0.1, hinter einem Port

Blueprint 4.3 nennt **PostgreSQL + pgvector** als Zielspeicher. Core 0.1 nutzt
stattdessen einen SQLite-Adapter.

**Begründung:** pgvector wird für Vektor-Retrieval im Memory gebraucht — das ist
Blueprint 8 und damit eine spätere Phase. Core 0.1 braucht Persistenz, keine
Vektorsuche. SQLite braucht keinen Server-Prozess, was die Local-first-Zusage
(Prinzip 3) auch auf einer Maschine ohne weitere Installation einlöst und die
Testsuite hermetisch hält.

**Warum das keine Architekturabweichung ist:** Der Core spricht ausschließlich
über die Protokolle in `persistence/ports.py` mit dem Speicher. Das Schema nutzt
bewusst die Form *indizierte Spalten + JSON-Dokument*, die 1:1 auf `jsonb` +
Indizes in PostgreSQL abbildet. Der PostgreSQL-Adapter ist ein Geschwister-
Modul, kein Redesign.

**Entscheidung liegt bei dir:** Wenn PostgreSQL schon in Phase 0 stehen soll,
sag Bescheid — dann ist das ein zusätzlicher Adapter, kein Umbau.

### 4.2 Rule-Based Provider statt Claude Agent SDK — bewusst

Core 0.1 läuft auf `agents/rule_provider.py`, nicht auf dem Claude Agent SDK.

**Begründung:** Prinzip 1 sagt, Claude ist ein austauschbarer Intelligence
Provider. Wenn der Core nur funktioniert, solange Claude erreichbar ist, *ist*
Claude das System — genau die Kopplung, die Blueprint 4.1 ablehnt. Den Core
zuerst gegen einen deterministischen Provider zu bauen, macht diese Trennung
strukturell statt aspirativ.

Der Rule-Provider ist außerdem kein Wegwerf-Stub: er ist die Zeile „Offline /
private Basics: lokales Modell + deterministic intents" aus Blueprint 6.1 und
bleibt der Fallback für Offline-Betrieb und `SECRET`-Verkehr.

Das Claude Agent SDK kommt hinter demselben `IntelligenceProvider`-Port dazu —
additiv. Dafür brauche ich von dir die Entscheidung zu API-Key/Abo (Blueprint
6.2, „Billing/Auth").

### 4.3 Jedes Kommando wird eine Mission

Auch „Licht an". Eine Mission ist die Einheit, die Correlation-ID, ablaufenden
Capability Grant und Audit-Spur trägt — und Blueprint 7.2 will die bei *jeder*
Aktion, nicht nur bei langlaufenden. Billige Kommandos durchlaufen die State
Machine einfach schnell.

### 4.4 Eine gemeinsame Regeltabelle für Router und Provider

`intent/rules.py` wird von Intent Router *und* Rule-Provider genutzt. Zwei
parallele Tabellen würden auseinanderdriften — ein Satz, der auf der Couch
funktioniert, muss auch im Flugzeug funktionieren.

**Nebenwirkung:** In 0.1 kann der Agent-Pfad keine Tool-Calls produzieren, die
der Router nicht schon lokal erledigt hätte. Das ist korrekt und ehrlich für
einen Core ohne echten Reasoner — die Maschinerie dahinter ist trotzdem
vollständig getestet (`tests/test_agent_path.py` nutzt Scripted Provider).

---

## 5. Exit-Kriterien 5.4 → Tests

Jedes Kriterium hat eine eigene Testklasse in `tests/test_dod_core_01.py`.

| # | Kriterium | Testklasse |
|---|---|---|
| 1 | Textkommando erreicht Core über lokale API/WebSocket | `TestCriterion1LocalApi` |
| 2 | Intent Router wählt zwischen lokalem Mock-Tool und Claude-Agent | `TestCriterion2IntentRouter` |
| 3 | Permission Engine blockiert/erlaubt/fordert Bestätigung | `TestCriterion3PermissionEngine` |
| 4 | Tool Registry führt mindestens drei Mock-Tools aus | `TestCriterion4ToolRegistry` |
| 5 | Jede Aktion erzeugt Events und Audit-Logs | `TestCriterion5EventsAndAudit` |
| 6 | Mission bleibt nach Prozessneustart erhalten | `TestCriterion6MissionSurvivesRestart` |
| 7 | Verifier unterscheidet „aufgerufen" von „erreicht" | `TestCriterion7Verifier` |
| 8 | Keine UI außer minimalem Debug-Dashboard | `TestCriterion8MinimalDebugUiOnly` |

---

## 6. Was als Nächstes ansteht

Nach Prinzip 5 („build core before spectacle") und in dieser Reihenfolge:

1. **Claude Agent SDK hinter dem bestehenden Port** — braucht deine
   Billing/Auth-Entscheidung.
2. **Context Builder + Memory** (Blueprint 8) — Privacy-Filter, strukturierte
   Stores, Knowledge Graph. Hier wird PostgreSQL + pgvector relevant.
3. **Planner + Scheduler** — echte Mehrschritt-Pläne mit Dependencies und
   Checkpoints, Background Missions.
4. **Voice Engine** (Blueprint 9) — Wake Word, Streaming STT/TTS, Barge-in.
5. **HUD** (Blueprint 3) — erst danach.
