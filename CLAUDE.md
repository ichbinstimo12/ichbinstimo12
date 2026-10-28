# MASTER-PROMPT: Autonome Trading-Firma aus Agents (für Claude Code)

## So benutzt du diese Datei

1. Neuen, leeren Projektordner anlegen (nicht den alten `firm/`-Ordner weiterverwenden).
2. Diese Datei als `CLAUDE.md` in den Projektroot legen (Claude Code liest sie bei jeder Sitzung).
3. Deinen ursprünglichen Trading-Agent-Prompt (AgenKit) als `docs/trading_agent_prompt.md` daneben legen. Er gilt als Teil dieser Spezifikation.
4. Den alten Code als `legacy/` hineinkopieren, nur als Referenz, nichts davon wird übernommen.
5. `.env` nur lokal auf deinem Rechner lassen. Nie in Chats hochladen, nie committen.
6. Claude Code starten und als erste Nachricht senden: **„Lies CLAUDE.md und docs/trading_agent_prompt.md vollständig. Beginne mit Phase 0. Stoppe an jedem Gate und warte auf meine Freigabe."**

Alles unterhalb der Linie ist der eigentliche Prompt.

---

# ROLLE

Du bist ein Senior-Engineer-Team aus: Quant-Systementwickler, Risk Engineer, Backend-Architekt und Frontend-Entwickler. Du baust eine **autonome Trading-Firma, die ausschließlich aus Agents besteht**, mit Live-Büro-Dashboard, lernenden Mitarbeitern und einem Chef-Agent, der die Firma erweitern kann. Du arbeitest nach dem AgenKit-Ablauf: **Spec → Architektur → Plan → Test-first-Bau → Review → Ship**. An jedem Gate stoppst du und wartest auf meine ausdrückliche Freigabe.

Sei skeptisch. Suche aktiv nach Gründen, warum das System scheitern kann. Überleben des Kapitals schlägt jeden beeindruckenden Backtest.

# AUSGANGSLAGE (Audit des Altcodes, Fakten)

Der vorhandene Prototyp in `legacy/` ist ein Attrappen-System, kein Trading-System. Nicht reparieren, ersetzen. Befunde:

- `CapitalComClient` ist komplett gestubbt: `get_balance()` gibt fest `200.0` zurück, `place_order()` loggt nur „REAL ORDER SUCCESS" und sendet nichts. Es gibt keinen Zugang zum Kapitalkonto. Die Basis-URLs im Code sind falsch.
- Marktdaten sind fest codiert (`price: 60000.0`, `funding_rate: 0.0006`). Das Signal feuert deshalb bei jedem Tick.
- `agent.pnl += 1.0` ist erfundener Gewinn. Die Dashboard-Zahlen sind Fiktion.
- Der „Media Veto" ist `random.random() < 0.1`. Das ist Zufall, keine Analyse.
- Drei Trader mit identischer Strategie senden identische Orders, also dreifache Position. Das RiskEngine-Objekt wird geteilt, prüft aber weder Tagesverlust noch Drawdown (die Parameter existieren, werden nie ausgewertet). Kein Kill-Switch funktioniert (`flatten_all` existiert nicht).
- Die „Funding-Arbitrage" ist auf Capital.com konzeptionell unmöglich. Funding Rates gibt es bei Krypto-Perpetuals, nicht bei CFDs. Ohne Gegenposition ist das eine reine Richtungswette.
- `researcher.py` und `firm_manager.py` importieren `src.*`, das nicht existiert. Beide sind nie gelaufen. Es gibt zwei verschiedene `FirmManager` und `FirmState`, eine kaputte Datei `state_manager.py` + Backtick, keine `requirements.txt`, ein Dockerfile, das auf nicht vorhandene Dateien zeigt, und leere Ordner (`core/`, `risk/`, `strategies/`, `data/`).
- `/control` im API schreibt in eine Textdatei, die niemand liest. CORS ist `*`, es gibt keine Authentifizierung.
- `office.html` zeigt nur eine Liste aus `/status`. Es gibt keine Detailansicht pro Agent, keine Live-Ereignisse, und Tailwind kommt per CDN.
- Zustand läuft über eine geteilte JSON-Datei mit Race-Conditions. Pfade sind Windows-spezifisch.

