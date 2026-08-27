#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if ! command -v gcc >/dev/null 2>&1; then
  echo "[WARN] gcc not found. The controller will use its built-in extractor."
  exit 0
fi
echo "[INFO] Building C FPGA signal extractor..."
gcc -O2 -std=c99 -Wall -Wextra fpga_signal_extractor.c -o fpga_signal_extractor
chmod +x fpga_signal_extractor
echo "[OK] C extractor ready: ./fpga_signal_extractor"
