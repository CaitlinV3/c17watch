FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    C17WATCH_STATE_PATH=/data/state.json

WORKDIR /app
COPY requirements.txt .
RUN grep -q -- '--hash=sha256:' requirements.txt \
 || { echo "requirements.txt is not locked: run scripts/lock.sh on your Mac and commit it" >&2; exit 1; } \
 && pip install --require-hashes --no-deps -r requirements.txt

COPY c17watch/ c17watch/

# Unprivileged user; /data holds the state file so restarts don't re-alert.
RUN useradd --system --uid 10001 --no-create-home app \
 && mkdir /data && chown app /data
USER app
VOLUME ["/data"]

# The app handles SIGTERM itself and saves state before exiting.
ENTRYPOINT ["python", "-m", "c17watch", "--config", "/config/config.toml"]
