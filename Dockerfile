FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/n2-study
COPY app/requirements.txt /opt/n2-study/app/requirements.txt
RUN pip install -r app/requirements.txt
COPY app /opt/n2-study/app
# COPY preserves restrictive permissions from a private SSH extraction directory.
# Application source is public inside the image; the private /data mount is separate.
RUN chmod -R a+rX /opt/n2-study
USER 65534:65534
# Fail the image build here if an ordinary runtime user cannot import/read the app.
RUN python -c "import app.server, app.device_clock; from pathlib import Path; [p.read_bytes() for p in Path('app/static').iterdir() if p.is_file()]"
ENV APP_DATA_DIR=/data DPT_AUTH_DIR=/data/auth APP_BIND=0.0.0.0 APP_PORT=4387
EXPOSE 4387
HEALTHCHECK --interval=30s --timeout=4s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4387/api/health',timeout=3)"
CMD ["python", "-m", "app.server"]
