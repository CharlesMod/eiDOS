#!/usr/bin/env bash
# remote_poller.sh — Pull-based health checker for Kairos nodes.
#
# Runs on the OPERATOR'S machine, NOT on any Kairos node.
# Loops through Tailscale IPs and curls /api/ping.
#
# Usage:
#   ./remote_poller.sh                  # one-shot status table
#   watch -n 300 ./remote_poller.sh     # poll every 5 minutes

set -euo pipefail

# --- Configure your nodes here ---
NODES=(
    # "name:tailscale_ip"
    "node-alpha:100.64.0.1"
    # "node-bravo:100.64.0.2"
)
PORT=8099
TIMEOUT=10

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

printf "\n%-15s %-6s %-6s %-8s %-8s %-8s %-10s %s\n" \
    "NODE" "OK" "TICK" "LEVEL" "MOOD" "TEMP" "FAILURES" "UPTIME"
printf '%0.s─' {1..75}
printf '\n'

for entry in "${NODES[@]}"; do
    name="${entry%%:*}"
    ip="${entry##*:}"

    response=$(curl -s --connect-timeout "$TIMEOUT" \
        "http://${ip}:${PORT}/api/ping" 2>/dev/null) || response=""

    if [ -z "$response" ]; then
        printf "${RED}%-15s %-6s%s\n" "$name" "DOWN" " (unreachable)${NC}"
        continue
    fi

    ok=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('ok','?'))" 2>/dev/null || echo "?")
    tick=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tick','?'))" 2>/dev/null || echo "?")
    level=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('level','?'))" 2>/dev/null || echo "?")
    mood=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('mood','?'))" 2>/dev/null || echo "?")
    temp=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); t=d.get('temp_c'); print(f'{t}°C' if t else 'n/a')" 2>/dev/null || echo "?")
    failures=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('failures',0))" 2>/dev/null || echo "?")
    uptime=$(echo "$response" | python3 -c "
import sys,json
d=json.load(sys.stdin)
s=d.get('uptime_s',0)
if s>86400: print(f'{s//86400}d {(s%86400)//3600}h')
elif s>3600: print(f'{s//3600}h {(s%3600)//60}m')
else: print(f'{s//60}m')
" 2>/dev/null || echo "?")

    if [ "$ok" = "True" ]; then
        color="$GREEN"
        ok_str="OK"
    else
        color="$RED"
        ok_str="FAIL"
    fi

    printf "${color}%-15s %-6s %-6s %-8s %-8s %-8s %-10s %s${NC}\n" \
        "$name" "$ok_str" "$tick" "Lv.$level" "$mood" "$temp" "$failures" "$uptime"
done

printf '\n'
