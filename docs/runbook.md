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

## 4b. Handels-Desk (deterministisch, ohne LLM)

Der LLM-Agent analysiert und lernt, aber er **handelt nicht**. Orders entstehen
ausschliesslich im `TradingDesk` (`src/firm/money/trading_desk.py`):

```
Kerzen -> Signal -> Positionsgroesse -> RiskGate -> ExecutionGateway -> Broker
```

- **Signal:** EMA12/EMA48-Crossover, gefiltert mit `|EMA-Gap| >= 0.25 * ATR14`.
  Stop = 1.5 ATR, Ziel = 3 ATR (2:1). In Seitwaertsphasen wird nicht gehandelt.
- **Groesse:** `balance * max_risk_per_trade / Stop-Distanz`, abgerundet auf den
  echten Broker-Increment, begrenzt durch Broker-Min/Max und `max_leverage`.
- **Ausstieg:** dreht der Trend gegen die Position, wird sie geschlossen.
- **Symbole:** aus `config/whitelist.toml` (BTCUSD, EURUSD, GOLD, AAPL).
  Geschlossene Maerkte werden uebersprungen (Status kommt live vom Broker).
- Jede Entscheidung landet als `SIGNAL`, `ORDER_FILLED`, `TRADE_REJECTED`,
  `POSITION_CLOSED` oder `DESK_CYCLE` im Event-Store und im Dashboard-Filter TRADES.

Steuerung:

```bash
FIRM_TRADING=0            # Desk komplett aus (nur Analyse/Agenten)
FIRM_TRADE_SECONDS=180    # Scan-Intervall (Default 180 s)
FIRM_TRADE_RESOLUTION=MINUTE_15
FIRM_TRADE_COOLDOWN=900   # Sperre pro Symbol nach einem Trade (Sekunden)
FIRM_TRADE_TIMEOUT=120    # harte Obergrenze pro Scan
```

Sicherheit: Im PAPER-Modus verweigert der Desk den Handel, wenn der Broker kein
Demo-Host ist. LIVE braucht weiterhin den Approval-Token.

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
