#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLM_NAMESPACE="${LLM_NAMESPACE:-ms-local-llm}"
OLLAMA_STORAGE_SIZE="${OLLAMA_STORAGE_SIZE:-100Gi}"
OLLAMA_IMAGE="${OLLAMA_IMAGE:-ollama/ollama:latest}"
OPEN_WEBUI_IMAGE="${OPEN_WEBUI_IMAGE:-ghcr.io/open-webui/open-webui:main}"
DEFAULT_MODEL="${DEFAULT_MODEL:-llama3.2}"
WEBUI_AUTH="${WEBUI_AUTH:-True}"
WEBUI_SERVICE_TYPE="${WEBUI_SERVICE_TYPE:-NodePort}"
OLLAMA_CPU_REQUEST="${OLLAMA_CPU_REQUEST:-2}"
OLLAMA_CPU_LIMIT="${OLLAMA_CPU_LIMIT:-8}"
OLLAMA_MEMORY_REQUEST="${OLLAMA_MEMORY_REQUEST:-4Gi}"
OLLAMA_MEMORY_LIMIT="${OLLAMA_MEMORY_LIMIT:-16Gi}"
WEBUI_CPU_REQUEST="${WEBUI_CPU_REQUEST:-250m}"
WEBUI_CPU_LIMIT="${WEBUI_CPU_LIMIT:-1}"
WEBUI_MEMORY_REQUEST="${WEBUI_MEMORY_REQUEST:-1Gi}"
WEBUI_MEMORY_LIMIT="${WEBUI_MEMORY_LIMIT:-3Gi}"

for command_name in kubectl envsubst; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Comando obrigatório não encontrado no PATH: $command_name" >&2
    exit 1
  }
done

kubectl get nodes >/dev/null || {
  echo "Não foi possível acessar um cluster Kubernetes com o contexto atual." >&2
  exit 1
}

if [[ -z "${OLLAMA_STORAGE_CLASS:-}" ]]; then
  OLLAMA_STORAGE_CLASS="$(
    kubectl get storageclass -o go-template='{{range .items}}{{if eq (index .metadata.annotations "storageclass.kubernetes.io/is-default-class") "true"}}{{.metadata.name}}{{"\n"}}{{end}}{{end}}'
  )"
fi
if [[ -z "$OLLAMA_STORAGE_CLASS" ]]; then
  echo "Nenhuma StorageClass padrão encontrada. Defina OLLAMA_STORAGE_CLASS." >&2
  exit 1
fi

if [[ -z "${LLM_INGRESS_CLASS+x}" ]]; then
  LLM_INGRESS_CLASS="$(
    kubectl get ingressclass -o go-template='{{range .items}}{{if eq (index .metadata.annotations "ingressclass.kubernetes.io/is-default-class") "true"}}{{.metadata.name}}{{"\n"}}{{end}}{{end}}'
  )"
  if [[ -z "$LLM_INGRESS_CLASS" ]] && [[ "$(kubectl get ingressclass -o name | wc -l)" -eq 1 ]]; then
    LLM_INGRESS_CLASS="$(kubectl get ingressclass -o jsonpath='{.items[0].metadata.name}')"
  fi
fi
INGRESS_HOST="${LLM_INGRESS_HOST:-llm.local}"
INGRESS_CLASS="${LLM_INGRESS_CLASS:-}"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

export LLM_NAMESPACE OLLAMA_STORAGE_SIZE OLLAMA_STORAGE_CLASS OLLAMA_IMAGE
export OPEN_WEBUI_IMAGE DEFAULT_MODEL WEBUI_AUTH WEBUI_SERVICE_TYPE
export OLLAMA_CPU_REQUEST OLLAMA_CPU_LIMIT OLLAMA_MEMORY_REQUEST OLLAMA_MEMORY_LIMIT
export WEBUI_CPU_REQUEST WEBUI_CPU_LIMIT WEBUI_MEMORY_REQUEST WEBUI_MEMORY_LIMIT
export INGRESS_CLASS INGRESS_HOST

render_template() {
  local template="$1"
  local output="$2"
  envsubst '${LLM_NAMESPACE} ${OLLAMA_STORAGE_SIZE} ${OLLAMA_STORAGE_CLASS} ${OLLAMA_IMAGE} ${OPEN_WEBUI_IMAGE} ${DEFAULT_MODEL} ${WEBUI_AUTH} ${WEBUI_SERVICE_TYPE} ${OLLAMA_CPU_REQUEST} ${OLLAMA_CPU_LIMIT} ${OLLAMA_MEMORY_REQUEST} ${OLLAMA_MEMORY_LIMIT} ${WEBUI_CPU_REQUEST} ${WEBUI_CPU_LIMIT} ${WEBUI_MEMORY_REQUEST} ${WEBUI_MEMORY_LIMIT} ${INGRESS_CLASS} ${INGRESS_HOST}' \
    <"$template" >"$output"
}

render_template "$DIR/templates/00-namespace.yaml.tpl" "$WORK_DIR/00-namespace.yaml"
render_template "$DIR/templates/01-ollama.yaml.tpl" "$WORK_DIR/01-ollama.yaml"
render_template "$DIR/templates/02-open-webui.yaml.tpl" "$WORK_DIR/02-open-webui.yaml"
render_template "$DIR/templates/04-model-default.yaml.tpl" "$WORK_DIR/04-model-default.yaml"

kubectl apply -f "$WORK_DIR/00-namespace.yaml"
kubectl apply -n "$LLM_NAMESPACE" -f "$WORK_DIR/01-ollama.yaml"
kubectl apply -n "$LLM_NAMESPACE" -f "$WORK_DIR/02-open-webui.yaml"
kubectl apply -n "$LLM_NAMESPACE" -f "$WORK_DIR/04-model-default.yaml"

if [[ -n "$INGRESS_CLASS" && -n "$INGRESS_HOST" ]]; then
  render_template "$DIR/templates/03-ingress.yaml.tpl" "$WORK_DIR/03-ingress.yaml"
  kubectl apply -n "$LLM_NAMESPACE" -f "$WORK_DIR/03-ingress.yaml"
else
  kubectl delete ingress open-webui -n "$LLM_NAMESPACE" --ignore-not-found
  echo "Ingress ignorado; defina LLM_INGRESS_CLASS e LLM_INGRESS_HOST para habilitá-lo."
fi

kubectl rollout status deployment/ollama -n "$LLM_NAMESPACE" --timeout=300s
kubectl rollout status deployment/open-webui -n "$LLM_NAMESPACE" --timeout=900s

if [[ "${PULL_MODELS_ON_DEPLOY:-true}" == "true" ]]; then
  if ! kubectl exec -n "$LLM_NAMESPACE" deployment/ollama -- ollama list |
    awk 'NR > 1 && NF { found=1 } END { exit !found }'; then
    echo "Baixando os modelos configurados para o armazenamento persistente..."
    "$DIR/pull-models.sh"
  else
    echo "Modelos existentes preservados. Para atualizá-los, execute ./update-models.sh."
  fi
fi

echo ""
echo "Recursos ativos em $LLM_NAMESPACE:"
kubectl get pods,svc,ingress,pvc -n "$LLM_NAMESPACE"
if kubectl get ingress open-webui -n "$LLM_NAMESPACE" >/dev/null 2>&1; then
  echo "Acesso via Ingress: http://${INGRESS_HOST}"
fi
