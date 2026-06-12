#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-${ROOT_DIR}/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BTC_PLUGIN_DIR="${ROOT_DIR}/rf_platform/plugins/bluetooth-classic"
BTC_BUILD_DIR="${BTC_PLUGIN_DIR}/build"
BLE_PLUGIN_DIR="${ROOT_DIR}/rf_platform/plugins/bluetooth-lowenergy"
ZIGBEE_PLUGIN_DIR="${ROOT_DIR}/rf_platform/plugins/zigbee-802154"

echo "[RF Sentinel] root: ${ROOT_DIR}"
echo "[RF Sentinel] host arch: $(uname -m)"

if ! command -v cmake >/dev/null 2>&1; then
  echo "error: cmake is required to build rf_platform/plugins/bluetooth-classic" >&2
  exit 1
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "error: ${PYTHON_BIN} not found" >&2
  exit 1
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[RF Sentinel] creating venv: ${VENV_DIR}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

echo "[RF Sentinel] installing Python requirements"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install -r "${ROOT_DIR}/requirements.txt"
"${VENV_DIR}/bin/python" -m pip install -e "${BTC_PLUGIN_DIR}"
"${VENV_DIR}/bin/python" -m pip install -e "${BLE_PLUGIN_DIR}"
"${VENV_DIR}/bin/python" -m pip install -e "${ZIGBEE_PLUGIN_DIR}"

echo "[RF Sentinel] rebuilding Bluetooth Classic plugin for native arch"
rm -rf "${BTC_BUILD_DIR}"
cmake -S "${BTC_PLUGIN_DIR}" -B "${BTC_BUILD_DIR}"
cmake --build "${BTC_BUILD_DIR}" --parallel "${BTC_BUILD_JOBS:-$(nproc 2>/dev/null || echo 2)}"

BTC_BINARY="${BTC_BUILD_DIR}/btcexplorer-sniffer"
if [[ ! -x "${BTC_BINARY}" ]]; then
  echo "error: expected binary missing or not executable: ${BTC_BINARY}" >&2
  exit 1
fi

if command -v file >/dev/null 2>&1; then
  echo "[RF Sentinel] built binary: $(file -b "${BTC_BINARY}")"
fi

echo
echo "Install complete."
echo "Run:"
echo "  source \"${VENV_DIR}/bin/activate\""
echo "  python3 \"${ROOT_DIR}/ui/backend/app.py\""
