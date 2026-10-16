FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/light-map

COPY requirements.txt .
RUN pip install -r requirements.txt

# Non-root runtime user. /data is created here and owned by that user so a fresh named
# volume mounted over it inherits the ownership; the code stays root-owned and read-only.
RUN groupadd --system --gid 10001 lightmap \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin lightmap \
 && mkdir /data \
 && chown 10001:10001 /data

# Copy named directories only, so a local .env holding the HA token can never land in the
# image.
COPY app/ app/
COPY static/ static/
COPY schema/ schema/
COPY tools/ tools/

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8099

# The slim image has no curl, so probe with Python's standard library.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=4)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099"]
