#!/bin/sh
# Build and verify an isolated stack. It never uses .env, port 8012, or a host DB.
set -eu

PROJECT="miniflux-brief-ci"
ENV_FILE="tests/fixtures/compose.env"
OVERRIDE="tests/fixtures/compose.test.yml"
HOST_CANARY_PID=""
HOST_CANARY_LOG=""

compose_command() {
    if [ -n "${COMPOSE_BIN:-}" ]; then
        "$COMPOSE_BIN" "$@"
    else
        docker compose "$@"
    fi
}

compose() {
    FEEDSUM_ENV_FILE="$ENV_FILE" FEEDSUM_PORT=18012 \
        compose_command --project-name "$PROJECT" -f docker-compose.yml -f "$OVERRIDE" "$@"
}

cleanup() {
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    sudo ops/reader-firewall.sh --remove >/dev/null 2>&1 || true
    if [ -n "$HOST_CANARY_PID" ]; then
        kill "$HOST_CANARY_PID" >/dev/null 2>&1 || true
        wait "$HOST_CANARY_PID" 2>/dev/null || true
    fi
    if [ -n "$HOST_CANARY_LOG" ]; then
        rm -f -- "$HOST_CANARY_LOG"
    fi
}
trap cleanup EXIT INT TERM

compose_command version >/dev/null
compose config >/dev/null
sudo ops/reader-firewall.sh --apply
HOST_CANARY_LOG=$(mktemp /tmp/miniflux-brief-host-canary.XXXXXX)
python3 -m http.server 18082 --bind 0.0.0.0 >"$HOST_CANARY_LOG" 2>&1 &
HOST_CANARY_PID=$!
sleep 1
kill -0 "$HOST_CANARY_PID"

if [ "${VERIFY_SKIP_BUILD:-0}" != "1" ]; then
    compose build feedsum reader egress-proxy
fi
compose up -d --wait

curl -fsS http://127.0.0.1:18012/healthz >/dev/null
test "$(curl -sS -o /dev/null -w '%{http_code}' \
    http://127.0.0.1:18012/v1/summaries/1)" = "401"
test "$(curl -sS -o /dev/null -w '%{http_code}' \
    -H 'X-Feedsum-UI-Token: ci-not-a-real-ui-token' \
    http://127.0.0.1:18012/v1/summaries/1)" = "404"

compose exec -T feedsum python -c \
    'import httpx; assert httpx.get("http://reader-gateway:8010/health").status_code == 200'

compose exec -T reader python - <<'PY'
import asyncio
import socket

from reader.reader import _dynamic_once, _static_once, _stealth_once


async def main():
    for target in (
        ("172.30.40.1", 18082),
        ("172.30.40.17", 18082),
        ("169.254.169.254", 80),
        ("10.0.0.1", 80),
    ):
        try:
            socket.create_connection(target, 1)
        except OSError:
            pass
        else:
            raise AssertionError(f"direct socket reached {target}")

    try:
        socket.getaddrinfo("feedsum", 8012)
    except socket.gaierror:
        pass
    else:
        raise AssertionError("reader resolved the sibling application container")

    static = await _static_once("https://example.com/")
    dynamic = await _dynamic_once("https://example.com/")
    stealth = await _stealth_once("https://example.com/")
    assert all(int(page.status) == 200 for page in (static, dynamic, stealth))

    # The proxy resolves this name on its outbound network to the private canary.
    # Both browsers must receive a denial rather than reach it.
    for fetch in (_dynamic_once, _stealth_once):
        try:
            page = await fetch("http://rebind.test/canary")
        except Exception:
            continue
        assert int(getattr(page, "status", 0)) != 200

    redirect = (
        "https://httpbingo.org/redirect-to?"
        "url=http%3A%2F%2Frebind.test%2Fredirect-canary"
    )
    for fetch in (_static_once, _dynamic_once, _stealth_once):
        try:
            page = await fetch(redirect)
        except Exception:
            continue
        assert int(getattr(page, "status", 0)) != 200


asyncio.run(main())
PY

if compose logs canary | grep -Eq 'GET /(canary|redirect-canary)'; then
    echo "private canary was reached" >&2
    exit 1
fi

echo "isolated stack, wiring, browser tiers, and reader egress: verified"
