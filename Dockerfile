# RTL-SDR Player — Docker image for Ubuntu servers (and any Linux host).
#
# Build:   docker build -t rtl-sdr-player .
# Run:     docker run -d --name rtl-sdr-player \
#              -p 8515:8515 --restart unless-stopped \
#              --device=/dev/bus/usb \
#              -v rtl-sdr-data:/app/data \
#              rtl-sdr-player
# Or use:  docker compose up -d --build
#
# The image uses pyrtlsdrlib's bundled librtlsdr; only libusb comes from
# apt. Pass the whole USB bus through (--device=/dev/bus/usb) so the
# RTL2832U dongle is visible inside the container. All runtime state
# (presets, agent/LLM settings, PI cache, digests, audits) lives in
# /app/data via the RTLSDR_DATA_DIR env var -> one named volume.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so source edits do not bust this layer
COPY requirements.txt .
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
ENV PATH="/opt/venv/bin:$PATH"

# Application
COPY app.py check_dlls.py config.json run.sh README.md ./
COPY backend ./backend
COPY web ./web
COPY dlls ./dlls

# One volume holds every runtime artifact (config: data_dir override)
ENV RTLSDR_DATA_DIR=/app/data
VOLUME ["/app/data"]

EXPOSE 8515

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8515/api/status', timeout=4)"]

# --share: listen on all interfaces (the container's port mapping needs it);
# --no-browser: a server has no desktop to open a browser on. Without a
# dongle the app automatically serves the simulated spectrum instead.
CMD ["python3", "app.py", "--share", "--no-browser"]
