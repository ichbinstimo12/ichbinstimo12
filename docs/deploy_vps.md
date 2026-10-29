# 24/7-Betrieb auf einem Server (ohne deinen PC)

Die Firma läuft in einem Docker-Container auf einem kleinen Mietserver (VPS). Dein PC kann aus sein.
Du bekommst Nachrichten aufs Handy und kannst das Dashboard vom Handy öffnen.

## 1. Server mieten

- Empfehlung: Hetzner Cloud **CX22** (2 vCPU, 4 GB RAM, ca. 4–5 €/Monat), Standort Falkenstein/Nürnberg,
  Image **Ubuntu 24.04**. Andere Anbieter (Infomaniak, DigitalOcean, Contabo) gehen genauso.
- Beim Anlegen deinen **SSH-Schlüssel** hinterlegen (Windows: `ssh-keygen` in PowerShell, Inhalt von
  `C:\Users\<du>\.ssh\id_ed25519.pub` einfügen).
- LLM: Auf einem kleinen Server ist **Anthropic** (`LLM_PROVIDER=anthropic`) die richtige Wahl. Ein lokales
  14B-Ollama-Modell braucht ≥16 GB RAM und ist auf CPU sehr langsam (Profil `ollama`, siehe unten).

## 2. Server einrichten (einmalig)

```bash
ssh root@<server-ip>
curl -fsSLO https://raw.githubusercontent.com/<dein-github>/<repo>/<branch>/scripts/vps_setup.sh   # oder per scp kopieren
bash vps_setup.sh            # mit --https, wenn du Schritt 6 nutzen willst
```

Das Skript installiert Updates (automatische Sicherheitsupdates), Docker, Firewall (nur SSH offen),
fail2ban, Swap und legt den Benutzer `firm` an. Passwort-Login per SSH wird abgeschaltet.

## 3. Code holen und `.env` anlegen

```bash
ssh firm@<server-ip>
git clone https://github.com/<dein-github>/<repo>.git trading-firm    # privates Repo: Token oder Deploy-Key
cd trading-firm
cp .env.example .env
nano .env        # Schlüssel eintragen (siehe unten), speichern: Strg+O, Enter, Strg+X
```

Mindestens ausfüllen:

| Variable | Wert |
|---|---|
| `CAPITAL_API_KEY`, `CAPITAL_IDENTIFIER`, `CAPITAL_PASSWORD` | Demo-Schlüssel |
| `FIRM_API_TOKEN` | langes Zufallspasswort: `openssl rand -hex 32` |
| `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY` | für die Agenten |
| `FIRM_MODE=PAPER` | bleibt so, bis du bewusst freigibst |

Die `.env` bleibt nur auf dem Server (`chmod 600`, macht das Start-Skript). Nie ins Git, nie in Chats.

## 4. Starten

```bash
./scripts/firmctl.sh start     # baut das Image und startet im Hintergrund
./scripts/firmctl.sh status    # Container, Gesundheit, Kontostand, Aufgaben
./scripts/firmctl.sh logs      # Live-Log (Strg+C beendet nur die Anzeige)
```

Der Container startet nach Absturz oder Server-Neustart von selbst (`restart: unless-stopped`).
Alle Daten liegen in `~/trading-firm/data/` (Datenbank, Zustand, Berichte, tägliche Sicherungen).

## 5. Benachrichtigungen aufs Handy

**ntfy (am einfachsten):** App „ntfy" installieren → Thema abonnieren mit langem Zufallsnamen
(z. B. `firm-` + `openssl rand -hex 12`). In `.env`: `NOTIFY_NTFY_URL=https://ntfy.sh/<thema>`.
Wer den Namen kennt, kann mitlesen: deshalb lang und zufällig, und Beträge sind standardmäßig aus
(`NOTIFY_INCLUDE_AMOUNTS=0`).

**Telegram:** Bei @BotFather `/newbot` → Token in `NOTIFY_TELEGRAM_BOT_TOKEN`. Dem Bot eine Nachricht
schicken, Chat-ID z. B. über @userinfobot → `NOTIFY_TELEGRAM_CHAT_ID`.

