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
# VIRTUAL BROWSER (Playwright)
# ============================================================
RUN pip3 install --no-cache-dir --break-system-packages playwright
RUN playwright install --with-deps chromium

# ============================================================
# BGUTIL PO TOKEN PROVIDER
# ============================================================
RUN curl -L https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/heads/main.tar.gz -o /tmp/pot.tar.gz \
    && mkdir -p /opt/bgutil-ytdlp-pot-provider \
    && tar -xzf /tmp/pot.tar.gz -C /opt/bgutil-ytdlp-pot-provider --strip-components=1 \
    && rm /tmp/pot.tar.gz

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
CMD ["sh", "-c", "PORT=4416 node /opt/bgutil-ytdlp-pot-provider/server/build/main.js & exec python3 app.py"]
