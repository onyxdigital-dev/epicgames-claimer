FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 3000
VOLUME ["/config"]

ENV CONFIG_DIR=/config \
    TZ=America/New_York

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]
