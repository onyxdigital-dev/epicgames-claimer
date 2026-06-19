FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN apt-get update && \
    pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium --with-deps && \
    rm -rf /var/lib/apt/lists/*

COPY app/ ./app/

EXPOSE 3000
VOLUME ["/config"]

ENV CONFIG_DIR=/config \
    TZ=America/New_York

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]
