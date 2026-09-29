# The collector only. The reporter runs inside Claude Code's status line on
# each machine, so it has no place in an image.
FROM python:3.12-slim

# Standard library only: nothing to install.
WORKDIR /app
COPY collector/ collector/

RUN useradd --system --no-create-home --uid 10001 statusgumbo
USER statusgumbo

# Reachable from outside the container means beyond loopback and the tailnet,
# so the collector refuses to start without a token: pass STATUSGUMBO_TOKEN or
# mount a file and point STATUSGUMBO_TOKEN_FILE at it.
ENV STATUSGUMBO_BIND=0.0.0.0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
EXPOSE 4747

# /healthz needs no token. The port is fixed here, so publish a different one
# with `-p 8080:4747` rather than passing --port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4747/healthz', timeout=3)"]

# No machine of its own to list (the hostname is a container id), and cloud
# sessions stay off (the default, stated so a changed default can't turn them
# on): they need a claude.ai login the container doesn't have.
ENTRYPOINT ["python3", "-m", "collector.server", "--no-local-host", "--no-cloud"]
