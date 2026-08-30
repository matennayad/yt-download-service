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
    curl \
    gnupg \
    tor \
    privoxy \
    && rm -rf /var/lib/apt/lists/*

RUN echo "forward-socks5t / 127.0.0.1:9050 ." >> /etc/privoxy/config

# ============================================================
# CLOUDFLARE WARP
# ============================================================
RUN curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ bookworm main" > /etc/apt/sources.list.d/cloudflare-client.list \
    && apt-get update && apt-get install -y --no-install-recommends cloudflare-warp \
    && rm -rf /var/lib/apt/lists/*

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
COPY start.sh .
RUN chmod +x start.sh

# ============================================================
# PORT
# ============================================================
ENV PORT=8080
EXPOSE 8080

# ============================================================
# START
# ============================================================
CMD ["./start.sh"]
