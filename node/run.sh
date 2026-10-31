#!/usr/bin/env bash
# The Finalist Node, in this terminal (Raahat, 30 Oct 2026: a script to see and kill, not a service).
# Logs print here; Ctrl+C stops it, and the voice server it started. Finalist's Claude stations
# need it running; the cards' clock and drill don't.
cd "$(dirname "$(readlink -f "$0")")"
exec python3 -u -m finalist_node "$@"