Konsequenz: **Von Grund auf neu bauen. Nichts aus `legacy/` übernehmen, ohne es zu testen.**

# OBERSTE PRINZIPIEN (unverletzlich)

1. **Das LLM berührt nie direkt Geld.** Agents erzeugen nur strukturierte Vorschläge (`TradeProposal`, Pydantic-validiert). Ausgeführt wird ausschließlich durch deterministischen Code (Risk Gate → Execution Gateway). Kein Prompt, kein Agent, kein Memory-Eintrag kann Risiko-Limits ändern.
2. **Hard Limits liegen außerhalb der Reichweite der Agents.** Sie stehen in einer schreibgeschützten Konfiguration (`config/limits.toml`), die nur ich per Hand ändere. Agents haben kein Schreibrecht darauf. Der Execution-Prozess ist von den Agent-Prozessen getrennt.
3. **Keine erfundenen Zahlen.** Jede Zahl im Dashboard, in Reports und Backtests stammt aus echten Daten oder echten Berechnungen. Fehlt etwas, zeigt die UI `—` oder `OFFLINE`, nie einen Platzhalter. Schätzungen sind als solche beschriftet.
4. **Modi-Leiter, nur nach oben mit meiner Freigabe:** `BACKTEST → PAPER (Capital.com Demo) → LIVE_MICRO → LIVE`. Standard ist immer `PAPER`. Der Wechsel zu `LIVE_*` erfordert ein von mir erzeugtes Freigabe-Token (CLI-Befehl `firm approve-live`, zeitlich begrenzt, an Kontostand und Limits gebunden). Fehlt es, verweigert der Execution Gateway Live-Orders im Code.
5. **Der Agent bewegt nie Geld.** Keine Ein- oder Auszahlungen, keine Transfers. Nur Orders im Rahmen der Limits.
6. **Kein Gewinnversprechen.** Weder im Code, noch in der UI, noch in Reports. Die Firma darf auch „keine Strategie gefunden, wir handeln nicht" melden. Das ist ein gültiges, gutes Ergebnis.
7. **Jede Entscheidung wird geloggt**: Eingaben, Ausgabe, Begründung, Ergebnis, Zeitstempel. Append-only, nicht nachträglich änderbar.
8. **Secrets:** nur aus `.env`/Umgebungsvariablen, nie in Logs, nie im Dashboard, nie in Agent-Prompts, `.env` in `.gitignore`. Nur der Execution-Prozess kennt die Broker-Keys. Separate Keys für Demo und Live.

# KONTEXT UND REALITÄTSCHECK

- Venue: **Capital.com** (CFD-Broker, Schweiz-Nutzer), Konto startet klein (Größenordnung ca. 200 CHF). Gebühren, Spreads, Overnight-Finanzierung und Mindestordergrößen sind bei dieser Kontogröße oft größer als jeder realistische Edge. **Phase 1 muss das mit echten Zahlen quantifizieren, bevor irgendeine Strategie gebaut wird.** Ergebnis darf lauten: „Mit diesem Kapital ist keine Strategie netto profitabel."
- API-Details (aus meinem Wissen, **du musst alles gegen die offizielle Doku unter open-api.capital.com verifizieren, bevor du es nutzt**): Session per `POST /api/v1/session` mit Header `X-CAP-API-KEY` und Body `identifier`/`password`; die Antwort liefert die Header `CST` und `X-SECURITY-TOKEN`, die bei Inaktivität auslaufen und erneuert werden müssen. Demo und Live haben getrennte Basis-URLs. Relevante Endpunkte: Konten, Märkte/Epics, Preise (historisch), Positionen eröffnen/schließen, Deal-Bestätigung per `dealReference`, WebSocket-Streaming für Live-Kurse. Rate Limits beachten und im Client durchsetzen.
- Erster Schritt der API-Anbindung ist **ausschließlich lesend** (Konto, Kontostand, Märkte, Preise) gegen die **Demo**. Orders erst nach Gate 3.

# ZIELARCHITEKTUR

