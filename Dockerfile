FROM python:3.12-slim

ARG VERSION=dev

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    JFTOOLS_DATA_DIR=/data \
    TZ=Europe/Stockholm \
    JFTOOLS_VERSION=$VERSION

WORKDIR /app

# gosu drops privileges in the entrypoint; tzdata gives the OS real time zones;
# ffmpeg provides ffprobe for the subtitle scans.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu tzdata ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY resources ./resources
# The version is written into the image as well as exported as ENV: Docker
# snapshots ENV into a container only at create time, so a container recreated
# from an older config snapshot would otherwise report a stale version while
# running the new code. See app/version.py.
RUN printf '%s\n' "$VERSION" > /app/app/VERSION
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && useradd --create-home --uid 1000 jftools \
    && mkdir -p /data

EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=3).status == 200 else 1)"

# Starts as root so the entrypoint can align jftools with PUID/PGID, then drops
# to that user before running the app. JFTools keeps state in memory, so it must
# run as a single process (no multiple uvicorn workers / replicas).
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
