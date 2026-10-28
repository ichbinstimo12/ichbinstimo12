# Analyse und Upgrade: warum die Firma Verlust macht und was jetzt anders ist

Stand der Auswertung: deine `tests.zip` (Demo-Konto, 24.10.2026, 13:40 bis 17:08 UTC). Alle Zahlen unten stammen aus deiner
`firm_events.db`, nicht aus Schätzungen. Auswertbar mit `python scripts/analyze_trades.py firm_events.db`.

## 1. Was wirklich passiert ist

| | |
|---|---|
| Konto | 1000.00 → 995.17 CHF in 3,5 Stunden (−0,48 %), max. Drawdown 1,4 % |
| Trades | 5, alle **BTCUSD SELL** am **Samstag** (FX, Gold, Aktien waren geschlossen) |
| Ergebnis je Trade | −4,16 / −4,09 / **+8,21** / −4,11 / −0,68 (letzter beim letzten Snapshot noch offen) |
| In R (Stop-Abstand = 1 R, in USD gerechnet) | −1 / −1 / **+2** / −1 |
| Gewinnquote | 1 von 5. 95-%-Intervall: **0 % bis 55 %** |

**Das ist statistisch kein Beweis für irgendetwas.** Mit 5 Trades kann dieselbe Strategie profitabel oder wertlos sein.
Ein Urteil braucht ca. 100 abgeschlossene Trades. Die Ergebnisse passen aber exakt zu dem, was man bei einem Verfahren ohne
Edge erwartet: Bei Stop 1,5 ATR und Ziel 3 ATR gewinnt ein Zufallskurs rund ein Drittel der Trades, das ist **vor Kosten ±0**
und nach Kosten negativ.

## 2. Kritische Befunde (nach Gewicht)

1. **Die Strategie wurde nie geprüft.** EMA12/EMA48 auf 15-Minuten-Kerzen mit festem 2:1-Ziel ist ein Standardverfahren
   ohne Nachweis. Es gibt weder Backtest noch Walk-Forward noch Kostenrechnung dafür. Du hast eine Strategie live (Demo)
   laufen lassen, bevor sie ein einziges Mal gegen Daten bestanden hat. Das widerspricht deiner eigenen Spec.
2. **Einstieg zu spät und im Dauerzustand.** Das Signal ist ein *Zustand* (EMA-Lücke ≥ 0,25 ATR), kein Ereignis. Jeder Einstieg
   kam bei einer Lücke von 1,4 bis 1,5 ATR, also nachdem die Bewegung schon gelaufen war. Nach jedem Stop stieg der Desk
   15 Minuten später **erneut in dieselbe Richtung** ein (4× SELL hintereinander). Das ist Hinterherlaufen plus Rache-Handel.
3. **Rechnet mit der unfertigen Kerze.** `get_candles` liefert die laufende Kerze mit. EMA, ATR und Signal ändern sich dadurch
   alle 3 Minuten (Desk-Takt) mitten in der Kerze. Der ATR sprang in deinen Trades von 123 auf 342.
4. **Währungsfehler beim Sizing.** Risiko = Stop-Abstand × Größe wird in USD gerechnet, das Budget (0,5 %) aber in CHF. Echtes
   Risiko pro Trade war konstant **0,83 R** (Verluste −0,82 bis −0,83 R, Gewinn +1,66 R statt +2 R). Bei Instrumenten in
   stärkerer Währung als dem Konto (z. B. GBP) wäre es *zu hoch*. Das Risikolimit wird so nicht eingehalten.
5. **Es wird kein Gewinn/Verlust gemessen.** Im Ledger steht überall `profit: null`, Schließungen heißen „closed at broker".
   Ohne gemessenes Ergebnis kann niemand lernen und kein Kill-Kriterium greifen.
6. **Die Agenten verändern nichts am Handel, und ihr Output ist Füllstoff.** Alle 30 bis 60 Sekunden entstehen Aufgaben
   (151 in 3,5 Stunden, über 100 Modellaufrufe). Das Modell (llama3.1:8b) schreibt Sätze wie „Still no exposure observed"
   *während eine Position offen ist*, erfindet Annahmen („mean reversion in daily range", „Bollinger Bands"), die mit der
   tatsächlichen EMA-Strategie nichts zu tun haben, und die „Lektionen" sind generisch („I should have investigated…").
   Nichts davon fließt in Parameter oder Entscheidungen. Das ist Rechenzeit ohne Wirkung und erzeugt den *Eindruck* von Lernen.
7. **Nicht repräsentativer Testzeitraum.** Samstag, nur Krypto, dünner Markt. Dass nur BTCUSD gehandelt hat, liegt an den
   Marktzeiten, nicht an einer Auswahl.
8. **Kosten unsichtbar.** In der Demo werden Stops/Ziele exakt gefüllt. Live ist das nicht so (Slippage, Spread-Ausweitung,
   Gaps). Die Demo-Zahlen unterschätzen Kosten systematisch.

## 3. Was dieses Paket ändert

