# The article reader. Large, because the escalation tiers drive real browsers:
# the image carries Chromium and Camoufox and their system libraries.
#
# Build from the repository root:  docker build -f reader/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY reader/requirements.txt ./reader/
RUN pip install --no-cache-dir -r reader/requirements.txt

# Two Chromium builds, because the two browser tiers are driven by different
# libraries: Scrapling's dynamic fetcher uses Playwright, and its stealth
# fetcher uses Patchright. `scrapling install` only covers the first, so
# missing the second turns every escalation to stealth into a fetch error.
# This is most of the image's size.
#
# It runs as root, because installing the system libraries the browsers link
# against needs apt. HOME points at the unprivileged user first, so the
# downloads land somewhere that user can read.
RUN useradd --create-home --system --uid 1102 reader
ENV HOME=/home/reader
RUN scrapling install \
    && patchright install chromium \
    # Each install also brings a headless-shell build, half a gigabyte between
    # them, that nothing here launches: Scrapling pins the browser channel to
    # "chromium" and opens a persistent context, both of which mean the full
    # browser. Delete this line if a Scrapling upgrade ever changes that.
    && rm -rf "$HOME"/.cache/ms-playwright/chromium_headless_shell-* \
    && rm -rf /var/lib/apt/lists/* \
    && chown -R reader /home/reader /app

USER reader
COPY --chown=reader reader/*.py ./reader/

EXPOSE 8010
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8010/health').status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "reader.app:app", "--host", "0.0.0.0", "--port", "8010", "--no-access-log"]
