FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN DEBIAN_FRONTEND=noninteractive apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends xvfb tzdata && \
    rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 3000
VOLUME ["/config"]

ENV CONFIG_DIR=/config \
    TZ=America/New_York

# xvfb-run starts a virtual framebuffer and sets DISPLAY for all child processes.
# Chromium inherits DISPLAY and runs in headed mode — hCaptcha sees a real
# browser context (WebGL, canvas, GPU via Mesa software renderer) rather than
# the headless flags that trigger visual challenge mode.
CMD ["xvfb-run", "--server-args=-screen 0 1280x720x24 -ac", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]
