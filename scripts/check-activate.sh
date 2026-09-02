#!/usr/bin/env bash
# Prove `bin/plow-activate` against a Plow stack running on this machine.
#
# The activation handshake needs a human with a phone. A local stack stands in
# for the phone network with a twin that accepts inbound messages over HTTP, so
# this script plays that part: it starts the tool, reads the code off its
# stderr, sends it through the twin, and then checks what came back -- that the
# credential file carries the two lines and nothing else, at mode 600, and that
# the token in it reaches this agent's chats and nothing else on the account.
#
# usage: scripts/check-activate.sh
#
#   TWIN_BASE       required: the twin that delivers an inbound message
#   PLOW_API_BASE   how the AGENT will reach the API   (default http://api:8000)
#   PLOW_API_HOST   how THIS SHELL reaches the same API (default http://localhost:8000)
#   TIMEOUT         seconds to wait for the redeem (default 300)
set -euo pipefail

cd "$(dirname "$0")/.."

api_base="${PLOW_API_BASE:-http://api:8000}"
api_host="${PLOW_API_HOST:-http://localhost:8000}"
timeout="${TIMEOUT:-300}"
twin="${TWIN_BASE:-}"
[ -n "$twin" ] || { echo "set TWIN_BASE to the stack's inbound-message twin" >&2; exit 2; }
# Unused, and outside any pool the twin manages: a number that already holds a
# chat on the assigned line sends the activation down the reassignment path,
# which is a different thing to test than this.
member="${MEMBER_PHONE:-+1415555$((RANDOM % 9000 + 1000))}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

bin/plow-activate --api-base "$api_base" --contact-base "$api_host" \
  --out "$work/plow-credentials" --name check-activate \
  --poll-interval 3 --timeout "$timeout" >"$work/out" 2>"$work/err" &
tool=$!

# The code and the number it has to reach are on stderr, by design. Wait for
# the line rather than sleeping a guessed interval.
for _ in $(seq 60); do
  line="$(grep -o 'Plow Activate: [A-Z0-9]* *to *+[0-9]*' "$work/err" || true)"
  [ -n "$line" ] && break
  sleep 1
done
[ -n "${line:-}" ] || { echo "FAIL: the tool printed no activation code" >&2; cat "$work/err" >&2; exit 1; }
code="$(echo "$line" | sed -E 's/Plow Activate: ([A-Z0-9]*).*/\1/')"
send_to="$(echo "$line" | sed -E 's/.*(\+[0-9]+)$/\1/')"
echo "sending '$code' to $send_to as $member" >&2

curl -fsS -X POST -H 'Content-Type: application/json' \
  -d "{\"to_phone\":\"$send_to\",\"remote_phone\":\"$member\",\"text\":\"Plow Activate: $code\"}" \
  "$twin/ui/inbound" >/dev/null

wait "$tool" || { echo "FAIL: plow-activate exited non-zero" >&2; cat "$work/err" >&2; exit 1; }
cat "$work/err" >&2

# Nothing on stdout: the token goes to a file this tool creates at 600, and a
# tool that also printed it would have handed it to every terminal log.
[ ! -s "$work/out" ] || { echo "FAIL: the tool wrote to stdout" >&2; cat "$work/out" >&2; exit 1; }

mode="$(stat -f '%OLp' "$work/plow-credentials" 2>/dev/null || stat -c '%a' "$work/plow-credentials")"
[ "$mode" = 600 ] || { echo "FAIL: the credential file is mode $mode, not 600" >&2; exit 1; }

# Exactly the two names the image accepts. A third would be refused at boot, so
# a tool that wrote one would write a credential that cannot be used.
names="$(grep -v '^[[:space:]]*\(#\|$\)' "$work/plow-credentials" | sed 's/=.*//' | sort | tr '\n' ' ')"
[ "$names" = "PLOW_AGENT_TOKEN PLOW_API_BASE " ] \
  || { echo "FAIL: the credential file names '$names'" >&2; exit 1; }
echo "ok: mode 600, PLOW_API_BASE and PLOW_AGENT_TOKEN, nothing on stdout" >&2

token="$(sed -n 's/^PLOW_AGENT_TOKEN=//p' "$work/plow-credentials")"
home="$(sed -n 's/^ *home: *//p' "$work/err")"
[ "$(sed -n 's/^PLOW_API_BASE=//p' "$work/plow-credentials")" = "$api_base" ] \
  || { echo "FAIL: the file carries the address this script used, not the agent's" >&2; exit 1; }
case "$api_base" in */v1) echo "FAIL: PLOW_API_BASE carries a /v1 suffix" >&2; exit 1 ;; esac

# The header in a file, never in argv: `$work` is a mktemp -d, so 0700, and a
# curl command line is readable from the process table by every account on the
# machine. This token can read and send the agent's chats and spend its
# inference.
printf 'Authorization: Bearer %s\n' "$token" > "$work/auth"
chmod 600 "$work/auth"

# `000` is curl's answer when it never got one -- an unresolvable host, a
# refused connection. Reported as itself, because "the token can still list
# account keys" is a claim about the credential, and a request that never left
# the machine says nothing about it either way.
status() {
  code="$(curl -sS -o /dev/null -w '%{http_code}' -H @"$work/auth" "$api_host$1" || true)"
  case "$code" in
    ''|000) echo "FAIL: no response from $api_host$1 -- the check proved nothing" >&2; return 1 ;;
  esac
  printf '%s' "$code"
}

# The credential is narrowed, proven by what it can no longer do. An
# account-wide token -- what activation hands back before the tool narrows
# it -- answers 200 on both of these.
keys_code="$(status /v1/api-keys)" || exit 1
[ "$keys_code" = 403 ] || { echo "FAIL: the token can still list account keys (HTTP $keys_code)" >&2; exit 1; }
chat_code="$(status /v1/chats/$home/messages?limit=1)" || exit 1
[ "$chat_code" = 200 ] || { echo "FAIL: the token cannot read its own chat (HTTP $chat_code)" >&2; exit 1; }
echo "ok: keys:manage refused, its own chat readable" >&2

# ...and by what it is scoped to: the line of the chat it was actually given,
# which is not always the line the activation was assigned.
granted="$(sed -n 's/^  chats:  //p' "$work/err")"
chat_line="line:$(curl -fsS -H @"$work/auth" "$api_host/v1/chats/$home" \
  | python3 -c 'import json,sys
chat = json.load(sys.stdin)
agents = [p for p in chat["participants"] if p.get("type") == "agent"]
mine = [p for p in agents if p.get("relationship") == "self"] or agents
print(mine[0]["line"]["uid"])')"
[ "$granted" = "$chat_line" ] || { echo "FAIL: granted $granted but the chat is on $chat_line" >&2; exit 1; }
echo "ok: granted exactly $granted, the line the provisioned chat is on" >&2

echo "ok: plow-activate mints, narrows and proves a Plow credential for $api_base" >&2
