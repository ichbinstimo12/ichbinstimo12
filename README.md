# Autonome Trading-Firma (Capital.com, Agenten + deterministische Money-Zone)

- Spezifikation: `CLAUDE.md`, Betrieb: `docs/runbook.md`
- Letztes Audit und Änderungen: **`docs/AUDIT_2026-10.md`**

```bash
pip install -r requirements.txt
cp .env.example .env      # Demo-Zugangsdaten eintragen, nie committen
python -m pytest -q
python -m src.firm run --dashboard
```

Kein Gewinnversprechen. Handel nur mit Strategien, die die Walk-Forward-Prüfung bestehen; Live nur mit `approve-live`.
