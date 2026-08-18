FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH" \
    BGUTIL_SCRIPT_PATH="/opt/bgutil-ytdlp-pot-provider/server/build/generate_once.js"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 \
       python3-venv \
       ffmpeg \
       git \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python environment
COPY requirements.txt /tmp/requirements.txt

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r /tmp/requirements.txt

# Build the bgutil PO-token generation provider.
# 1.3.1 is pinned to match the Python plugin version above.
RUN git clone --depth 1 --branch 1.3.1 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    /opt/bgutil-ytdlp-pot-provider \
    && cd /opt/bgutil-ytdlp-pot-provider/server \
    && npm ci \
    && npx tsc

WORKDIR /app

COPY main.py /app/main.py

EXPOSE 10000

CMD ["sh", "-c", "exec /opt/venv/bin/uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]
