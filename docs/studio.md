# Studio: die Firma verdient mit digitalen Produkten

Stand 2026-10-30. Kein Gewinnversprechen. Der häufigste Ausgang bei digitalen Produkten ist: wenige oder keine
Verkäufe, weil niemand das Produkt findet. Das Studio ist so gebaut, dass das wenig kostet und du es an echten
Zahlen siehst.

## Was automatisch läuft (ohne dich)

| Schritt | Wer | Was |
|---|---|---|
| Nische wählen | Code | aus `config/studio.toml`; sobald es Verkäufe gibt, mehr von dem, was sich verkauft (jede 3. Idee testet eine wenig genutzte Nische) |
| Idee | StudioResearcher (KI) | Titel, Zielgruppe, Problem, Gliederung, Preis (vom Code auf deinen Preisrahmen begrenzt, keine Doppel) |
| Produkt schreiben | StudioBuilder (KI) | vollständiges Produkt in Markdown (Checkliste, Planer, Kurz-Guide …) |
| Prüfung 1 | Code | Länge, Struktur, Preis, verbotene Versprechen („garantiert", „reich werden", Anlage-/Steuer-/Medizinrat), fremde Marken, Platzhalter, persönliche Daten, Links |
| Prüfung 2 | StudioAuditor (KI, Red Team) | Fehler, Füllstoff, Nutzen, irreführende Aussagen; Score ≥ 7/10 nötig. Ein Überarbeitungsversuch, dann Schluss |
| Shop-Text + Paket | StudioBuilder + Code | Titel, Beschreibung, Schlagworte; druckfertige HTML-Datei, Upload-Anleitung, KI-Hinweis |
| Verkäufe lesen | Studio-Zone (Code) | alle 30 min aus dem Shop (nur lesen), jeder Verkauf genau einmal, Rückerstattungen separat |
| Schatzkammer | Code | täglich: Umsatz, KI-Kosten, Gewinn, Rücklagen, Empfehlung |

Grenzen (Standard, änderbar nur von dir in `config/limits.toml`, siehe unten): höchstens 3 neue Produkte pro Woche,
Pause wenn 5 bei dir warten, höchstens 10 USD KI-Kosten pro Monat fürs Studio, Preis 3–49 USD.

## Was du tust (ca. 10 Minuten pro Produkt)

1. Handy-Meldung „Neues Produkt wartet auf Freigabe" → Dashboard → **STUDIO** → **PAKET** herunterladen und
   `product.html` lesen. Gut? **FREIGEBEN**. Nicht gut? **ABLEHNEN** (mit kurzem Grund).
2. Freigegebenes Produkt hochladen (Anleitung `UPLOAD.md` im Paket), dann **VEROEFFENTLICHT** klicken und die
   Produkt-ID aus dem Shop eintragen – damit die Firma Verkäufe zuordnen kann.
