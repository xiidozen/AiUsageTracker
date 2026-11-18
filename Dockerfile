FROM --platform=$BUILDPLATFORM python:3.12-slim AS codex-download

ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && case "$TARGETARCH" in \
        amd64) arch=x86_64; checksum=9af4d751fce183faedea629437d5252dbf458eeb04130cf4f97be844621ea985 ;; \
        arm64) arch=aarch64; checksum=97b97d87eea075c5cb7709c7f6156a451622a7667953ecda40e0276637196f3f ;; \
        *) exit 1 ;; \
    esac \
    && asset="codex-${arch}-unknown-linux-musl" \
    && curl -fsSL --retry 3 "https://github.com/openai/codex/releases/download/rust-v0.169.0/${asset}.tar.gz" -o /tmp/codex.tar.gz \
    && echo "$checksum  /tmp/codex.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/codex.tar.gz -C /tmp "$asset" \
    && install -m 0755 "/tmp/$asset" /usr/local/bin/codex

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8080

COPY --from=codex-download /usr/local/bin/codex /usr/local/bin/codex

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tracker ./tracker

RUN useradd --system --uid 10001 --home-dir /app tracker \
    && mkdir -p /data && chown tracker /data
USER tracker

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8080'), timeout=4)"

CMD ["python", "-m", "tracker.main"]
