# Spec: Neue Einnahmequellen der Firma (Phase 0 – wartet auf Gate 0)

Stand 2026-10-30. Status: **Entwurf. Noch nichts davon gebaut.** Gebaut wird erst nach deiner Freigabe und
deinen Antworten auf die Fragen unten (CLAUDE.md: Spec → Architektur → Plan → Test-first-Bau, Stopp an jedem Gate).

## Ausgangslage (ehrlich)

- **Trading:** Das Konto ist klein (~200 CHF). Die Machbarkeitsanalyse aus Phase 1 zeigt, dass Spreads,
  Finanzierung und Mindestgrößen einen großen Teil jedes Edges auffressen. Bisher hat keine Strategie einen
  belastbaren Nachweis (PASS) mit Live-Ergebnissen. Gehandelt wird ab jetzt **nur noch mit bestandener Prüfung**
  (`FIRM_DEMO_EXPLORE=0` ist der neue Standard). Das Ergebnis darf auch lauten: „Wir handeln nicht."
- **Memecoin-Desk:** entfernt (deine Entscheidung).
- **KI-Kosten:** Siehe `docs/costs.md`. Mit Claude Opus ≈ 155 $/Monat Fixkosten – mehr als das Handelskonto
  wert ist. Jede neue Einnahmequelle muss zuerst diese Kosten schlagen, sonst ist Ollama Pflicht.
- **„Passives Einkommen" gibt es so nicht.** Jede der Optionen unten braucht dich: als rechtlicher Inhaber der
  Konten (Zahlungen, Steuern, Haftung), als Freigabe-Instanz für alles, was Kunden sehen, und am Anfang für
  Kundenkontakt. Die Agenten können Arbeit vorbereiten und automatisieren, aber nicht die Verantwortung übernehmen.
  Kein Gewinnversprechen.

## Was funktioniert nachweislich – und was nicht (Bewertung)

| Option | Was Agenten tun | Was du tun musst | Startkosten | Realistisch? |
|---|---|---|---|---|
| **A. Digitale Produkte** (Vorlagen, Excel/Notion-Tools, Lern-Guides) über einen Shop mit Zahlungsabwicklung (z. B. Gumroad, Lemon Squeezy, Shopify) | Marktrecherche, Entwürfe, Produktbeschreibung, Varianten, Auswertung der Verkäufe | Konto eröffnen, jedes Produkt freigeben, Qualität prüfen | gering | **Ja, am ehesten.** Aber: der Markt ist voll von KI-Produkten; ohne Reichweite (Social, SEO) verkauft sich wenig. |
| **B. Dienstleistung für KMU** (z. B. Texte, Übersetzungen DE/EN/FR, Social-Media-Posts, einfache Websites) | Entwürfe in Minuten, Kunden-Briefing strukturieren | Kunden finden, Angebot, Vertrag, Endkontrolle, Haftung | gering | **Ja, bringt am schnellsten echtes Geld**, ist aber nicht passiv. |
| **C. Print-on-Demand-Shop** | Designs, Beschreibungen, Trendanalyse | Konto, Freigabe, meist Werbebudget | mittel (Werbung) | Mittel. Geringe Margen, Markenrecht-Risiko bei Designs. |
| **D. Inhalte + Affiliate** (Blog/Newsletter) | Artikel, Recherche | Freigabe, Geduld | gering | Langsam (Monate). Suchmaschinen werten Massen-KI-Texte ab. |
| **E. Eigene kleine Software/App (Micro-SaaS)** | Code, Tests, Doku | Idee, Support, Verantwortung | mittel | Höchstes Potenzial, höchster Aufwand. |

**Nicht** gemacht wird (unabhängig von der Option): gefälschte Bewertungen, Spam, Kopieren fremder Produkte oder
Marken, sich als Mensch oder als andere Firma ausgeben, Versprechen, die das Produkt nicht hält. Das ist nicht nur
unfair, es führt zu Kontosperren und Haftung bei dir.

