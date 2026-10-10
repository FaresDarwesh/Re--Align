# ---------- build stage ----------
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip && pip install -r requirements.txt

# ---------- runtime stage ----------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ENV=production

RUN apt-get update && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash realign

WORKDIR /srv/re-align

COPY --from=builder /opt/venv /opt/venv
COPY --chown=realign:realign . /srv/re-align

RUN mkdir -p /srv/re-align/data && chown -R realign:realign /srv/re-align

USER realign
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=6s --start-period=25s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/v1/health || exit 1

# Shell form so ${PORT} expands: Railway injects PORT, locally it defaults to 8000.
CMD uvicorn realign.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2
