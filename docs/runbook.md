# Runbook: Autonomous Trading Firm

## 1. Start

Voraussetzungen:
- Python 3.11+
- Capital.com API-Credentials in `.env` (Demo) – s. `.env.example`
  (ohne Keys läuft die Firma automatisch im **Mock-Broker**, PAPER-Modus)
- LLM: **Ollama** läuft (`ollama serve`) und das Modell ist geladen:
  `ollama pull llama3.1:8b` – Standard. Oder Anthropic-Key + `LLM_PROVIDER=anthropic`.

Start (lokal, ohne Dashboard):

```bash
python -m src.firm run --mode PAPER
```

Start mit Live-Office-Dashboard:

```bash
python -m src.firm run --mode PAPER --dashboard
# Dashboard: http://127.0.0.1:8000/
```

Schnellstart unter Windows: `start.bat` im Projektordner doppelklicken
(macht dasselbe, Fenster bleibt bei Fehlern offen).

Der Firm-Core startet:
1. Broker-Verbindung (Capital.com Demo → Fallback auf Mock-Broker)
2. Money-Zone (RiskGate, ExecutionGateway, KillSwitch, Reconciler)
3. Brain-Zone (LLM-Provider, Rollen aus `config/roles/`, CEO-Delegation, Task-Queue)
4. Crash-Recovery: Tasks, die ein Absturz in `RUNNING` hinterlassen hat, werden
   beim Start automatisch wieder auf `PENDING` gesetzt.

Stopp (sauber, mit Kill-Switch):

```bash
python -m src.firm halt     # Flag + Kill-Switch, Prozess beendet sich in ~1s
python -m src.firm resume   # Flag entfernen, danach Neustart möglich
```

Status / Diagnose:

```bash
python -m src.firm status   # Modus, LLM, Tasks, Balance, Broker, Halt-Flag
python -m src.firm doctor   # .env, Ollama/Modell, Rollen-Konfiguration
```

Exit-Code von `doctor` ist 1, wenn Kern-Checks fehlschlagen (CI-tauglich).

## 2. Dashboard & API

- Seite: `http://127.0.0.1:8000/` (Token wird beim ersten Laden abgefragt)
- Die Seite ist **offline-fähig**: keine CDN-Abhängigkeiten (kein Tailwind/Three.js),
  alles inline. Sie rendert auch dann, wenn das LLM oder der Broker gerade hängt.
- Panels: Office Floor (ein Desk pro echtem Agent, klickbar), Staff (Rolle, Modell,
  Lessons, RUNNING/IDLE), Live Feed (Filter THINK/LEARNED/TASKS), Task Board
  (PENDING/RUNNING/DONE/FAILED), „Learned by the Firm" (Lektionen mit Confidence),
  Inspector, Kill Switch.
- Zahlen erscheinen erst, wenn echte Daten da sind (`—` davor). Kein Mock-Balance-Wert.
- Health (ohne Auth): `GET /health`
- API (Bearer-Token, falls `FIRM_API_TOKEN` gesetzt):
  - `GET /api/status`, `GET /api/events`, `GET /api/tasks`, `GET /api/agents`
  - `GET /api/hiring`, `POST /api/hire/{id}`
  - `POST /api/halt` (Kill-Switch)
  - `WS /ws/office?token=...`
- `FIRM_API_TOKEN=HIER_EINFUEGEN` gilt als **nicht gesetzt** → API ist offen,
  aber nur auf `127.0.0.1`. Für Zugriff aus dem LAN einen echten Token setzen.
- Der Start bindet **nicht** an `0.0.0.0`, solange kein Token gesetzt ist
  (Schutz vor offenem API-Zugriff).

## 3. LLM-Wechsel (Ollama → Anthropic)

```bash
# Auf Ollama (lokal, default)
python -m src.firm llm --provider ollama

# Auf Anthropic (Upgrade) – ANTHROPIC_API_KEY muss in .env stehen
python -m src.firm llm --provider anthropic
```

Anschließend den Firmen-Prozess neu starten (`python -m src.firm run`).
Hinweis: Ein Claude-Pro-Abo enthält **keinen** API-Zugang – der Key wird unter
`console.anthropic.com` erzeugt und wird pro Token abgerechnet.

## 4. Selbstverbesserung (Lernschleife)

Jeder Agent läuft **Beobachten → Denken → Handeln → Reflektieren**:

1. **Denken:** LLM-Aufruf mit Rolle, Aufgabe, eigenen letzten Ergebnissen und
   seinen bisherigen Lektionen.
2. **Handeln:** Das Ergebnis (Deliverable) wird als `RESULT`-Event geloggt.
   Es werden **keine** Tool-Ausgaben oder Kurse erfunden.
