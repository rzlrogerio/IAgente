#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/bin"
LINK_PATH="${BIN_DIR}/investigar-code"
FORCE=false

usage() {
  cat <<'EOF'
Uso: ./install.sh [--bin-dir DIR] [--force]

Opcoes:
  --bin-dir DIR  Diretorio do atalho (padrao: ~/bin)
  --force        Sobrescreve um atalho existente
  -h, --help     Mostra esta ajuda
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bin-dir)
      shift
      [[ $# -gt 0 ]] || { echo "Erro: informe o valor de --bin-dir" >&2; exit 1; }
      BIN_DIR="$1"
      LINK_PATH="$BIN_DIR/investigar-code"
      ;;
    --force) FORCE=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Erro: opcao invalida: $1" >&2; usage; exit 1 ;;
  esac
  shift
done

mkdir -p "$BIN_DIR"
chmod +x "$SCRIPT_DIR/investigar-code"

if [[ -e "$LINK_PATH" || -L "$LINK_PATH" ]]; then
  if [[ "$FORCE" == true ]]; then
    rm -f "$LINK_PATH"
  else
    echo "Aviso: ja existe $LINK_PATH" >&2
    echo "Use --force para sobrescrever." >&2
    exit 1
  fi
fi

ln -s "$SCRIPT_DIR/investigar-code" "$LINK_PATH"
echo "Instalacao concluida: $LINK_PATH -> $SCRIPT_DIR/investigar-code"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo "Adicione ao PATH do shell: export PATH=\"$BIN_DIR:\$PATH\""
fi
echo "Execute investigar-code dentro de qualquer clone Git Python."