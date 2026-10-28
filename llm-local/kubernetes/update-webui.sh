#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="${LLM_NAMESPACE:-ms-local-llm}"
OPEN_WEBUI_IMAGE="${OPEN_WEBUI_IMAGE:-ghcr.io/open-webui/open-webui:main}"
KUBECTL_IMAGE="${KUBECTL_IMAGE:-bitnami/kubectl:latest}"
MODE="${1:-manual}"

for command_name in kubectl envsubst; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Comando obrigatório não encontrado no PATH: $command_name" >&2
    exit 1
  }
done

kubectl get deployment -n "$NAMESPACE" open-webui >/dev/null 2>&1 || {
  echo "Deployment open-webui não encontrado no namespace $NAMESPACE." >&2
  exit 1
}

run_manual_update() {
  local target_image="${1:-}"

  echo "Configurando imagePullPolicy para Always no container open-webui..."
  kubectl patch deployment open-webui -n "$NAMESPACE" --type='strategic' \
    -p '{"spec":{"template":{"spec":{"containers":[{"name":"open-webui","imagePullPolicy":"Always"}]}}}}'

  if [[ -n "$target_image" ]]; then
    local full_image
    if [[ "$target_image" != *"/"* ]]; then
      full_image="ghcr.io/open-webui/open-webui:${target_image}"
    else
      full_image="$target_image"
    fi
    echo "Definindo imagem do Open WebUI para: $full_image"
    kubectl set image deployment/open-webui open-webui="$full_image" -n "$NAMESPACE"
  else
    local current_image
    current_image="$(kubectl get deployment open-webui -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.containers[0].image}')"
    echo "Reiniciando rollout para baixar a versão mais recente da imagem: $current_image"
    kubectl rollout restart deployment/open-webui -n "$NAMESPACE"
  fi

  echo "Aguardando conclusão do rollout (pode levar alguns minutos)..."
  kubectl rollout status deployment/open-webui -n "$NAMESPACE" --timeout=900s

  echo ""
  echo "Open WebUI atualizado com sucesso no namespace $NAMESPACE."
  kubectl get pods -n "$NAMESPACE" -l app=open-webui -o wide
}

case "$MODE" in
  manual|--manual)
    target=""
    if [[ "$MODE" == "--manual" ]]; then
      shift
      target="${1:-}"
    elif [[ "$#" -gt 1 && "$1" == "manual" ]]; then
      target="$2"
    fi
    run_manual_update "$target"
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

    export LLM_NAMESPACE="$NAMESPACE" UPDATE_SCHEDULE="$schedule" KUBECTL_IMAGE
    envsubst '${LLM_NAMESPACE} ${UPDATE_SCHEDULE} ${KUBECTL_IMAGE}' \
      <"$DIR/templates/webui-update-cronjob.yaml.tpl" |
      kubectl apply -f -
    echo "Atualização automática do Open WebUI configurada: $2, às 03:00 no horário do cluster."
    ;;
  --disable)
    kubectl delete cronjob open-webui-update -n "$NAMESPACE" --ignore-not-found
    kubectl delete role,rolebinding,serviceaccount open-webui-updater -n "$NAMESPACE" --ignore-not-found
    echo "Atualização automática do Open WebUI desativada."
    ;;
  --status)
    echo "=== CronJob de Atualização Agendada ==="
    kubectl get cronjob,jobs -n "$NAMESPACE" \
      -l app.kubernetes.io/name=open-webui-update 2>/dev/null || true
    echo ""
    echo "=== Status do Deployment Open WebUI ==="
    kubectl get deployment open-webui -n "$NAMESPACE" -o wide
    echo ""
    echo "=== Pods do Open WebUI ==="
    kubectl get pods -n "$NAMESPACE" -l app=open-webui -o wide
    ;;
  --help|-h)
    cat <<USAGE
Uso:
  $0 [manual] [tag|imagem]       Atualiza o Open WebUI agora
  $0 --schedule weekly|monthly   Agenda atualização automática
  $0 --disable                   Desativa a agenda automática
  $0 --status                    Mostra a agenda e status atual

Exemplos:
  $0                             Atualiza para a versão mais recente de main
  $0 --manual v0.12.4            Atualiza para uma tag específica do GitHub
  $0 v0.12.4                     Atualiza para a tag v0.12.4 diretamente
  $0 --schedule weekly           Configura atualização semanal automática
  $0 --status                    Consulta versão e agendamentos ativos
USAGE
    ;;
  -*)
    echo "Opção desconhecida: $MODE" >&2
    echo "Use $0 --help para ver as opções." >&2
    exit 2
    ;;
  *)
    run_manual_update "$MODE"
    ;;
esac
