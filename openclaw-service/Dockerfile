# Official OpenClaw 2026.10.5-slim; GHCR manifest verified HTTP 200.
# No npm install, source compilation, browser or additional runtime.
FROM ghcr.io/openclaw/openclaw@sha256:f40d1e66fd14d8c5b49c832f4100f83b53fdc01f5108f7db11de38fd0ee6dbe5
COPY --chown=node:node bootstrap.mjs start.sh /opt/openclaw-service/
ENV NODE_OPTIONS="--max-old-space-size=384"
USER node
ENTRYPOINT ["tini", "-s", "--"]
CMD ["/bin/sh", "/opt/openclaw-service/start.sh"]