**Totmannschalter:** Bei healthchecks.io (kostenlos) einen Check mit Periode 5 min, Karenz 15 min anlegen →
URL in `HEALTHCHECK_PING_URL`. Fällt der ganze Server aus, meldet dich der Dienst per Mail/App.

Danach: `./scripts/firmctl.sh restart` und `./scripts/firmctl.sh notify-test`.

Du bekommst: Trades (eröffnet/geschlossen), Kill-Switch (mit „flach bestätigt ja/nein"), Handelsstopp,
gestoppte Strategien, Broker offline, Forschung fertig/fehlgeschlagen, Freigaben (Einstellung, Playbook,
Skalierungs-Leiter), Tagesbericht. Mit Rate-Limit gegen Nachrichtenfluten (Alarme gehen immer durch).

## 6. Dashboard vom Handy

**Variante A – SSH-Tunnel (am sichersten, nichts offen):**
`ssh -L 8000:127.0.0.1:8000 firm@<server-ip>` und dann im Browser `http://127.0.0.1:8000`.
Am Handy z. B. mit Termius (Port-Forwarding).

**Variante B – HTTPS mit eigener Domain:**
1. Subdomain (z. B. `firm.deinname.ch`) als A-Record auf die Server-IP zeigen lassen.
2. Setup mit `--https` ausgeführt (Ports 80/443 offen).
3. Passwort-Hash erzeugen: `docker run --rm caddy:2.8 caddy hash-password` und in `.env` eintragen
   (jedes `$` als `$$` schreiben):
   ```
   FIRM_DOMAIN=firm.deinname.ch
   CADDY_PASSWORD_HASH=$$2a$$14$$...
   FIRM_ALLOWED_ORIGINS=https://firm.deinname.ch
   ```
4. `FIRM_PROFILES=https ./scripts/firmctl.sh restart`

Zwei Schutzschichten: Passwort (Caddy) + `FIRM_API_TOKEN` im Dashboard. Nur `/health` und `/health/deep`
sind ohne Passwort erreichbar (ohne Kontozahlen), damit Uptime-Dienste prüfen können.

## 7. Bedienung

| Aufgabe | Befehl |
|---|---|
| Notfall: alles glattstellen und anhalten | `./scripts/firmctl.sh halt` (oder Kill-Switch im Dashboard) |
| Nach Halt wieder starten | `./scripts/firmctl.sh resume` – die Firma startet dann **von selbst** neu |
| Update einspielen | `./scripts/firmctl.sh update` |
| Stoppen / Starten | `./scripts/firmctl.sh stop` / `start` |
| Sicherung sofort | `./scripts/firmctl.sh backup` |
| Live-Freigabe | `./scripts/firmctl.sh approve-live 3600`, dann `FIRM_MODE=LIVE_MICRO` in `.env` und `restart` |

Verhalten bei Halt (Limitbruch, Kill-Switch, Reconciler-Abweichung): Positionen werden glattgestellt, die
Firma bleibt im Warte-Modus (Dashboard erreichbar, nichts handelt) bis du `resume` ausführst.
Bei `stop`/`update` werden Positionen **nicht** geschlossen (sie haben serverseitige Stops beim Broker);
beim Start wird der Stand vom Broker geladen.

## 8. Ollama statt Anthropic (optional)

Nur mit genug RAM (≥16 GB für 14B-Modelle):
```bash
# .env: LLM_PROVIDER=ollama, OLLAMA_BASE_URL=http://ollama:11434, OLLAMA_MODEL=qwen2.5:14b
FIRM_PROFILES=ollama ./scripts/firmctl.sh restart
docker compose exec ollama ollama pull qwen2.5:14b
```

## Was der Server nicht ändert

Mehr Laufzeit heißt nicht mehr Gewinn. Die Firma handelt nur Strategien, die die Forschung bestehen, mit
denselben harten Limits aus `config/limits.toml`. „Keine Strategie gefunden, wir handeln nicht" bleibt
ein gültiges Ergebnis.
