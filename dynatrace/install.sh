#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${BIN_DIR:-${HOME}/bin}"
LINK_PATH="${BIN_DIR}/view_dyna_incidents"

usage() {
  cat <<'EOF'
Uso:
  ./install.sh [--bin-dir DIR] [--bootstrap] [--force]

Opcoes:
  --bin-dir DIR  Diretorio de binarios do usuario (padrao: ~/bin)
  --bootstrap    Executa setup inicial (instala deps na venv local do projeto)
  --force        Sobrescreve link existente
  -h, --help     Mostra esta ajuda

Descricao:
  - Garante permissoes de execucao dos scripts
  - Cria link simbolico view_dyna_incidents em ~/bin (ou DIR informado)
  - Cria .env a partir de .env.example caso nao exista
EOF
}

BOOTSTRAP="false"
FORCE="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bin-dir)
      shift
      [[ $# -gt 0 ]] || { echo "Erro: informe o valor de --bin-dir" >&2; exit 1; }
      BIN_DIR="$1"
      LINK_PATH="${BIN_DIR}/view_dyna_incidents"
      ;;
    --bootstrap)
      BOOTSTRAP="true"
      ;;
    --force)
      FORCE="true"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Erro: opcao invalida: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "Erro: python3 nao encontrado no PATH" >&2
  exit 1
fi

mkdir -p "$BIN_DIR"
chmod +x "$SCRIPT_DIR/view_dyna_incidents"
[[ -f "$SCRIPT_DIR/setup.sh" ]] && chmod +x "$SCRIPT_DIR/setup.sh"

if [[ -e "$LINK_PATH" || -L "$LINK_PATH" ]]; then
  if [[ "$FORCE" == "true" ]]; then
    rm -f "$LINK_PATH"
  else
    echo "Aviso: ja existe $LINK_PATH" >&2
    echo "Use --force para sobrescrever." >&2
    exit 1
  fi
fi

ln -s "$SCRIPT_DIR/view_dyna_incidents" "$LINK_PATH"

if [[ -f "$SCRIPT_DIR/.env.example" && ! -f "$SCRIPT_DIR/.env" ]]; then
  cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
  chmod 600 "$SCRIPT_DIR/.env"
fi

if [[ -f "$SCRIPT_DIR/config.ini.example" && ! -f "$SCRIPT_DIR/config.ini" ]]; then
  cp "$SCRIPT_DIR/config.ini.example" "$SCRIPT_DIR/config.ini"
  echo "Criado config.ini a partir de config.ini.example"
fi

if [[ "$BOOTSTRAP" == "true" ]]; then
  if [[ -x "$SCRIPT_DIR/setup.sh" ]]; then
    "$SCRIPT_DIR/setup.sh"
  else
    python3 -m venv "$SCRIPT_DIR/.venv"
    "$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"
  fi
fi

echo "Instalacao concluida."
echo "Link criado: $LINK_PATH -> $SCRIPT_DIR/view_dyna_incidents"
echo "Se ~/bin nao estiver no PATH, adicione no shell profile:"
echo '  export PATH="$HOME/bin:$PATH"'
echo ""
echo "Uso: view_dyna_incidents \"cdc\""
