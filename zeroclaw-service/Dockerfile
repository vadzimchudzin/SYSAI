FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl tar python3 \
    && rm -rf /var/lib/apt/lists/*
ARG ZEROCLAW_VERSION=v0.8.6
ARG ZEROCLAW_ARCHIVE=zeroclaw-x86_64-unknown-linux-musl.tar.gz
WORKDIR /app
RUN curl -fsSL "https://github.com/zeroclaw-labs/zeroclaw/releases/download/${ZEROCLAW_VERSION}/${ZEROCLAW_ARCHIVE}" \
    -o /tmp/zeroclaw.tgz \
    && tar -xzf /tmp/zeroclaw.tgz -C /tmp \
    && BIN=$(find /tmp -type f -name zeroclaw | head -n1) \
    && test -n "$BIN" \
    && mv "$BIN" /usr/local/bin/zeroclaw \
    && chmod +x /usr/local/bin/zeroclaw \
    && rm -rf /tmp/zeroclaw.tgz /tmp/zeroclaw*
COPY bootstrap.py start.sh /app/
RUN chmod +x /app/start.sh
ENV PORT=10000
EXPOSE 10000
CMD ["/app/start.sh"]
