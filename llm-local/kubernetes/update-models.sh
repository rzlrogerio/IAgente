#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="${LLM_NAMESPACE:-ms-local-llm}"
OLLAMA_IMAGE="${OLLAMA_IMAGE:-ollama/ollama:latest}"
MODE="${1:-manual}"

for command_name in kubectl envsubst; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Comando obrigatório não encontrado no PATH: $command_name" >&2
    exit 1
  }
done

case "$MODE" in
  manual|--manual)
    if (($#)); then
      shift
    fi
    "$DIR/pull-models.sh" "$@"
    ;;
  --schedule)
    [[ "$#" -eq 2 ]] || {
      echo "Uso: $0 --schedule weekly|monthly" >&2
      exit 2
    }
    case "$2" in
      weekly) schedule="0 3 * * 0" ;;
      monthly) schedule="0 3 1 * *" ;;
      *)
        echo "Periodicidade inválida: $2 (use weekly ou monthly)." >&2
        exit 2
        ;;
    esac

    kubectl get deployment -n "$NAMESPACE" ollama >/dev/null || {
      echo "Deployment ollama não encontrado no namespace $NAMESPACE." >&2
      exit 1
    }
    kubectl create configmap ollama-update-models \
      --namespace "$NAMESPACE" \
      --from-file=models.txt="$DIR/models.txt" \
      --dry-run=client -o yaml | kubectl apply -f -

    export LLM_NAMESPACE="$NAMESPACE" OLLAMA_IMAGE UPDATE_SCHEDULE="$schedule"
    envsubst '${LLM_NAMESPACE} ${OLLAMA_IMAGE} ${UPDATE_SCHEDULE}' \
      <"$DIR/templates/model-update-cronjob.yaml.tpl" |
      kubectl apply -f -
    echo "Atualização automática configurada: $2, às 03:00 no horário do cluster."
    ;;
  --disable)
    kubectl delete cronjob ollama-model-update -n "$NAMESPACE" --ignore-not-found
    ;;
  --status)
    kubectl get cronjob,jobs -n "$NAMESPACE" \
      -l app.kubernetes.io/name=ollama-model-update
    ;;
  --help|-h)
    cat <<USAGE
Uso:
  $0 [manual] [modelo ...]       Atualiza os modelos agora
  $0 --schedule weekly|monthly   Agenda atualização automática
  $0 --disable                   Desativa a agenda automática
  $0 --status                    Mostra a agenda e seus Jobs

Sem modelos informados no modo manual, usa models.txt.
USAGE
    ;;
  *)
    echo "Opção desconhecida: $MODE" >&2
    echo "Use $0 --help para ver as opções." >&2
    exit 2
    ;;
esac