Python 3.11+, `asyncio`, Pydantic v2, SQLite (WAL-Modus) als Event-Store und Datenbank, FastAPI mit WebSocket für das Dashboard, pytest. Keine geteilte JSON-Datei als Zustand. Plattformunabhängige Pfade (`pathlib`), läuft unter Windows und Linux, Docker-Compose für den VPS.

Zwei strikt getrennte Zonen:

- **Brain-Zone (Agents):** LLM-gestützte Mitarbeiter über die konfigurierte LLM (default: **Ollama** lokal; danach **Anthropic** via API-Key). Dürfen lesen, analysieren, Vorschläge machen. Haben kein Broker-Token.
- **Money-Zone (deterministisch):** `RiskGate`, `ExecutionGateway`, `BrokerClient`, `KillSwitch`, `Reconciler`. Kein LLM-Code. Eigener Prozess, eigene Secrets, einzige Stelle mit Broker-Zugriff.

Kommunikation über einen **Event-Bus** (Tabelle `events`, append-only: `id, ts, agent_id, type, payload_json, correlation_id`). Das Dashboard liest denselben Strom, deshalb sind Dashboard und Wahrheit identisch.

```
trading-firm/
├── CLAUDE.md
├── docs/ (spec.md, architecture.md, plan.md, trading_agent_prompt.md, runbook.md)
├── config/ (limits.toml [read-only], firm.toml, roles/*.yaml)
├── src/firm/
│   ├── bus/            # Event-Store, Pub/Sub
│   ├── money/          # RiskGate, ExecutionGateway, KillSwitch, Reconciler
│   ├── broker/         # CapitalComClient (REST + WS), PaperBroker, Fehlerbehandlung
│   ├── data/           # Loader, Zeitstempel-Disziplin, Qualitätsprüfungen
│   ├── research/       # Backtest-Engine, Walk-Forward, Parameter-Sweep, Trial-Counter
│   ├── strategies/     # Signal-Module (rein, ohne Execution)
│   ├── agents/         # Runtime, Rollen, Tools, Memory, Hiring
│   ├── api/            # FastAPI, WebSocket, Auth
│   ├── llm/            # LLM-Provider (Ollama <-> Anthropic) – Brain-Zone
│   └── cli.py          # firm run | approve-live | halt | status | llm
├── web/ (office.html + js/css, lokal gebündelt, kein CDN)
├── tests/ (unit, integration, property, failure-injection, replay)
├── legacy/             # nur Referenz
└── docker-compose.yml, Dockerfile, requirements.txt, .env.example
```

# LLM-PROVIDER (BRAIN-ZONE) – OLLAMA-FIRST, ANTHROPIC-UPGRADE

Die Agent-Runtime (`src/firm/agents/runtime.py`) kommuniziert ausschließlich über das
LLM-Interface (`src/firm/llm/`): `OllamaLLM` oder `AnthropicLLM`, ausgewählt über
`LLM_PROVIDER` (Default: `ollama`).

- **Phase 1 (lokal, aktiv):** `LLM_PROVIDER=ollama`, `OLLAMA_BASE_URL=http://localhost:11434`,
  `OLLAMA_MODEL=llama3.1`. Start: `ollama serve` · Pull: `ollama pull llama3.1`.
  Kein API-Key, keine Cloud-Kosten, läuft offline.
- **Upgrade auf Anthropic:** `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=<...>`,
  `ANTHROPIC_MODEL=claude-opus-5-5`. API-Key: https://console.anthropic.com → API Keys
  (inkl. pro/upgrade Verfügbarkeit des gewählten Modells). Danach: `python -m src.firm llm --provider anthropic`.

Der Firm-Code (Agent-Runtime, Orchestrator) kennt keinen Provider direkt – er nutzt die
Factory. Ein Provider-Wechsel bedeutet also nur eine Konfigurationsänderung, kein Rewrite.



# DIE MITARBEITER (Agents)

Jeder Mitarbeiter ist **Konfiguration, kein Spezialcode**: `config/roles/<rolle>.yaml` mit Name, Rolle, System-Prompt, Tool-Allowlist, Modell, Tages-Token-Budget, Reporting-Linie. Ein gemeinsames Agent-Runtime führt sie aus.

Startbesetzung (klein halten, erst bei nachgewiesenem Bedarf erweitern):

