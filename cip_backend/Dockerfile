# CIP Monitor: replays the CIP log files as live 60-second pings (or takes readings from gateways in
# live mode) and serves the web UI on port 8000.
# The client's log files are NOT part of the image: mount them read-only at /data at runtime.
FROM python:3.12.16-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Dependencies first so code changes do not invalidate this layer.
COPY requirements.txt .
# The libraries' own test suites are dropped (~30 MB; never imported at runtime).
RUN pip install -r requirements.txt \
 && find /usr/local/lib/python3.12/site-packages -depth -type d -name tests -path '*/pandas/*' -exec rm -rf {} + \
 && find /usr/local/lib/python3.12/site-packages -depth -type d -name tests -path '*/numpy/*' -exec rm -rf {} + \
 && groupadd --system --gid 10001 cip \
 && useradd --uid 10001 --gid cip --no-create-home --shell /usr/sbin/nologin cip \
 && mkdir /state && chown cip:cip /state

# Application code stays root-owned and read-only for the runtime user.
COPY cip_alerting/ cip_alerting/
COPY scripts/ scripts/
COPY web/ web/
# Proposed T/A/C rule definitions (pending Danone sign-off; CIP_PROPOSED_RULES=off|show|alert, default show)
COPY rules/ rules/

# Defaults; override with `docker run -e` or docker-compose `environment:`.
# /state holds the SQLite database (acknowledgements, audit log, run history): mount a volume there so it
# survives the container. Without a writable /state the server warns and keeps them in memory only.
# CIP_INGEST_TOKEN (live mode), the Teams webhook and SMTP settings (notifications) are deliberately not given
# defaults here: secrets come from the environment at run time.
ENV CIP_DATA_DIR=/data \
    CIP_SPEED=60 \
    CIP_START= \
    CIP_MODE=replay \
    CIP_DB_PATH=/state/cip_monitor.db \
    CIP_PROPOSED_RULES=show \
    CIP_STALE_AFTER_SECONDS=180

USER cip
EXPOSE 8000

# start-period covers loading the xlsx logs (about 5-15 s).
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=4)"]

CMD ["python", "scripts/serve.py", "--host", "0.0.0.0", "--port", "8000", "--loop", "--data-dir", "/data"]
