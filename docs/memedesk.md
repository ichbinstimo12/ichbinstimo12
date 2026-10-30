# Memecoin-Desk (Solana) – Erweiterung der Trading-Firma

Ein eigenständiger Desk im Stil erfahrener Solana-Memecoin-Trader (z. B. Orangie): Aufmerksamkeit und Narrativ
zuerst, schnelle Ausführung, kleine Positionen, Gewinne früh sichern, Verluste sofort schneiden.
**Standard ist PAPER**: Alles läuft mit echten Live-Daten, gekauft und verkauft wird nur simuliert (mit echten
Jupiter-Quotes, Slippage und Gebühren). Kein Gewinnversprechen – Memecoins sind extrem riskant, die meisten gehen
auf null.

## Die sieben Agenten

| Agent | Aufgabe | Daten |
|---|---|---|
| Scout | neue/trendende Tokens, Grobfilter (DEX, Liquidität, Alter, Volumen) | DexScreener (Profile, Boosts), GeckoTerminal (trending/neue Pools) |
| Narrativ-Analyst | Aufmerksamkeit + Frische: Boosts, Socials, Alter, Kopie-Wellen („durchgekaut"), optional X | DexScreener, optional X-API |
| Wallet-Tracker | Smart-Money/KOL-Wallets: frühe Käufe (Signal), Verkäufe (Sofort-Exit) | Solana-RPC (Helius empfohlen), Liste in `config/memedesk_wallets.toml` |
| Safety-Checker (**Veto**) | Mint-/Freeze-Authority, LP < 90 % gelockt, Holder > 15 %, Top-10 > 35 %, Insider-/Bundle-Cluster, Honeypot (Kauf→Verkauf-Quote), zu wenig Liquidität | RugCheck, Jupiter |
| Risk-Manager (**Veto**) | Einsatz 1–2 % des Desk-Kapitals, max. 5 Positionen, −5 % Tag → Pause, −20 % vom Hoch → Stopp | `config/memedesk_limits.toml` |
| Executor | PAPER-Fills mit echter Jupiter-Route, dynamischer Slippage, Priority-Fee | Jupiter |
| Journal & Coach | jeder Trade mit Grund und Daten-Schnappschuss; wöchentliche Auswertung, begrenzte Anpassung | `archiv/memedesk/journal.jsonl` |

Jede Entscheidung – auch jedes Veto mit Grund – steht im Ereignisprotokoll (`MEME_*`) und im App-Tab.

## Handelslogik

- **Einstieg** nur bei mindestens **3 von 5** Signalen: frisches Narrativ · steigendes Volumen mit mehr Käufern/Holdern ·
  Smart-Money-Käufe · saubere Holder-Verteilung · gesunde Chartstruktur (Ausbruch nach Konsolidierung oder gehaltener
  Pullback). Safety- und Risk-Veto gehen immer vor.
- **Ohne Geldfluss kein Kauf** (`require_flow = true`): Mindestens eines von „steigendes Volumen mit mehr Käufern"
  oder „Smart Money" muss erfüllt sein. Aufmerksamkeit allein (Boosts, Socials) reicht nicht – genau solche Käufe
  haben in den ersten PAPER-Läufen Geld gekostet. Bezahlte Boosts und Socials zählen im Narrativ-Score kaum,
  organische Aufmerksamkeit (GeckoTerminal-Trending, echte Käufe pro Stunde, X, Nachrichten) stark.
  Ohne eingetragene Wallets gilt Smart Money als „nicht messbar" (nicht als „nein").
- **Nie hinterherrennen:** > 25 % in 5 Minuten ohne Konsolidierung = kein Einstieg, warten auf Pullback/Ausbruch.
- **Exits:** bei +100 % den Einsatz rausnehmen (Free Ride) → Stufen +200 %/+400 % je 25 % → Trailing-Stop 30 % auf
  den Rest. Harter Stop −30 %. Zeit-Stop nach 45 min ohne Bewegung (±10 %). Sofort raus bei Narrativ-/Liquiditäts-
  Bruch, Dev-Verkauf, Smart-Money-Exit oder neuem Sicherheitsproblem.
- Offene Positionen werden alle 5 s bewertet (Sammel-Preisabruf, Ausstiegskosten eingerechnet); verkauft wird mit
  einer echten Quote für genau diese Menge. Scout und Positionsverwaltung laufen parallel.

Alle Schwellen stehen in `config/memedesk.toml`, die harten Risikoregeln in `config/memedesk_limits.toml`.

