# syntax=docker/dockerfile:1
#
# DocuShelf: web interface, background worker and OCR in one container.
#
#   docker build -t docushelf .
#   docker run -d -p 127.0.0.1:8000:8000 -v "$PWD/data:/data" docushelf
#
# The Python dependencies ship wheels for linux/amd64 and linux/arm64, so the
# image builds on both without a compiler. The data volume (/data) must be
# writable for UID 1000.

ARG PYTHON_IMAGE=python:3.12-slim-trixie

# ---------------------------------------------------------------------------
# Build stage: application and dependencies in a virtual environment
FROM ${PYTHON_IMAGE} AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN python -m venv /opt/docushelf
ENV PATH=/opt/docushelf/bin:$PATH

WORKDIR /src

# Dependencies first: they are only reinstalled when pyproject.toml changes.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt

COPY docushelf ./docushelf
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps . \
 && docushelf --version

# ---------------------------------------------------------------------------
# Runtime stage
FROM ${PYTHON_IMAGE}

ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="DocuShelf" \
      org.opencontainers.image.description="Ordnerbasiertes Dokumentenarchiv mit OCR, Eingang und Volltextsuche" \
      org.opencontainers.image.version="${VERSION}"

# OCR toolchain called by OCRmyPDF, time zone data, tini as init process.
# The data directory is prepared for UID 1000 so that a new named volume is
# writable right away.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-deu \
        tesseract-ocr-eng \
        tesseract-ocr-osd \
        ghostscript \
        unpaper \
        pngquant \
        tini \
        tzdata \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /var/log/apt /var/log/dpkg.log \
 && groupadd --gid 1000 docushelf \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/docushelf --shell /usr/sbin/nologin docushelf \
 && mkdir -p /data \
 && chown 1000:1000 /data

COPY --from=build /opt/docushelf /opt/docushelf

ENV PATH=/opt/docushelf/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/docushelf \
    DOCUSHELF_DATA_DIR=/data

USER 1000:1000
WORKDIR /home/docushelf
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["docushelf", "healthcheck"]

# tini forwards SIGTERM to the server (which shuts down gracefully) and reaps
# OCR helper processes.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["docushelf", "serve"]