| Baustein | Datei | Wirkung |
|---|---|---|
| Forschungslabor | `src/firm/research/lab.py` | 4 Kandidaten-Strategien (inkl. deiner alten als Baseline), Backtest mit Spread, Slippage, Gaps, Stop-zuerst; Walk-Forward (5 Folds, Parameter nur auf Trainingsdaten gewählt); Bootstrap-Konfidenzintervall; Trial-Counter mit steigender Signifikanz-Hürde; Urteil PASS / FORWARD_TEST / FAIL / INSUFFICIENT_DATA |
| Historie | `src/firm/research/history.py` | seitenweise Kursdaten (inkl. Spread), CSV-Cache, Datenqualitätsprüfung |
| Forschungslauf | `scripts/research_run.py` | lädt Daten, prüft alle Märkte, schreibt `data/strategy_verdicts.json` und `data/research_report.md` |
| Strategie-Tor | `src/firm/money/strategy_gate.py` | Handel nur mit Urteil. FAIL oder unbekannt = kein Handel. FORWARD_TEST nur Demo mit halbem Risiko |
| Live-Signal v2 | `src/firm/money/signal_v2.py` | derselbe Code wie im Labor (keine Abweichung Backtest/Live), nur abgeschlossene Kerzen, eine Auswertung je Kerze, Kanten-Trigger, Spread-Filter |
| FX-Sizing | `src/firm/money/sizing.py` | echtes Risiko in Kontowährung, Test: Risiko = Budget |
| Journal | `src/firm/money/journal.py` | rekonstruiert reales P&L je Trade aus Broker-Kontoständen, Kennzahlen, automatische Kill-Kriterien, Schutzregeln (kein Re-Entry nach Stop, Tageslimit, Pause nach Verlustserie) |
| Auswertung | `scripts/analyze_trades.py` | die Tabelle oben, für jede Datenbank |
| Desk-Patch | `patched/src/...` | `trading_desk.py`, `client.py`, `main.py` mit all dem verdrahtet (1-Stunden-Kerzen, Tor, FX, Regeln) |

**Getestet** (24 Tests, alle grün, ohne Netzwerk): kein Look-ahead (Zukunft verändern ändert keine Signale), Backtest-Präfix stabil,
Kosten wirken monoton, Stop-vor-Ziel und Gap-Verlust, **Zufallskurse bestehen nie** (6 Serien × 4 Strategien), ein künstlich
eingebauter Trend wird erkannt, Trial-Counter, Sizing mit FX, nur geschlossene Kerzen, Spread-Sperre, P&L-Rekonstruktion,
Kill-Kriterien, Re-Entry-Sperre, Tor, Historienseiten, Desk-Rauchtest mit Attrappen.

## 4. Was ich NICHT weiß und nicht versprechen kann

- **Ob irgendeine dieser Strategien auf echten Daten profitabel ist, weiß niemand.** Ich konnte hier keine echten Kurse laden
  (kein Netz). Ob ein Edge existiert, entscheidet erst `research_run.py` auf deinen echten Kursdaten. Rechne damit, dass bei einem
  Teil der Märkte `FAIL` herauskommt. Das ist ein *gutes* Ergebnis, weil du dann dort nicht handelst. Privatkunden verlieren bei
  CFDs mehrheitlich Geld, das steht nicht ohne Grund in den Risikohinweisen der Broker.
- Die Tests des Desk-Patches liefen mit Attrappen für Gateway, RiskGate und Broker. **Deine eigene Testsuite (httpx, pydantic,
  pytest-asyncio) konnte ich hier nicht ausführen.** Lass sie nach dem Einspielen laufen (siehe `CLAUDE_CODE_PROMPT.md`).
- Capital.com-Feldnamen (z. B. `instrument.currency`) und der Seitenabruf der Historie (`from`/`to`) sind aus der Doku
  bekannt, aber nicht gegen die echte API geprüft. Erster Lauf: ein Markt, 30 Tage, Ergebnis ansehen.
- Die Journal-P&L-Zuordnung ist exakt, solange sich Trades nicht überlappen (`exact`-Feld). Bei mehreren gleichzeitigen
  Positionen ist sie eine Näherung. Besser wäre die Capital-Transaktionshistorie (`/history/transactions`), siehe Prompt.

## 5. So gehst du vor

1. ZIP in den Projektordner entpacken. `src/...` und `scripts/...` landen neben deinem Code. Die drei Dateien unter `patched/`
   über deine Originale kopieren (`trading_desk.py`, `client.py`, `main.py`).
2. In deiner `.env` darf **kein** `FIRM_TRADE_RESOLUTION=MINUTE_15` stehen (Standard ist jetzt `HOUR`).
3. `python scripts/research_run.py BTCUSD EURUSD GOLD --days 365 --resolution HOUR` und `data/research_report.md` lesen.
4. Desk starten. Ohne PASS/FORWARD_TEST handelt er nicht, das ist Absicht.
5. Nach jeweils ~2 Wochen: `python scripts/analyze_trades.py`. Entscheidung erst ab ~100 Trades. Vorher gilt nur: Kill-Kriterien
   (6 Verluste in Folge, Live-Erwartungswert ≤ −0,30 R nach 20 Trades, Live-Drawdown ≥ 10 R) beenden die Strategie automatisch.
6. Agenten: erst wieder einschalten, wenn sie echte Aufgaben mit messbarem Ergebnis haben (siehe Prompt, Teil C). Bis dahin
   `FIRM_DELEGATION_SECONDS` hoch setzen oder den Loop abschalten: Er kostet nur Rechenzeit.