3. Kommt eine Schatzkammer-Empfehlung („X USD aufs Trading-Konto"): selbst überweisen und **UEBERWEISUNG
   EINTRAGEN** klicken (oder `firm treasury transferred X`).

Warum das Hochladen nicht automatisch passiert: Die Upload-Schnittstelle des Shops konnte ich nicht gegen eine
offizielle Doku prüfen, und ein Fehler dort (falscher Preis, falsche Datei, kaputtes Produkt öffentlich) kostet dich
Geld und Ruf. Ein Klick von dir pro Produkt ist die sichere Variante (CLAUDE.md: im Zweifel sicher).

## Einrichtung (einmalig, ca. 30 Minuten)

1. **Shop-Konto** auf deinen Namen eröffnen. Vorschlag: **Gumroad** (übernimmt Zahlungsabwicklung und laut mehreren
   Quellen seit 2025 als „Merchant of Record" auch die Umsatzsteuer der Käufer). Gebühr laut Drittquellen ca. 10 % +
   0.50 USD pro Verkauf, Mindest-Auszahlung 100 USD (seit März 2026) – **beim Anmelden auf gumroad.com prüfen**,
   ebenso Mindestalter und Auszahlung auf ein Schweizer Konto. Alternative mit niedrigerer Gebühr: Lemon Squeezy
   (ca. 5 % + 0.50 USD; Verkäufe lesen ist dort noch nicht angebunden).
2. **Zugangstoken nur zum Lesen** (Gumroad: Settings → Advanced → Applications → Anwendung anlegen → „Generate access
   token") und in `.env` eintragen: `GUMROAD_ACCESS_TOKEN=...`. Den Token sieht nur die Studio-Zone, nicht die
   Agenten, nicht das Dashboard.
3. **KI fürs Studio** (empfohlen): Ein lokales 8B-Modell (Ollama) schreibt brauchbare, aber eher mittelmässige
   Produkte – und die prüft dasselbe Modell. Besser: nur das Studio mit Claude Sonnet laufen lassen, die übrigen
   Agenten bleiben gratis auf Ollama:
   ```
   STUDIO_LLM_PROVIDER=anthropic
   STUDIO_MODEL=claude-sonnet-5-5
   ANTHROPIC_API_KEY=...        # von console.anthropic.com, dort auch ein Monatslimit (z. B. 10 USD) setzen
   ```
   Kosten (ANNAHME, Testwerte): pro Produkt ca. 4 Aufrufe, rund 25 000 Tokens → mit Sonnet etwa 0.10–0.30 USD.
   Das Studio stoppt selbst bei `max_monthly_ai_usd`.
4. **Optional, deine Grenzen** in `config/limits.toml` (von Hand, die Firma ändert diese Datei nie):
   ```toml
   [studio]
   enabled = true
   max_products_per_week = 3
   max_awaiting_approval = 5
   max_monthly_ai_usd = 10
   min_price_usd = 3
   max_price_usd = 49
   ```
5. **Steuern:** Einnahmen aus dem Verkauf sind auch als Hobby/Nebeneinkommen in der Steuererklärung anzugeben; ab
   einer gewissen Regelmässigkeit gilt es als selbständige Tätigkeit (AHV). Das klärst du mit dem Steueramt oder
   einer Fachperson. Die Schatzkammer legt deshalb 25 % als Steuer-Rücklage zur Seite (ANNAHME, in
   `config/treasury.toml` änderbar).

## Wie die Firma das Geld „schlau" nutzt (Schatzkammer)

Reihenfolge, festgelegt von dir in `config/treasury.toml`:
1. Steuer-Rücklage (25 % vom Netto-Umsatz)
2. Reserve für 3 Monate KI- und Plattformkosten
3. Vom freien Rest höchstens 50 % ins Trading – **nur wenn mindestens eine Strategie die Prüfung bestanden hat
   (PASS)**. Ohne geprüften Vorteil wäre Trading reines Glücksspiel; dann bleibt das Geld als Polster.
4. Decken die Einnahmen der letzten 30 Tage die KI-Kosten mehr als 3-fach, schlägt die Firma vor, das Studio-Budget
   massvoll zu erhöhen – erhöhen tust du.

Die Firma bewegt **nie** selbst Geld: keine Auszahlungen, keine Überweisungen, kein Zugriff auf Bank oder Shop-
Auszahlungen (CLAUDE.md, Prinzip 5). Sie rechnet und empfiehlt.

## Befehle

| Was | Wie |
|---|---|
| Produkte ansehen | Dashboard → STUDIO, oder `firm studio list` |
| Freigeben / ablehnen | Buttons, oder `firm studio approve <ID>` / `firm studio reject <ID>` |
| Als veröffentlicht melden | Button, oder `firm studio published <ID> <Shop-Produkt-ID>` |
| Zurückziehen | Button, oder `firm studio retire <ID>` |
| Schatzkammer | Dashboard → SCHATZKAMMER, oder `firm treasury status` |
| Überweisung eintragen | Button, oder `firm treasury transferred <USD>` |
| Studio abschalten | `.env`: `FIRM_STUDIO=0` (oder `[studio] enabled = false`) |

## WIE KÖNNTE DAS SCHEITERN?

- **Niemand kauft.** Gumroad bringt kaum eigene Besucher. Ohne Reichweite (z. B. ein Kanal, den du ohnehin nutzt)
  bleiben die Verkäufe bei null. Das Studio kann keine Werbung machen und tut es bewusst nicht (Spam-Risiko).
- **Qualität:** KI-Produkte können Fehler enthalten. Zwei Prüfungen + deine Freigabe senken das Risiko, schliessen
  es nicht aus. Lies jedes Produkt, bevor du freigibst.
- **Kosten > Umsatz:** Mit Ollama kostet das Studio nichts. Mit Claude ist es auf 10 USD/Monat begrenzt.
- **Plattformregeln** zu KI-Inhalten können sich ändern; der KI-Hinweis steht deshalb in jedem Produkt.
- **Antwortformat der Shop-API** ist als ANNAHME aus der Gumroad-Doku übernommen; passt es nicht, verbucht das
  Studio nichts und meldet STUDIO_ERROR – es rät keine Zahlen.
