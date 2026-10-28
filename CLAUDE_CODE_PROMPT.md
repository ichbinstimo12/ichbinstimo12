# Auftrag für Claude Code: Upgrade-Paket integrieren und die Firma auf Evidenz umstellen

Lies zuerst `docs/IMPROVEMENT_REPORT.md`. Die Diagnose dort stammt aus echten Daten. Arbeite in Phasen, halte nach jeder an,
berichte (gebaut / getestet / offen / Risiken) und warte auf meine Freigabe. Erfinde keine Zahlen. Keine Gewinnversprechen.

## Teil A: Einspielen und absichern (Phase 1)
1. Entpacke das Paket ins Projekt. Kopiere `patched/src/firm/money/trading_desk.py`, `patched/src/firm/broker/client.py` und
   `patched/src/firm/main.py` über die Originale (vorher `git diff` ansehen, nichts anderes verändern).
2. `pytest` komplett laufen lassen (inkl. neuer Tests unter `tests/`: test_lab, test_pack, test_desk_integration). Behebe alles,
   was durch den Patch bricht. Bestehende Tests, die das alte 15-Minuten-/EMA-Verhalten festschreiben, an das neue Verhalten
   anpassen, nicht löschen, und jede Änderung begründen.
3. Prüfe `get_market_details` gegen die echte Demo-API: Kommt `instrument.currency` an? Wenn nicht, den Pfad korrigieren.
   Ohne Währung handelt der Desk bewusst nicht.
4. `scripts/research_run.py` einmal mit **einem** Markt, 30 Tage, ausführen und mir Qualitätsbericht und Dateien zeigen.
5. Realisiertes P&L: Schließungen mit echtem Ergebnis aus `/history/transactions` (Capital.com) ins Ledger und in
   `POSITION_CLOSED` schreiben (profit, profit_currency, Schlusskurs). Der Rekonstruktions-Fallback in `journal.py` bleibt.

## Teil B: Die Strategie-Pipeline vervollständigen (Phase 2)
1. Forschungslauf für die ganze Whitelist (365 Tage, `HOUR`). Zusätzlich `HOUR_4` und `DAY`, wo genug Historie existiert.
2. Zeige mir den Bericht: pro Markt und Strategie Out-of-Sample-Trades, Erwartungswert in R, Profit Factor, Drawdown,
   Ergebnis bei doppelten Kosten, Nachbar-Parameter, Gesamtzahl der Versuche. **Keine Parameter nachträglich anpassen, bis
   etwas besteht.** Jeder Nachtest erhöht den Trial-Counter.
3. Overnight-Finanzierung und echte Spreads je Tageszeit in `lab.backtest` einbauen (heute nur Pauschalannahme
   `spread_default_pct` und `overnight_r_per_bar=0`). Marktzeiten pro Instrument beachten.
4. Den Schutz im Live-Pfad verdrahten: `StrategyHealth.check` aus den echten Trade-R-Werten je Strategie nach jedem
   Abschluss. Bei `STOP` den Markt aus der Whitelist-Wirkung nehmen (nur sperren, nie freischalten), Event
   `STRATEGY_STOPPED`, Dashboard-Hinweis.
5. Stilles Weiterhandeln bei `FORWARD_TEST` nie live: im Code erzwungen und per Test belegt (liegt im Tor, bitte Test ergänzen,
   dass `mode=LIVE*` + FORWARD_TEST = kein Handel).

## Teil C: Die Agenten nützlich machen oder abschalten (Phase 3)
Befund: Aufgaben alle 60 s, generischer Text, Behauptungen, die den Fakten widersprechen, keine Wirkung auf den Handel.
1. Aufgabenfrequenz drastisch senken (ereignisgesteuert: nach Handelsabschluss, nach Forschungslauf, täglicher Bericht).
2. Jede Agentenantwort muss auf **übergebenen Fakten** beruhen: Kontostand, offene Positionen, `TRADE_RESULT`-Events,
   Forschungsbericht, Journal-Kennzahlen als strukturierter Kontext. Prompt-Regel: „Nenne nur Zahlen aus dem Kontext. Fehlt
   etwas, schreibe UNBEKANNT."
3. Automatische Faktenprüfung: Behauptungen wie „keine offene Position" gegen den Broker-Stand prüfen, bei Widerspruch die
   Antwort verwerfen und als `AGENT_INVALID` loggen.
4. Lektionen nur speichern, wenn sie (a) auf konkrete Event-IDs zeigen, (b) eine **überprüfbare Aussage** enthalten
   (z. B. „SELL-Signale bei ATR < X verlieren"), (c) nicht fast-identisch zu einer bestehenden sind. Deduplizieren, Konfidenz
   nur durch spätere Bestätigung erhöhen. Generische „I should have…"-Sätze verwerfen.
5. Agenten dürfen **nur vorschlagen** (Parameter, neue Hypothesen). Übernommen wird ein Vorschlag nur, wenn er den
   Forschungslauf (Walk-Forward, Trial-Counter) besteht UND ich ihn freigebe. Kein automatisches Parameter-Tuning auf Live-Ergebnissen.
6. Das Modell ist mit llama3.1:8b zu schwach für Tool-Aufrufe und Analyse. Teste ein stärkeres Modell oder reduziere die
   Agenten auf Berichte; messe, ob die Faktenprüfung (Punkt 3) besteht.

## Teil D: Dashboard (Phase 4)
Zeige echte Größen: Equity-Kurve, Journal-Kennzahlen mit Aussagekraft-Hinweis („n = 5, kein Urteil möglich"), Strategie-Tor-Status
je Markt (PASS / FORWARD_TEST / FAIL / ohne Urteil), Trial-Counter, Kill-Kriterien-Status, Datenfrische. Keine Fake-Werte.

## Regeln
- Hart: Limits, Kill-Switch, RiskGate, Modus-/Live-Token bleiben unverändert und werden nicht gelockert.
- Nichts Live ohne `approve-live` von mir. FORWARD_TEST und alles ohne PASS nur Demo.
- Jede Zahl im Bericht stammt aus Daten oder Berechnung. Annahmen sind als ANNAHME beschriftet.
- Beende jede Phase mit „WIE KÖNNTE DAS SYSTEM SCHEITERN?".