3. **Reflektieren:** Zweiter LLM-Aufruf – „Was mache ich nächstes Mal anders?"
   Max. 2 Lektionen, **jede Zeile muss mit `LESSON: ` beginnen**. Nur so
   markierte Sätze werden gespeichert (`LEARNED`-Event, mit `evidence_task_id`).
   Das ist absichtlich streng: ein Modell, das das Format ignoriert, erzeugt
   **keine** Lektion – eine schlechte Lektion wäre schlimmer als keine.

Vor der nächsten Aufgabe werden die stärksten, nicht abgelaufenen Lektionen
(max. 6) in den System-Prompt injiziert → das Verhalten ändert sich messbar
(`lessons_applied` in jedem `THOUGHT`-Event).

Grenzen (im Code, nicht durch das LLM):
- Lektionen sind reiner Text und werden **nie ausgeführt**.
- Sie können Rolle, Tool-Allowlist, Risiko-Limits und Money-Zone **nicht** ändern.
- Max. 20 Lektionen pro Agent; TTL 30 Tage, wird durch erneute Anwendung verlängert.
- Bei LLM-Ausfall wird **nicht** gelernt (kein Lernen aus Fallback-Texten).
- Jede Rolle kann ihr eigenes Modell fahren: `model:` in `config/roles/*.yaml`
  (z. B. ein größeres lokales Modell nur für den CEO). Leer = `OLLAMA_MODEL`.

Steuerung und Einsicht:

```bash
FIRM_REFLECT=0 python -m src.firm run   # Lernschleife aus (spart Tokens/CPU)
python -m src.firm status               # "Lessons learned: N"
GET /api/lessons?agent=CEO              # Lektionen ansehen
```

Noch **nicht** implementiert (bewusst, aus dem Master-Prompt): versionierte
Playbooks mit Freigabe-Diff, automatische Widerspruchserkennung, Qualitäts-
Kennzahlen pro Agent. Diese brauchen echte Bewertungsdaten, keine Schätzwerte.

## 4b. Handels-Desk, Forschung und Agenten (Stand Audit 2026-10)

Ablauf ohne Handarbeit:

```
Start -> Broker-Login (Retry, kein stiller Mock) -> Forschungslauf, falls kein Urteil < 7 Tage
      -> data/strategy_verdicts.json -> Strategie-Tor -> Desk (jede abgeschlossene Kerze)
      -> RiskGate (mit Waehrungsumrechnung) -> ExecutionGateway -> Broker
```

- **Forschung:** `src/firm/research/pipeline.py`. 5 Kandidaten (EMA-Baseline, Donchian-Trend,
  Zeitreihen-Momentum, RSI-Reversion, lernendes Modell `ml_logit`), Walk-Forward mit echtem
  Spread, Slippage und Uebernacht-Finanzierung (ANNAHME in `config/costs.toml`), Trial-Counter
  wird fortgeschrieben. Automatisch beim Start (ohne gueltiges Urteil), danach alle
  `FIRM_RESEARCH_DAYS` Tage oder auf Anfrage eines Agenten (max. 1x/24 h).
  Von Hand: `python scripts/research_run.py --days 365`.
- **Strategie-Tor:** PASS = volles Risiko, FORWARD_TEST = Demo/halbes Risiko,
  EXPLORE = Demo/Viertel-Risiko (`FIRM_DEMO_EXPLORE=0` schaltet ab). Im LIVE-Modus nur PASS.
  Urteil fuer andere Kerzen als `FIRM_TRADE_RESOLUTION` = kein Handel (mit Begruendung).
- **Kill-Kriterien:** je Markt+Strategie auf echten R-Werten (6 Verluste in Folge,
  Live-DD >= 10 R, Erwartungswert <= -0.3 R nach 20 Trades) -> `STRATEGY_STOPPED`, gesperrt
  bis zum naechsten Forschungsurteil.
- **Warum handelt die Firma nicht?** Dashboard-Panel *STRATEGY GATE* und Event `DESK_STATUS`
  (nur bei Aenderung). Nach `FIRM_IDLE_ALERT_HOURS` ohne Einstieg: `DESK_IDLE` -> CEO-Aufgabe.
- **Agenten:** ereignisgesteuert (`triggers`, `schedule_hours` in `config/roles/*.yaml`),
  Antworten als JSON, Zahlen werden gegen das Faktenblatt geprueft (`AGENT_INVALID`).
  Wirkung nur ueber `RiskOverlay`: Risiko senken / Einstiege befristet sperren, nie erhoehen.
- **MCP:** `capitalcom-mcp` als Werkzeugkasten der Agenten, nur lesend
  (Server mit `CAP_ALLOW_TRADING=false`, `CAP_DRY_RUN=true`, Demo-Konto; Client-Allowlist).

Steuerung:

