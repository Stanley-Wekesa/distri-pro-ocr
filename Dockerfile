FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/var/data \
    PORT=8080

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 libglib2.0-0 libgl1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install paddlepaddle==3.2.0 \
    && python -m pip install -r requirements.txt

COPY app ./app
RUN mkdir -p /var/data/sources

# Download PaddleOCR models during image build to avoid first-request downloads.
RUN python -c "from paddleocr import PaddleOCR; PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=True, engine='paddle')"

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --start-period=300s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
