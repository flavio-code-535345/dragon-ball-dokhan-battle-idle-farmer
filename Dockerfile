# syntax=docker/dockerfile:1
FROM python:3.13-slim

LABEL org.opencontainers.image.title="Dokkan Idle Farmer" \
      org.opencontainers.image.description="Self-hosted idle farmer for Dragon Ball Z Dokkan Battle (ADB + OpenCV + web UI)" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    HOME=/home/farmer \
    DATA_DIR=/data \
    WEB_PORT=8080

# adb talks to the Android device, tini reaps adb's background server,
# tzdata makes TZ / ACTIVE_HOURS work, kmod (modprobe) lets host-setup load
# the binder driver for the all-in-one Android stack.
RUN apt-get update \
 && apt-get install -y --no-install-recommends adb kmod tini tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 1000 farmer \
 && useradd --uid 1000 --gid farmer --home-dir /home/farmer --create-home \
      --shell /usr/sbin/nologin farmer \
 && mkdir -p /data \
 && chown farmer:farmer /data

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY src ./src
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/host-setup.sh /usr/local/bin/host-setup
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/host-setup \
 && python -m compileall -q src

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('WEB_PORT', '8080') + '/api/health', timeout=4)"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "dokkan_farmer"]