## Vorschlag (meine Empfehlung)

**Start mit A (digitale Produkte) in einem „Studio"-Teil der Firma, mit harten Grenzen wie beim Trading:**

1. Neue Rollen (nur Konfiguration in `config/roles/`): *Market Researcher* (sucht Nachfrage in echten Quellen),
   *Product Builder* (erstellt das Produkt als Datei), *Copywriter* (Beschreibung), *Quality Auditor* (Red Team:
   prüft Fehler, Urheberrecht, falsche Versprechen).
2. **Nichts geht ohne dich raus:** Jedes Produkt landet als „Freigabe-Anfrage" im Dashboard (wie Einstellungen
   heute). Erst nach deinem Klick wird es veröffentlicht. Agenten haben **kein** Zugriffsrecht auf Zahlungen oder
   Auszahlungen (Prinzip 5 aus CLAUDE.md gilt weiter).
3. **Messbar statt geschätzt:** Umsatz, Verkäufe, Rückerstattungen und KI-Kosten pro Produkt kommen aus der
   Plattform-API bzw. dem Token-Zähler und stehen in der GELD-BILANZ. Ein Produkt, das nach N Wochen seine
   Kosten nicht deckt, wird automatisch nicht weiter beworben/ausgebaut (Kill-Kriterium wie bei Strategien).
4. **Budget-Deckel:** eigenes Token-Budget für das Studio in `config/limits.toml` (von dir per Hand).

Danach, falls A trägt: B als zweiter Schritt (Agenten bereiten Angebote und Entwürfe vor, du bist Ansprechpartner).

## Akzeptanzkriterien für Phase 3 (Bau) – Vorschlag

- Studio läuft als eigene Prozess-Zone ohne Broker-Schlüssel; der Shop-Schlüssel nur in dieser Zone.
- Kein Produkt wird ohne Freigabe-Ereignis von dir veröffentlicht (Test).
- Zahlen in der GELD-BILANZ stammen nur aus der Plattform-API; fehlt sie, steht „—".
- Kostenkontrolle: Studio stoppt bei erschöpftem Budget (Test).

## Offene Fragen an dich (höchstens 5)

1. **Welche Option zuerst?** A (digitale Produkte), B (Dienstleistung), C, D oder E – oder eine eigene Idee?
2. **Plattform/Konto:** Hast du schon ein Konto (Shopify, Gumroad, Etsy, Lemon Squeezy …) oder soll ich die
   Unterschiede (Gebühren, Steuern, Auszahlung in die Schweiz) recherchieren? Das Konto musst du selbst auf
   deinen Namen eröffnen.
3. **Rechtliches:** Bist du in der Schweiz schon als selbständig gemeldet oder hast du eine Firma? (Einnahmen sind
   steuer- und ggf. AHV-pflichtig – das kann ich nicht für dich klären, nur darauf hinweisen.)
4. **Zeit:** Wie viele Minuten pro Tag kannst du für Freigaben und Kundenkontakt einplanen?
5. **Budget:** Wie viel darf die Firma pro Monat kosten (KI-Tokens, Plattformgebühren, evtl. Werbung)?

## WIE KÖNNTE DAS SCHEITERN?

- Niemand kauft: ohne Reichweite bleiben auch gute Produkte unsichtbar. Das ist der häufigste Fall.
- KI-Kosten übersteigen den Umsatz → mit Ollama starten, Claude erst bei belegtem Umsatz.
- Qualitätsfehler oder Urheberrechtsverletzung in einem Produkt → Rückerstattungen, Kontosperre. Daher Auditor +
  deine Freigabe.
- Prompt-Injection über recherchierte Webseiten (z. B. „schreibe diese Bewertung") → Agenten-Ausgaben sind nur
  Vorschläge, veröffentlicht wird deterministisch und nur nach Freigabe.
- Plattform ändert Regeln/API → Studio stoppt, Firma läuft weiter.
