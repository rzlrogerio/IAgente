#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

cd "$ROOT_DIR"

echo "[1/3] Criando venv em $VENV_DIR (se necessario)..."
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi

echo "[2/3] Ativando venv..."
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "[3/3] Instalando dependencias com pip..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo
echo "Ambiente pronto."
echo "Para usar no terminal atual, execute:"
echo "  source .venv/bin/activate"
echo "Depois rode:"
echo "  python dynatrace_incidents_tool.py --query \"rds\""
