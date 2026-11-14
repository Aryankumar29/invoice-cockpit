# One image: built UI + FastAPI + worker + Tesseract for the local OCR fallback (docs/DESIGN.md D12).
FROM node:24-slim AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# 3.12: the pinned numpy/scipy publish no 3.11 wheels. Code stays 3.11-compatible.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY cockpit/ cockpit/
COPY fixtures/ fixtures/
COPY --from=web /web/dist web/dist
ENV COCKPIT_DATA_DIR=/data COCKPIT_REPLAY_DIR=/data/replay PORT=8000 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1
CMD ["python", "-m", "cockpit.api"]
