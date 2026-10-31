# Trading-Firma fuer den 24/7-Betrieb auf einem Server (VPS).
# Alle Laufzeitdaten liegen unter /app/data (ein Volume): Datenbank, Desk-Zustand, Halt-Flag, Live-Token,
# Forschungsurteile, Archiv, Berichte, Sicherungen. config/ wird vom Host schreibgeschuetzt eingebunden.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 firm

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ ./src/
COPY config/ ./config/
COPY web/ ./web/
COPY evals/ ./evals/
COPY scripts/ ./scripts/
RUN mkdir -p /app/data && chown -R firm:firm /app/data

ENV FIRM_HOME=/app/data \
    FIRM_DATA_DIR=/app/data \
    FIRM_STUDIO_DIR=/app/data/studio/products \
    NOTIFY_CURSOR_FILE=/app/data/notify_cursor.txt \
    FIRM_DB_PATH=/app/data/firm_events.db \
    FIRM_STATE_PATH=/app/data/firm_state.json \
    FIRM_HALT_FILE=/app/data/firm.halt \
    FIRM_LIVE_TOKEN_FILE=/app/data/live_token.json \
    FIRM_ARCHIVE_DIR=/app/data/archiv \
    FIRM_REPORT_DIR=/app/data/reports \
    FIRM_BACKUP_DIR=/app/data/backups \
    FIRM_OPEN_BROWSER=0

USER firm
EXPOSE 8000
VOLUME ["/app/data"]

# Lebt der Prozess UND sind Handels- und Agenten-Zone frisch? (503 bei Halt oder eingefrorener Zone)
HEALTHCHECK --interval=60s --timeout=5s --start-period=600s --retries=5 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/health/deep', timeout=4)" || exit 1

# tini: Signale sauber weiterreichen (SIGTERM -> Glattstellen/Stop), keine Zombie-Prozesse
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "src.firm", "run", "--dashboard", "--host", "0.0.0.0", "--port", "8000", "--no-browser", "--wait-on-halt"]