| Rolle | Aufgabe | Darf nicht |
|---|---|---|
| **CEO** | Priorisiert, vergibt Aufgaben, liest Reports, schlägt Neueinstellungen vor, verteilt Kapital innerhalb der Limits | Limits ändern, Orders senden |
| **Head of Research** | Generiert Strategie-Hypothesen, führt Validierung, verwaltet den Trial-Counter | Strategien selbst live schalten |
| **Quant Developer** | Implementiert Signal-Module und Backtests, Test-first | Zugriff auf Broker |
| **Data Engineer** | Datenqualität, Lücken, Zeitstempel, Look-ahead-Prüfung | Daten rückwirkend ändern |
| **Macro/News Analyst** | Liest echte Quellen (Kalender, Nachrichten), liefert Regime- und Event-Flags mit Quellenangabe | Erfundene Quellen, Veto ohne Beleg |
| **Risk Officer** | Überwacht Kennzahlen, schlägt Limit-Verschärfungen vor, kann **nur verschärfen oder halten**, nie lockern | Limits lockern |
| **Execution Trader** | Wandelt freigegebene Signale in `TradeProposal`, überwacht Fills und Slippage | Am RiskGate vorbei handeln |
| **Red Team / Auditor** | Versucht jede Strategie zu zerstören (Overfitting, Leakage, Kostenempfindlichkeit, Regime-Abhängigkeit), schreibt Kill-Kriterien | Eigene Strategien freigeben |

Jeder Agent-Schritt ist ein Loop aus **Beobachten → Denken → Handeln → Reflektieren**, und jeder Schritt wird als Event geloggt (`THOUGHT`, `RESULT`, `LEARNED`, inkl. Begründungstext, Provider/Modell, Anzahl angewandter Lektionen). Das ist die Datenquelle für die „genau sehen, was er macht"-Ansicht.

## Gedächtnis und Lernen

Pro Agent eine eigene Memory-Datenbank mit drei Ebenen:

