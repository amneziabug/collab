#!/usr/bin/env bash
# One command to upload everything new in your video folder:
#   ~/github/collab/upload.sh            upload now
#   ~/github/collab/upload.sh --dry-run  preview only
set -euo pipefail

cd "$(dirname "$0")"

# Activate the Python environment (yours lives next to the project: ~/github/venv)
for venv in ../venv .venv venv; do
    if [ -f "$venv/bin/activate" ]; then
        # shellcheck disable=SC1091
        source "$venv/bin/activate"
        break
    fi
done

# Make sure Ollama is running (it writes the analysis/tags)
if ! ollama list >/dev/null 2>&1; then
    echo "Starting Ollama..."
    nohup ollama serve >/tmp/ollama.log 2>&1 &
    for _ in $(seq 1 20); do
        ollama list >/dev/null 2>&1 && break
        sleep 1
    done
fi

python3 -m shortpipe upload-now "$@"
echo
python3 -m shortpipe status --events 5
