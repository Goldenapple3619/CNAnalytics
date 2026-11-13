FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --system --no-create-home --uid "10001" processor

RUN mkdir -p /mnt/data/processed \
    && chown -R 10001:10001 /mnt/data

USER processor

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=4s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["sh", "-c", "exec python main.py --ingest-db \"$INGEST_DB\" --db \"$PROCESSED_DB\""]
