#!/usr/bin/env bash
set -euo pipefail

KENERGY_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

usage() {
    echo "Usage: kenergy <command> [args]"
    echo ""
    echo "Commands:"
    echo "  watch [top] [interval_ms]   Start collecting energy metrics (default: top 20, 5000ms)"
    echo "  analyze [YYYY-MM-DD]        Analyze a day's energy log (default: today)"
    exit 1
}

cmd_watch() {
    local top="${1:-20}"
    local interval="${2:-5000}"
    echo "To collect accurate energy information we need to run with sudo:"
    echo "  sudo powermetrics --show-process-energy -i $interval"
    echo ""
    sudo powermetrics --show-process-energy -i "$interval" \
        | python3 "$KENERGY_DIR/parse-powermetrics.py" --top "$top"
}

cmd_analyze() {
    local date_str="${1:-$(date +%Y-%m-%d)}"
    python3 "$KENERGY_DIR/analyze-power.py" "$date_str"
}

if [[ $# -lt 1 ]]; then
    usage
fi

command="$1"
shift

case "$command" in
    watch)   cmd_watch "$@" ;;
    analyze) cmd_analyze "$@" ;;
    *)       usage ;;
esac
