"""FastAPI interface for the article reader.

Turns a URL into clean article Markdown. feedsum is the only caller here, but
the service is deliberately independent of it: anything that speaks the two-route
contract below can be dropped in instead (see FEEDSUM_READER_URL).
"""

from __future__ import annotations

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .reader import ReaderError, read_article


app = FastAPI(title="Article Reader")


@app.exception_handler(ReaderError)
async def reader_error_handler(_request: Request, exc: ReaderError) -> JSONResponse:
    headers = {"Retry-After": exc.retry_after} if exc.retry_after else None
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message, "code": exc.code},
        headers=headers,
    )


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/v1/read")
async def read(url: str = Query(...), json: bool = Query(False)):
    article = await read_article(url)
    if json:
        return article.as_dict()
    return PlainTextResponse(article.text, media_type="text/markdown")


@app.get("/{url:path}")
async def compatibility_read(url: str, request: Request):
    # ASGI separates the query string before route matching, but on this
    # compatibility route it is part of the target URL rather than our API.
    query = request.scope["query_string"].decode()
    if query:
        url = f"{url}?{query}"
    article = await read_article(url)
    return PlainTextResponse(article.text, media_type="text/markdown")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("reader.app:app", host="127.0.0.1", port=8010)
