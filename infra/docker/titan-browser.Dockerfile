# titan-browser — the headless-Chromium container for the browser.* tools
# (Phase 22; Vol 7 §5, TITAN-INT-032). Playwright's official image already
# carries the matched browsers under /ms-playwright; we bake in the `playwright`
# npm package so `playwright run-server` runs FULLY OFFLINE (no npx download) and
# exposes a websocket the trusted Tool Host connects to via chromium.connect().
# The container itself runs the browser; the orchestrator process never does
# (browser runs "in its own container", not the Tool Host).
# Build:  docker build -f infra/docker/titan-browser.Dockerfile -t titan-browser:local .
# syntax=docker/dockerfile:1

FROM mcr.microsoft.com/playwright:v1.61.0-noble

# Pin the driver to the image's bundled browser build so run-server is offline.
WORKDIR /opt/titan-browser
RUN npm init -y >/dev/null 2>&1 && npm install --no-audit --no-fund playwright@1.61.0

# The controller overrides host/port at RUN time; browsers live here already.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
CMD ["npx", "playwright", "run-server", "--port", "3000", "--host", "0.0.0.0"]
