#!/usr/bin/env bash
set -euo pipefail

MODEL="qwen2.5-coder:1.5b"

if ! command -v ollama >/dev/null 2>&1; then
  echo "ERROR: ollama is not installed or is not in PATH." >&2
  exit 1
fi

echo "Pulling ${MODEL}..."
ollama pull "${MODEL}"

echo
echo "Installed Ollama models:"
ollama list

echo
echo "Testing local Ollama API..."
curl -fsS http://127.0.0.1:11434/api/tags >/dev/null
echo "Ollama API is responding."
echo "Ready: ${MODEL}"
