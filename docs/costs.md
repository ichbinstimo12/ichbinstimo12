# Was kostet die KI der Firma?

Stand 2026-10-30. Preise: Anthropic-API-Listenpreise (USD je 1 Mio. Tokens), Stand 2026-10-06 laut offizieller
Modellübersicht. Vor dem Umstieg auf console.anthropic.com → Pricing gegenprüfen.

| Modell | Eingabe | Ausgabe | Cache-Lesen |
|---|---|---|---|
| Claude Opus 5.5 (`claude-opus-5-5`, Standard im Code) | 4.00 | 20.00 | 0.20 |
| Claude Sonnet 5.5 (`claude-sonnet-5-5`) | 2.00 | 10.00 | 0.20 |
| Claude Haiku 5.5 (`claude-haiku-5-5`) | 0.10 | 0.50 | — |
| Ollama lokal (`llama3.1`) | 0 | 0 | — (nur Strom, eigener PC/VPS) |

## Wie viele Tokens verbraucht die Firma?

Die Obergrenzen stehen im Code und werden **vor jedem Agenten-Schritt geprüft** (`AgentRuntime.step`):

- je Agent `daily_token_budget` in `config/roles/*.yaml`: 11 Agenten, zusammen **720 000 Tokens/Tag**
- für die ganze Firma `max_token_budget_daily = 1 000 000` in `config/limits.toml`

Bedarf ohne Grenze (ANNAHME, nicht gemessen): 11 Agenten × Routine alle 30 min = 528 Schritte/Tag plus
ereignisgesteuerte Aufgaben, je Schritt ca. 3 000–5 000 Tokens → 1.5–2.6 Mio./Tag. Die Budgets greifen also; der
reale Verbrauch liegt **an der Obergrenze**. Gemessene Werte zeigt das Dashboard (Kopfzeile „Tokens heute") und
`GET /api/agents`.

## Kosten pro Tag / 30 Tage (ANNAHME: 80 % Eingabe, 20 % Ausgabe, ohne Cache)

| Modell | 720 000 Tokens/Tag | 1 000 000 Tokens/Tag |
|---|---|---|
| Opus 5.5 | 5.18 $/Tag · **≈ 155 $/Monat** | 7.20 $/Tag · ≈ 216 $/Monat |
| Sonnet 5.5 | 2.59 $/Tag · **≈ 78 $/Monat** | 3.60 $/Tag · ≈ 108 $/Monat |
| Haiku 5.5 | 0.13 $/Tag · **≈ 4 $/Monat** | 0.18 $/Tag · ≈ 5 $/Monat |
| Ollama | 0 $ | 0 $ |

Rechnung Opus, 720 000: 576 000 × 4 $/Mio. + 144 000 × 20 $/Mio. = 2.30 $ + 2.88 $ = 5.18 $.
Denken (Thinking) zählt als Ausgabe und steckt im Budget; `ANTHROPIC_EFFORT=low` hält es klein.

Weniger ausgeben: Budgets in `config/roles/*.yaml` senken, `FIRM_AGENT_ROUTINE_MIN` erhöhen (z. B. 60 statt 30),
für Routine-Rollen `ANTHROPIC_MODEL=claude-sonnet-5-5` setzen. Die Kosten der KI sind für die Firma ein
Fixkosten-Block: bei einem 200-CHF-Konto frisst Opus (≈ 155 $/Monat) jeden denkbaren Handelsgewinn. **Empfehlung:
Ollama (gratis), bis ein Teil der Firma nachweislich Geld verdient.**

## Kann die Firma über mein Claude-Abo (Pro/Max) laufen?

**Nein.** Laut Anthropic (code.claude.com/docs/en/legal-and-compliance, „Authentication and credential use"):
Die Anmeldung mit dem Claude-Konto (OAuth) ist für die normale Nutzung von Claude Code und den Anthropic-Apps
gedacht; wer eigene Programme baut, „should use API key authentication through Claude Console". Die
Abo-Limits gelten außerdem für „ordinary, individual usage". Eine Firma aus Python-Agenten, die rund um die Uhr
läuft, ist weder das eine noch das andere.

- **Abo (Pro/Max):** gut, um mit Claude Code an diesem Ordner weiterzubauen (wie in dieser Sitzung).
- **API-Schlüssel (console.anthropic.com):** nötig, wenn die Agenten der Firma Claude nutzen sollen
  (`LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=...`). Abgerechnet nach Tokens wie oben, getrennt vom Abo.
  Im Konsolen-Konto ein monatliches Ausgabenlimit setzen.
