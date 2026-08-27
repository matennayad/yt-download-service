FROM node:20-bookworm-slim

# ============================================================
# SYSTEM PACKAGES
# ============================================================

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    ffmpeg \
    git \
    ca-certificates \
    tor \
    privoxy \
    && rm -rf /var/lib/apt/lists/*

RUN echo "forward-socks5t / 127.0.0.1:9050 ." >> /etc/privoxy/config


# ============================================================
# APP
# ============================================================

WORKDIR /app

COPY requirements.txt .

RUN pip3 install \
    --no-cache-dir \
    --break-system-packages \
    -r requirements.txt


# ============================================================
# BGUTIL PO TOKEN PROVIDER
# ============================================================

RUN git clone \
    --single-branch \
    --branch 1.3.1 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    /opt/bgutil-ytdlp-pot-provider

WORKDIR /opt/bgutil-ytdlp-pot-provider/server

RUN npm ci

RUN npx tsc


# ============================================================
# COPY FLASK APP
# ============================================================

WORKDIR /app

COPY app.py .


# ============================================================
# PORT
# ============================================================

ENV PORT=8080

EXPOSE 8080


# ============================================================
# START
# ============================================================

CMD sh -c "tor --SocksPort 9050 --RunAsDaemon 0 & \
    sleep 5 && \
    privoxy --no-daemon /etc/privoxy/config & \
    sleep 2 && \
    export HTTP_PROXY=http://127.0.0.1:8118 && \
    export HTTPS_PROXY=http://127.0.0.1:8118 && \
    node /opt/bgutil-ytdlp-pot-provider/server/build/main.js & \
    exec gunicorn \
    --bind 0.0.0.0:\$PORT \
    --workers 1 \
    --threads 4 \
    --timeout 600 \
    app:app"