## Einrichtung

1. Nichts nötig für PAPER: DexScreener, RugCheck, Jupiter (lite), GeckoTerminal und der öffentliche Solana-RPC sind
   kostenlos. Der Desk startet automatisch mit der Firma (`FIRM_MEMEDESK=1`, abschalten mit `0`).
2. Empfohlen: **Helius**-RPC (kostenloses Konto auf helius.dev) für den Wallet-Tracker:
   `.env`: `SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=DEIN_KEY`
3. Smart-Money-Wallets in `config/memedesk_wallets.toml` eintragen (öffentliche Adressen, keine Schlüssel).
4. Optional X/Twitter-Erwähnungen: `.env`: `X_BEARER_TOKEN=...` (kostenpflichtige X-API).
5. GMGN hat keine öffentliche API und wird nicht genutzt; Birdeye ist vorbereitet, aber noch nicht angebunden.

Die Schlüssel sieht nur der Prozess des Desks (`meme`-Zone) – nicht die Agenten, nicht das Dashboard, nicht der
Handelsprozess der Firma. Der Desk kennt umgekehrt keine Capital.com- oder LLM-Schlüssel.

## Bedienung

| Was | Wie |
|---|---|
| Ansehen | Dashboard → Tab **MEMECOIN DESK** (Positionen, P/L heute/Woche/gesamt, Watchlist mit Scores, Entscheidungen inkl. Vetos, Coach) |
| Notaus | Button **DESK KILL SWITCH** oder `firm meme kill` (verkauft alle Positionen, Desk stoppt; die übrige Firma läuft weiter) |
| Wiederanlauf | `firm meme resume` (auch nach dem −20 %-Stopp) |
| Status | `firm meme status` |
| Coach manuell | `firm meme coach` (sonst: Auswertung mit genug Daten) |
| LIVE anfordern | `firm meme live` oder Button – prüft die Kriterien |

Handy-Meldungen (über die bestehende ntfy/Telegram-Zone): Käufe, Verkäufe, Limits, Fehler, Coach-Änderungen.

## Selbstverbesserung (Coach)

Der Coach ändert **nur** Gewichte und Schwellen der Signale und **nur** innerhalb `[bounds]`. Grundlage ist ein
Backtest auf dem eigenen Journal: Die beim Einstieg gespeicherten Kennzahlen werden mit neuen Schwellen neu bewertet.
Voraussetzung: mindestens 30 abgeschlossene Trades, mindestens die Hälfte bleibt erhalten, mindestens +2 Prozentpunkte
Verbesserung. Jede Änderung wird versioniert (`data/memedesk/params_vN.json`) und im App-Tab angezeigt.
Risikoregeln kann der Coach nicht ändern (er liest die Datei nicht einmal).

## LIVE-Modus (noch gesperrt)

Laut Spezifikation erst nach **≥ 4 Wochen PAPER mit ≥ 100 Trades und positivem Ergebnis** (wird im Code geprüft).
Der LIVE-Executor ist in diesem Stand **bewusst nicht gebaut** (`NotImplementedError`): Er braucht ein separates
Wallet, einen verschlüsselt gespeicherten Schlüssel nur im Executor-Prozess, signierte Jupiter-Swaps und eine
App-Bestätigung für jeden Kauf über `confirm_above_sol`. Das wird ergänzt, wenn die PAPER-Kriterien erfüllt sind –
vorher gäbe es keine belastbare Grundlage dafür.

## Testlauf

`python scripts/memedesk_paper_run.py --minutes 10` läuft isoliert gegen die echten Quellen und schreibt ein
Protokoll. Ein echter Beispiel-Lauf liegt in `docs/memedesk_example_log.md`.

## Annahmen und Grenzen

- Swap-Gebühr 0.25 % je Seite und Priority-Fee 0.0005 SOL sind **ANNAHMEN** (je nach Pool bis 1 %).
- PAPER-Fills nehmen die halbe Slippage-Toleranz als Verlust an – eher zu pessimistisch.
- Holder-Wachstum wird aus wiederholten RugCheck-Abfragen gemessen; beim ersten Blick dient das Käufer/Verkäufer-
  Verhältnis als Ersatz.
- Kostenlose APIs haben Rate-Limits; der Desk drosselt sich selbst und pausiert eine Quelle bei „429".
- Ein Narrativ-Score aus Boosts/Socials misst Aufmerksamkeit nur grob; ohne X-API fehlt die wichtigste Quelle.