```bash
FIRM_TRADING=0              # Desk komplett aus (nur Analyse/Agenten)
FIRM_TRADE_SECONDS=180      # Scan-Intervall (Default 180 s; ausgewertet wird je abgeschlossener Kerze)
FIRM_TRADE_RESOLUTION=HOUR  # MUSS zur Forschung passen
FIRM_RESEARCH_DAYS=7
FIRM_DEMO_EXPLORE=1
FIRM_MCP=1
FIRM_MAX_API_ERRORS=10         # API-Fehlerlimit ...
FIRM_API_ERROR_WINDOW_MIN=30   # ... innerhalb dieses Fensters -> keine neuen Orders
FIRM_REPORT_DIR=reports        # Tagesberichte (UTC), automatisch
```

Limitbruch (Tagesverlust/Drawdown auf Equity): Halt-Flag + Kill-Switch (Flatten). Wiederanlauf nur von Hand:

```bash
python -m src.firm resume   # erst nach Pruefung der Ursache
```

Sicherheit: Im PAPER-Modus verweigert der Desk den Handel, wenn der Broker kein
Demo-Host ist. LIVE braucht weiterhin den Approval-Token und ein PASS-Urteil.

## 4c. Archiv (Aktenschrank fuer Menschen und Agenten)

Der Event-Store (SQLite) ist das unveraenderliche Protokoll, an dem der Code
laeuft. Das Archiv daneben ist der **Aktenschrank**: einfache JSON-Dateien, die
ein Mensch oeffnen kann, ein Ordner pro Agent, ein Ordner pro Handelstag.

```
archiv/
  INDEX.md                      Stand + Aufbewahrungsregeln (vom Curator)
  README.md                     Erklaerung des Layouts
  trades/
    ledger.jsonl                eine Zeile pro Trade-Event (append-only)
    2026-10-24/*.json           ein File pro Fill/Close
    compressed/2026-10.jsonl.gz alte Monate, komprimiert
  agents/<Agent>/
    tasks/*.json                die letzten Aufgaben inkl. Ergebnis
    tasks/compressed/*.gz       aeltere Aufgaben
    actions.jsonl               was der Agent konkret getan hat
    summary.md                  Kurzfassung fuer den Agenten selbst
```

- **Wer schreibt:** der Desk schreibt Fills/Closes, der Orchestrator jede
  erledigte Aufgabe. Archiving ist best-effort und kann den Handel nie stoppen.
- **Wer liest:** jeder Agent bekommt vor dem Denken seinen eigenen Verlauf
  (`context_for`) in den Prompt. Genau das macht aus "36 Lektionen" Erfahrung.
- **Aufraeumen:** der `ArchiveCurator` ist deterministischer Code, kein LLM.
  Er behaelt die letzten 40 Task-Files pro Agent und 3 Handelstage, gzippt den
  Rest und schreibt `INDEX.md`. Vor dem Loeschen wird geprueft, dass die Daten
  im `.gz` angekommen sind. Der Agent "Archivist" darf nur berichten, nie
  loeschen.
- **Paarung:** ein Close wird ueber die Deal-ID dem Fill zugeordnet. Liefert
  der Broker eine andere Positions-ID, wird nur dann verknuepft, wenn genau
  ein offener Fill zu Symbol/Richtung/Groesse passt - nie geraten. Exit und
  P&L werden nur angezeigt, wenn der Broker sie bestaetigt hat.

Steuerung:

```bash
FIRM_ARCHIVE_DIR=archiv          # Wurzel
FIRM_ARCHIVE_KEEP_TASKS=40       # Task-Files pro Agent
FIRM_ARCHIVE_KEEP_TRADE_DAYS=3   # Handelstage, die unkomprimiert bleiben
```

API/Dashboard: `GET /api/archive`, `/api/archive/trades`,
`/api/archive/agents/{agent}`; im Office-Dashboard das Panel **TRADE HISTORY**
(Ordner-Tab `archiv/trades`, 20-s-Auto-Refresh).

## 5. Live-Modus

```bash
python -m src.firm approve-live --duration 3600
python -m src.firm run --mode LIVE_MICRO
```

Nur mit gültigem, nicht abgelaufenem Token (`live_token.json`). Ohne Token
lehnt das ExecutionGateway jeden Live-Order ab. Limits in `config/limits.toml`
bleiben unverändert und sind für Agenten read-only.

## 6. Docker

```bash
# FIRM_API_TOKEN in .env setzen (Pflicht bei 0.0.0.0-Bind)
docker compose up --build
```

Ein Prozess (`run --dashboard --host 0.0.0.0`), Healthcheck auf `/health`,
`restart: unless-stopped`.

## 7. Störungsfälle

- **LLM fällt aus:** Agent-Runtime fällt auf deterministischen Gedanken-Fallback
  zurück (kein Crash-Loop).
- **Broker down:** Mock-Fallback / Reconciler-Alarm → Kill-Switch prüfen.
- **Absturz mitten in einem Task:** beim nächsten Start via `requeue_stale()`
  wieder aufgenommen.
- **Events:** SQLite Event-Store (WAL) – immutable, append-only.

## 8. Tests

```bash
python -m pytest tests/ -q
```