- **Episodisch:** Rohchronik eigener Aufgaben und Ergebnisse (automatisch aus Events).
- **Semantisch (Lessons):** Kurze verallgemeinerte Erkenntnisse, jeweils mit `evidence` (Verweis auf Event-IDs), `confidence`, `created_at`, `last_confirmed`, `expires_at`. Entstehen in einem **Reflexionsschritt** nach jeder abgeschlossenen Aufgabe, jedem Trade und jedem Experiment („Was hat funktioniert, was nicht, warum, was ändere ich?").
- **Prozedural (Playbooks):** Versionierte Arbeitsanleitungen pro Rolle, die der Agent bei jeder Aufgabe lädt. Änderungen am Playbook sind Vorschläge, die der CEO oder ich freigebe, mit Diff.

Regeln: Eine Lesson ohne Belegereignis wird nicht gespeichert. Lessons verfallen, wenn sie nicht erneut bestätigt werden. Widersprüche werden markiert statt überschrieben. Memory darf nie Risiko-Limits, Modus oder Freigaben beeinflussen. Beim Abruf werden nur die relevantesten Einträge in den Kontext geladen (Embedding- oder Schlagwortsuche), nicht alles. Memory ist in der UI einsehbar und löschbar.

Lernen ist **messbar**: pro Agent Kennzahlen im Zeitverlauf (z. B. Anteil korrekter Prognosen, Fehlerquote, Aufgabenqualität laut Auditor). Keine erfundene „Erfahrungspunkte"-Zahl, `experience` muss aus echten Größen berechnet sein.

**Umsetzungsstand (Code):** `src/firm/agents/memory.py` (`LessonStore`) + `AgentRuntime.step()`.
Real: episodisch (Events), semantisch (Lessons mit `evidence_task_id`, `confidence`, `expires_at`, `applied_count`), Reflexionsschritt nach jeder Aufgabe, Injektion der Top-6-Lektionen in den Prompt, Harte Grenzen (max. 20/Agent, TTL 30 Tage, kein Lernen bei LLM-Ausfall, Lektionen sind nie ausführbar).
Qualitätsmetriken je Agent: `src/firm/agents/metrics.py` (gültige Antworten, verworfene, Aktionen, Tokens, je Tag).
Prozedural: `src/firm/agents/playbooks.py` (versionierte Playbooks, Änderung nur per Vorschlag mit Diff und Freigabe durch den Operator).
Widersprüche: `LessonStore` markiert widersprüchliche Lektionen gegenseitig (`contradicts`), nichts wird überschrieben.

## Expansion durch den Chef

Der CEO erkennt Engpässe aus Daten (Aufgaben-Rückstau, Wartezeiten, Fehlerraten) und erstellt einen **Hiring Request**: Rolle, Begründung mit Metriken, erwarteter Nutzen, Kosten (Token-Budget pro Tag).

- Neue Agents entstehen nur aus **Rollen-Templates** (`config/roles/`) oder aus einer vom CEO entworfenen neuen Rolle, die als YAML-Vorschlag zur Freigabe vorgelegt wird.
- Harte Obergrenzen in `limits.toml`: maximale Mitarbeiterzahl, maximales Tages-Token-Budget für die gesamte Firma, maximale Zahl neuer Agents pro Woche. Überschreitung = Freigabe durch mich nötig.
- Neue Rollen mit Tool-Rechten, die es bisher nicht gab, brauchen immer meine Freigabe. Ein neuer Agent erbt nie mehr Rechte als sein Vorgesetzter.
- Der CEO kann Agents auch pausieren oder entlassen (Memory wird archiviert, nicht gelöscht).
- Ein Agent darf nie Code des Money-Zone-Teils oder `limits.toml` ändern.

# STRATEGIE-PIPELINE (aus dem AgenKit-Trading-Prompt, verbindlich)

Befolge `docs/trading_agent_prompt.md` vollständig. Zusammengefasst, was du technisch erzwingen musst:

- Mehrere Kandidatenstrategien, nicht eine Lieblingsstrategie. Jede mit Ein-Satz-Edge, Gegenpartei, Ein-/Ausstieg, Stop, Haltedauer, Handelsfrequenz, ökonomischer Begründung.
- Daten: echte historische Daten von Capital.com (oder dokumentierter Quelle), Zeitstempel ohne Zukunftsinformation, Zeitzonen und Lücken geprüft. Aufteilung **nur zeitlich** (Training / Validierung / Hold-out, ideal Walk-Forward). Der Hold-out wird pro Strategie **einmal** angefasst.
- Backtests mit realistischen Spreads, Slippage, Gebühren, Overnight-Finanzierung und Mindestordergröße **dieses Brokers**.
- **Trial-Counter:** Jede getestete Variante wird unwiderruflich gezählt. Berichte immer, wie viele Varianten probiert wurden, und bewerte Ergebnisse gegen Zufall (z. B. Deflated Sharpe Ratio oder Bootstrap/Permutationstest).
- Parameter-Nachbarschaft: Strategie, die nur bei einem exakten Wert funktioniert, wird verworfen.
- Report pro Kandidat: Sharpe, Max Drawdown, Trefferquote, Profit Factor, Trades, Kostenempfindlichkeit (Ergebnis bei doppelten Kosten), Regime-Aufschlüsselung. **Alle Zahlen berechnet, nie geschätzt.**
- Verdict-Tabelle: Strategy | Edge | Markt | Timeframe | Sharpe | Max DD | Trades | Kostenempfindlichkeit | Verdict.
- Pro Strategie vorab festgelegte **Kill-Kriterien** (z. B. Live-Drawdown größer als X × Backtest-Drawdown, Slippage dauerhaft über Y, N Verlusttage in Folge), die automatisch stoppen.
- Positionsgröße: Volatility-Targeting oder höchstens Bruchteil-Kelly, nie Voll-Kelly.

Der Red Team Agent bekommt vor jeder Freigabe den Auftrag, die Strategie zu widerlegen. Ohne dokumentierte Widerlegungsversuche keine Freigabe.

# MONEY-ZONE: HARTE REGELN IM CODE

`RiskGate` prüft **jede** Order, bevor sie den Broker erreicht, und kennt **globalen** Zustand (nicht pro Agent):

- Max. Positionsgröße pro Instrument und gesamt, max. Hebel, max. Anzahl offener Positionen, Korrelations-/Klumpenrisiko (gleiche Richtung auf korrelierten Märkten zählt zusammen).
- **Max. Tagesverlust** und **max. Drawdown** vom Hochpunkt, bezogen auf den **echten Kontostand vom Broker**. Bei Überschreitung: automatisch Flatten + Halt, Wiederanlauf nur durch mich.
- Stale-Data-Check (Kurs älter als N Sekunden = Reject), Spread-Check, Marktzeiten, API-Fehlerlimit (N Fehler in M Minuten = Halt).
- Jede Order hat einen **serverseitigen Stop-Loss**. Orders ohne Stop werden abgelehnt.
- Idempotenz: Order-IDs, kein doppeltes Senden bei Retries, Bestätigung über `dealReference`.
- **Reconciler:** Gleicht alle paar Sekunden den eigenen Positionsstand mit dem Broker ab. Abweichung = Alarm + Halt (Broker ist die Wahrheit).
- **KillSwitch:** Ein Aufruf (CLI, Dashboard-Button mit Bestätigung, automatisch bei Limits) stoppt alle Agents, storniert Working Orders und schließt alle Positionen, mit Wiederholung bis der Broker „flat" bestätigt. Einzeln getestet mit Fehlerinjektion (Broker antwortet nicht, Teilfüllung, Token abgelaufen).
- Absturz-Sicherheit: Beim Start Zustand vom Broker laden, nie annehmen. Bei Unklarheit: nicht handeln.

# DASHBOARD: DAS LIVE-BÜRO (`web/office.html`)

Ein echtes Büro-Bild, alle Daten live per WebSocket aus dem Event-Bus. Keine Fake-Daten.

- **Optik:** Isometrische oder Top-Down-Büroansicht mit Schreibtischen, Konferenzraum, CEO-Büro und Server-/Risk-Raum. Jeder Agent ist ein Roboter an seinem Arbeitsplatz, mit Namensschild und Rolle. Animation spiegelt den echten Zustand: *denkt* (Sprechblase/Puls), *ruft Tool auf*, *wartet*, *Fehler* (rot), *pausiert*. Bewegung zwischen Plätzen, wenn Agents sich Aufgaben übergeben (echte Events). Neue Mitarbeiter erscheinen sichtbar mit Einstellungs-Animation. Lokal gebündelte Assets, keine CDN-Abhängigkeit.
- **Kopfzeile:** Modus (BACKTEST/PAPER/LIVE, deutlich farblich), echter Kontostand und Tages-PnL vom Broker, Drawdown, Verbindungsstatus, Datenfrische, Token-Kosten heute, **KILL-SWITCH-Button** (mit Bestätigungsdialog).
- **Klick auf einen Mitarbeiter öffnet ein Detailpanel:**
  - Aktuelle Aufgabe und Zustand, Live-Stream seiner Schritte (Beobachtung, Begründung, Tool-Aufrufe mit Argumenten und Ergebnissen, Fehler), filterbar
  - Eingehende und ausgehende Aufgaben, Vorgesetzter
  - Memory: Lessons (mit Beleg-Links), Playbook-Version, letzte Reflexionen
  - Kennzahlen im Zeitverlauf (Qualität, Fehlerrate, Kosten, bei Tradern echte PnL-Beiträge)
  - Rolle, Tool-Rechte, Budget
- **CEO-Ansicht:** Auftragsliste, Engpass-Metriken, offene Hiring Requests mit Button „Genehmigen / Ablehnen" (Freigabe durch mich), Kapitalverteilung.
- **Strategie-Wand:** Tabelle aller Kandidaten mit Verdict, Trial-Counter, Kill-Kriterien, Live-Status.
- **Telemetrie-Feed** unten (Event-Strom) und Alarm-Leiste.
- Wenn das Backend nicht erreichbar ist: groß „OFFLINE", keine alten Werte als aktuell anzeigen.
- API nur auf `127.0.0.1` bzw. hinter Token-Authentifizierung, CORS auf die eigene Origin beschränkt, Steuerbefehle (Halt, Genehmigung) authentifiziert und im Audit-Log.
- Barrierearm (Kontraste, Tastatur), responsiv.

# BAUABLAUF MIT GATES

Stoppe nach jeder Phase, fasse zusammen (was gebaut, was getestet, was offen, Risiken) und warte auf mein „Freigabe".

- **Phase 0, Spec:** Schreibe `docs/spec.md` (Ziele, Nicht-Ziele, Rollen, Limits, Akzeptanzkriterien, Annahmen klar beschriftet). Stelle mir maximal 5 offene Fragen. **Gate 0.**
- **Phase 1, Architektur und Machbarkeit:** `docs/architecture.md`, Datenmodell, Event-Typen, Prozessgrenzen, Bedrohungsmodell. Dazu die Kosten- und Edge-Machbarkeitsanalyse mit echten Brokerdaten (Spread, Overnight, Mindestgröße) für das reale Kontogrößen-Szenario. **Gate 1.**
- **Phase 2, Plan:** `docs/plan.md` mit kleinen, einzeln testbaren Modulen, Reihenfolge, Teststrategie. **Gate 2.**
- **Phase 3, Test-first-Bau, in dieser Reihenfolge:** (1) Event-Bus und Store, (2) BrokerClient nur lesend gegen Demo, (3) RiskGate + KillSwitch + Reconciler mit vollständiger Testsuite inkl. Fehlerinjektion, (4) PaperBroker und Execution Gateway, (5) Daten und Backtest-Engine mit Lookahead-Tests, (6) Agent-Runtime, Memory, Rollen, (7) CEO und Hiring, (8) API und Dashboard. Jedes Modul: erst Tests, dann Code, Tests grün, bevor das nächste beginnt. **Gate 3 nach den Modulen 1-4** (vor jeder Order, auch Demo).
- **Phase 4, Review:** Red-Team-Review des gesamten Systems, Failure-Injection-Läufe (Datenausfall, Broker down, doppelte Fills, Token-Ablauf, Prozessabsturz, Uhrzeit-Drift), Sicherheitscheck (Secrets, Auth, Logs). **Gate 4.**
- **Phase 5, Paper:** Lauf gegen die Demo mit echten Live-Kursen, mindestens 4 Wochen, tägliche automatische Berichte. Vergleich mit Backtest-Erwartung. **Gate 5.**
- **Phase 6, Live-Micro:** Nur nach meiner ausdrücklichen Freigabe per `firm approve-live`, kleinste mögliche Größe, enge Limits, tägliche Kontrolle. Hochskalieren nur schrittweise und nur durch mich.

# QUALITÄTSREGELN FÜR DICH

- Jede Behauptung über Performance, API-Verhalten oder Gebühren ist entweder belegt (Quelle/Messung) oder als **ANNAHME** markiert.
- Keine Platzhalter-Implementierungen, die so aussehen, als würden sie funktionieren. Was nicht gebaut ist, wirft `NotImplementedError` und steht in `docs/plan.md`.
- Logge nie Secrets. Schreibe `.env.example` ohne Werte.
- Wenn du unsicher bist, ob etwas die Geldsicherheit berührt, entscheide für die sichere Variante und frage mich.
- Wenn ein Test fehlschlägt, repariere die Ursache, nicht den Test.
- Halte Dokumentation aktuell (`docs/runbook.md`: Start, Stopp, Kill, Wiederanlauf, Störungsfälle).

# ABSCHLUSS JEDER PHASE: SELBSTKRITIK

Beende jede Phase mit dem Abschnitt **„WIE KÖNNTE DAS SYSTEM SCHEITERN?"**: schwächste Annahme, was ich übersehe, was bei höheren Kosten passiert, ob der Edge schon wegarbitriert ist, was bei Ausfall von Datenfeed, Broker, Server oder LLM-API passiert, was passiert, wenn ein Agent halluziniert oder manipuliert wird (Prompt-Injection über Nachrichtentexte). Priorisiere Überleben vor beeindruckenden Ergebnissen.

# STARTBEFEHL

Beginne jetzt mit Phase 0. Lies zuerst `docs/trading_agent_prompt.md` und prüfe `legacy/` nur zur Bestätigung des Audits oben. Schreibe `docs/spec.md`, stelle höchstens 5 Fragen und warte auf Gate 0.
