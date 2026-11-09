#!/usr/bin/env bash
# Smoke test of a built image: starts it twice (login on / login off) and checks that the panel answers as it should.
# Usage (from the repository root):  bash scripts/smoke.sh IMAGE
#
# Every stage leaves a CI "notice" annotation and every failure a CI "error" annotation with the container logs, so the
# cause is visible on the workflow run page without opening the raw logs.
set -uo pipefail

IMAGE="${1:-}"
[ -n "$IMAGE" ] || { echo "::error title=Smoke test::usage: smoke.sh IMAGE"; exit 2; }
EXPECTED_VERSION="$(sed -n 's/^ARG APP_VERSION=//p' Dockerfile | head -1)"
[ -n "$EXPECTED_VERSION" ] || { echo "::error title=Smoke test::no ARG APP_VERSION in the Dockerfile"; exit 2; }
note() { echo "::notice title=Smoke test::$*"; }
note "image $IMAGE, expected version $EXPECTED_VERSION"

WORK="$(mktemp -d)"
chmod 777 "$WORK"
cleanup() { docker rm -f smoke-auth smoke-open >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

logs() {   # container status and the tail of both containers' logs
  { docker ps -a --format 'table {{.Names}}\t{{.Status}}'
    for c in smoke-auth smoke-open; do echo "--- $c"; docker logs --tail 40 "$c" 2>&1; done; } 2>&1 | head -120
}
fail() {   # message: error annotation + job summary with the logs, then stop
  echo "SMOKE FAILED: $*" >&2
  local l; l="$(logs)"
  echo "$l" >&2
  echo "::error title=Smoke test failed::$* | $(printf '%s' "$l" | head -30 | tr '\n' '|' | sed 's/%/%25/g')"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then { echo "### Smoke test failed: $*"; echo '```'; echo "$l"; echo '```'; } >> "$GITHUB_STEP_SUMMARY"; fi
  exit 1
}

for n in auth open; do mkdir -p "$WORK/$n/drive" "$WORK/$n/home" || fail "cannot create $WORK/$n"; chmod -R 777 "$WORK/$n"; done

# No Apple credentials, no photos, no apps: it must start and answer without ever reaching the network
COMMON=(-e TZ=UTC -e SYNC_PHOTOS=false -e SYNC_APPS=false -e SMTP_USERNAME= -e PUBLIC_URL=http://localhost)
out="$(docker run -d --name smoke-auth -p 18080:8080 "${COMMON[@]}" -e GUI_PASS='a-long-smoke-test-password' \
  -v "$WORK/auth:/data" "$IMAGE" 2>&1)" || fail "docker run (login on) failed: $out"
out="$(docker run -d --name smoke-open -p 18081:8080 "${COMMON[@]}" -e AUTH_DISABLED=true \
  -v "$WORK/open:/data" "$IMAGE" 2>&1)" || fail "docker run (login off) failed: $out"
note "both containers started"

wait_for() {   # port
  for _ in $(seq 1 40); do
    curl -fsS "http://127.0.0.1:$1/health" >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
field() { python3 -c "import sys,json; print(json.load(sys.stdin)[sys.argv[1]])" "$1"; }

wait_for 18080 || fail "login on: no answer on /health after 80 s"
wait_for 18081 || fail "login off: no answer on /health after 80 s"
note "both answer on /health"

# Login on (the default): the panel and the API require a session; /health is public and reports the right version
health="$(curl -fsS http://127.0.0.1:18080/health)" || fail "login on: /health failed"
version="$(printf '%s' "$health" | field version)" || fail "login on: /health is not JSON: $health"
[ "$version" = "$EXPECTED_VERSION" ] || fail "version is '$version', expected '$EXPECTED_VERSION'"
c="$(code http://127.0.0.1:18080/api/status)"; [ "$c" = 401 ] || fail "login on: /api/status is $c, must be 401 without a session"
c="$(code http://127.0.0.1:18080/)"; [ "$c" = 302 ] || fail "login on: / is $c, must redirect (302) to the login"
[ "$(printf '%s' "$health" | field auth_disabled)" = False ] || fail "login on: /health says auth is disabled: $health"
note "login on: OK"

# Login off (AUTH_DISABLED): the panel works from a private network without a session
c="$(code http://127.0.0.1:18081/api/status)"; [ "$c" = 200 ] || fail "login off: /api/status is $c, must be 200"
c="$(code http://127.0.0.1:18081/)"; [ "$c" = 200 ] || fail "login off: / is $c, must serve the panel (200)"
health="$(curl -fsS http://127.0.0.1:18081/health)" || fail "login off: /health failed"
[ "$(printf '%s' "$health" | field auth_disabled)" = True ] || fail "login off: /health must say auth_disabled: $health"
note "login off: OK"

echo "smoke OK (version $version)"
