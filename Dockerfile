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
CMD ["python3", "app.py"]
