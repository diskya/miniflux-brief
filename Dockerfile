# feedsum: the summarizing service. Small — the browser stack belongs to the
# reader image, not this one.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY feedsum ./feedsum

# The SQLite database holds the summaries and, more importantly, the original
# entry content every write-back rebuilds from. Losing it means losing the
# ability to undo a rewrite, so it lives on a volume.
ENV FEEDSUM_DB=/data/feedsum.db
RUN mkdir -p /data && useradd --system --uid 1101 feedsum && chown feedsum /data
VOLUME /data
USER feedsum

EXPOSE 8012
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8012/healthz').status_code == 200 else 1)"

CMD ["python", "-m", "uvicorn", "feedsum.app:app", "--host", "0.0.0.0", "--port", "8012", "--no-access-log"]
