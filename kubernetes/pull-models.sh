#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="${LLM_NAMESPACE:-ms-local-llm}"

command -v kubectl >/dev/null 2>&1 || {
  echo "kubectl não encontrado no PATH." >&2
  exit 1
}

kubectl get deployment -n "$NAMESPACE" ollama >/dev/null 2>&1 || {
  echo "Deployment ollama não encontrado no namespace $NAMESPACE." >&2
  exit 1
}

if (($#)); then
  MODELS=("$@")
else
  mapfile -t MODELS < <(grep -Ev '^[[:space:]]*(#|$)' "$DIR/models.txt")
fi

if ((${#MODELS[@]} == 0)); then
  echo "Nenhum modelo configurado em $DIR/models.txt." >&2
  exit 1
fi

for model in "${MODELS[@]}"; do
  echo "Baixando/atualizando modelo: $model"
  kubectl exec -n "$NAMESPACE" deploy/ollama -- ollama pull "$model"
  echo "---"
done

echo "Todos os modelos selecionados foram baixados/atualizados com sucesso."
