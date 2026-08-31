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
# מפעיל ברקע את שרת ה-PO token provider (Node, פורט 4416 - זה מה ש-app.py
# מצפה לו דרך pot_provider: http://127.0.0.1:4416), ואז מריץ בחזית את
# שרת ה-Flask עצמו (exec כדי שהוא יהיה התהליך הראשי של הקונטיינר).
CMD ["sh", "-c", "PORT=4416 node /opt/bgutil-ytdlp-pot-provider/server/build/main.js & exec python3 app.py"]
