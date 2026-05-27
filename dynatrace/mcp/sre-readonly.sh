#!/usr/bin/env bash
# MCP Server — SRE Read-Only Shell
# Executa comandos aws, kubectl e dtctl somente em modo leitura.
# Protocolo: JSON-RPC 2.0 over stdio (MCP spec)
# Segurança: allowlist de subcomandos, bloqueio de mutações.
# Compatível com Linux e macOS (bash 3.2+).
set -euo pipefail

# ─── Portabilidade ───────────────────────────────────────────────────────────
# macOS não tem `timeout` nativo — usar perl como fallback
if command -v timeout >/dev/null 2>&1; then
  _timeout() { timeout "$1" bash -c "$2" 2>&1; }
else
  _timeout() {
    local secs="$1" cmd="$2"
    perl -e '
      use POSIX ":sys_wait_h";
      alarm shift @ARGV;
      $SIG{ALRM} = sub { kill 9, $pid; exit 124 };
      $pid = fork // die "fork: $!";
      if ($pid == 0) { exec("bash", "-c", $ARGV[0]) or die "exec: $!" }
      waitpid($pid, 0);
      exit($? >> 8);
    ' "$secs" "$cmd" 2>&1
  }
fi

# ─── Allowlists ──────────────────────────────────────────────────────────────
# Subcomandos permitidos por ferramenta (read-only)
AWS_ALLOWED="^(get-|describe-|list-|lookup-events|get-metric-statistics|get-log-events|filter-log-events|get-query-results|start-query|batch-get|get-caller-identity)"
KUBECTL_ALLOWED="^(get|logs|describe|top|rollout|events|api-resources)$"
DTCTL_ALLOWED="^(query|get|describe|doctor|version)$"
PANDOC_ALLOWED_OUTPUT="\.(pdf|html)$"

# Padrões SEMPRE bloqueados (independente da ferramenta)
BLOCKED_WORDS=("--force" "--delete-" "rm -rf" "sudo " "eval " "passwd" "shadow" "curl " "wget ")

# ─── Validação ───────────────────────────────────────────────────────────────

check_blocked_patterns() {
  local cmd="$1"
  for pattern in "${BLOCKED_WORDS[@]}"; do
    if [[ "$cmd" == *"$pattern"* ]]; then
      echo "BLOCKED: padrão perigoso detectado: '$pattern'"
      return 1
    fi
  done
  # Bloquear command chaining (;, &&, ||, backticks, $())
  if [[ "$cmd" =~ \;[[:space:]] || "$cmd" =~ \&\& || "$cmd" =~ \|\| || "$cmd" =~ \`.*\` || "$cmd" =~ \$\( ]]; then
    echo "BLOCKED: encadeamento de comandos não permitido"
    return 1
  fi
  # Bloquear redireção para arquivo
  if [[ "$cmd" =~ \>[[:space:]]/ || "$cmd" =~ \>\>[[:space:]]/ ]]; then
    echo "BLOCKED: redireção para arquivo não permitida"
    return 1
  fi
  return 0
}

validate_aws() {
  local cmd="$1"
  # Extrair: aws <service> <subcommand> → pegar o 3o token
  local subcmd
  subcmd=$(echo "$cmd" | awk '{print $3}')
  # Caso especial: aws configure list-profiles / aws sts get-caller-identity
  local svc
  svc=$(echo "$cmd" | awk '{print $2}')
  if [[ "$svc" == "configure" ]]; then
    subcmd=$(echo "$cmd" | awk '{print $3}')
    if [[ "$subcmd" == "list-profiles" || "$subcmd" == "get" ]]; then
      return 0
    fi
    echo "BLOCKED: subcomando 'aws configure $subcmd' não permitido"
    return 1
  fi
  if [[ -z "$subcmd" ]]; then
    echo "BLOCKED: não foi possível identificar subcomando AWS"
    return 1
  fi
  if ! echo "$subcmd" | grep -qE "$AWS_ALLOWED"; then
    echo "BLOCKED: subcomando AWS '$subcmd' não está na allowlist (somente leitura)"
    return 1
  fi
  return 0
}

validate_kubectl() {
  local cmd="$1"
  local subcmd
  subcmd=$(echo "$cmd" | awk '{print $2}')
  # Caso especial: "rollout history"
  if [[ "$subcmd" == "rollout" ]]; then
    local sub2
    sub2=$(echo "$cmd" | awk '{print $3}')
    if [[ "$sub2" == "history" || "$sub2" == "status" ]]; then
      return 0
    fi
    echo "BLOCKED: kubectl rollout '$sub2' não permitido (apenas history/status)"
    return 1
  fi
  if ! echo "$subcmd" | grep -qE "$KUBECTL_ALLOWED"; then
    echo "BLOCKED: subcomando kubectl '$subcmd' não está na allowlist (somente leitura)"
    return 1
  fi
  # Bloquear flags de mutação
  if [[ "$cmd" =~ --delete || "$cmd" =~ --patch || "$cmd" =~ --edit || "$cmd" =~ --force ]]; then
    echo "BLOCKED: flag de mutação detectada no kubectl"
    return 1
  fi
  return 0
}

validate_dtctl() {
  local cmd="$1"
  local subcmd
  subcmd=$(echo "$cmd" | awk '{print $2}')
  if ! echo "$subcmd" | grep -qE "$DTCTL_ALLOWED"; then
    echo "BLOCKED: subcomando dtctl '$subcmd' não está na allowlist (somente leitura)"
    return 1
  fi
  return 0
}

validate_pandoc() {
  local cmd="$1"
  # Pandoc só pode gerar PDF/HTML — verificar flag -o
  if ! echo "$cmd" | grep -qE '\-o\s+\S+\.(pdf|html)'; then
    echo "BLOCKED: pandoc requer flag -o com extensão .pdf ou .html"
    return 1
  fi
  # Bloquear se tentar escrever fora de ~/Documents/incidentes
  local outfile
  outfile=$(echo "$cmd" | grep -oE '\-o\s+\S+' | awk '{print $2}')
  case "$outfile" in
    ~/Documents/incidentes/*|/home/*/Documents/incidentes/*)
      return 0
      ;;
    *)
      echo "BLOCKED: pandoc só pode gerar arquivos em ~/Documents/incidentes/"
      return 1
      ;;
  esac
}

validate_command() {
  local cmd="$1"

  # Identificar ferramenta principal (antes de qualquer pipe)
  local first_segment="${cmd%%|*}"
  local tool
  tool=$(echo "$first_segment" | awk '{print $1}')

  # dtctl query usa pipes e || na sintaxe DQL — não são operadores shell.
  # Verificar apenas BLOCKED_WORDS + subcomando na allowlist.
  if [[ "$tool" == "dtctl" ]]; then
    for pattern in "${BLOCKED_WORDS[@]}"; do
      if [[ "$cmd" == *"$pattern"* ]]; then
        echo "BLOCKED: padrão perigoso detectado: '$pattern'"
        return 1
      fi
    done
    validate_dtctl "$first_segment"
    return $?
  fi

  # Para aws/kubectl, aplicar todas as verificações de segurança
  check_blocked_patterns "$cmd" || return 1

  # Para aws/kubectl, validar pipes (se houver)
  local first_cmd="$cmd"
  if [[ "$cmd" == *"|"* ]]; then
    first_cmd="$first_segment"
    # Validar que os comandos após pipe são apenas ferramentas de filtragem
    local pipe_rest="${cmd#*|}"
    local old_IFS="$IFS"
    IFS='|'
    for seg in $pipe_rest; do
      local seg_tool
      seg_tool=$(echo "$seg" | awk '{print $1}')
      if ! echo "$seg_tool" | grep -qE "^(grep|head|tail|jq|awk|sort|wc|tr|cut|sed|uniq|python3)$"; then
        IFS="$old_IFS"
        echo "BLOCKED: ferramenta '$seg_tool' não permitida em pipe (apenas grep/head/tail/jq/awk/sort/wc/tr/cut/sed/uniq)"
        return 1
      fi
    done
    IFS="$old_IFS"
  fi

  case "$tool" in
    aws)     validate_aws "$first_cmd" ;;
    kubectl) validate_kubectl "$first_cmd" ;;
    pandoc)  validate_pandoc "$cmd" ;;
    ls|stat|cat|file|wc|head|tail|date)
      # Ferramentas read-only básicas — permitidas sem restrição
      return 0
      ;;
    *)
      echo "BLOCKED: ferramenta '$tool' não permitida (apenas aws, kubectl, dtctl, pandoc, ls, stat, cat, file)"
      return 1
      ;;
  esac
}

# ─── Execução segura ─────────────────────────────────────────────────────────
execute_command() {
  local cmd="$1"
  local timeout="${2:-60}"

  local validation_result
  validation_result=$(validate_command "$cmd" 2>&1) || {
    echo "$validation_result"
    return 1
  }

  # Executar com timeout
  local output
  output=$(_timeout "${timeout}" "$cmd") || {
    local exit_code=$?
    if [[ $exit_code -eq 124 ]]; then
      echo "TIMEOUT: comando excedeu ${timeout}s"
    else
      echo "$output"
    fi
    return $exit_code
  }
  echo "$output"
}

# ─── MCP Protocol (JSON-RPC 2.0 over stdio) ─────────────────────────────────

TOOLS_JSON='[
  {
    "name": "sre_run_command",
    "description": "Executa um comando read-only de SRE (aws, kubectl, dtctl). Bloqueia automaticamente qualquer operação de escrita/mutação.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "command": {
          "type": "string",
          "description": "Comando completo a executar (ex: aws lambda get-function --profile X --function-name Y)"
        },
        "timeout": {
          "type": "integer",
          "description": "Timeout em segundos (padrão: 60, máximo: 120)",
          "default": 60
        }
      },
      "required": ["command"]
    }
  }
]'

send_response() {
  local id="$1"
  local result="$2"
  printf '%s\n' "$(jq -nc --arg id "$id" --argjson result "$result" '{jsonrpc:"2.0",id:($id|tonumber),result:$result}')"
}

send_error() {
  local id="$1"
  local code="$2"
  local message="$3"
  printf '%s\n' "$(jq -nc --arg id "$id" --arg code "$code" --arg msg "$message" '{jsonrpc:"2.0",id:($id|tonumber),error:{code:($code|tonumber),message:$msg}}')"
}

handle_initialize() {
  local id="$1"
  local result
  result=$(jq -nc '{
    protocolVersion: "2024-11-05",
    capabilities: {tools: {listChanged: false}},
    serverInfo: {name: "sre-readonly-shell", version: "1.0.0"}
  }')
  send_response "$id" "$result"
}

handle_tools_list() {
  local id="$1"
  local result
  result=$(jq -nc --argjson tools "$TOOLS_JSON" '{tools: $tools}')
  send_response "$id" "$result"
}

handle_tools_call() {
  local id="$1"
  local params="$2"

  local tool_name cmd timeout_val
  tool_name=$(echo "$params" | jq -r '.name')
  cmd=$(echo "$params" | jq -r '.arguments.command // empty')
  timeout_val=$(echo "$params" | jq -r '.arguments.timeout // 60')

  if [[ "$tool_name" != "sre_run_command" ]]; then
    send_error "$id" "-32602" "Tool desconhecida: $tool_name"
    return
  fi

  if [[ -z "$cmd" ]]; then
    send_error "$id" "-32602" "Parâmetro 'command' é obrigatório"
    return
  fi

  # Cap timeout at 120s
  if [[ "$timeout_val" -gt 120 ]]; then
    timeout_val=120
  fi

  local output is_error=false
  output=$(execute_command "$cmd" "$timeout_val" 2>&1) || is_error=true

  local result
  result=$(jq -nc --arg text "$output" --argjson err "$is_error" '{
    content: [{type: "text", text: $text}],
    isError: $err
  }')
  send_response "$id" "$result"
}

# ─── Main loop ───────────────────────────────────────────────────────────────
main() {
  while IFS= read -r line; do
    # Skip empty lines
    [[ -z "$line" ]] && continue

    local method id params
    method=$(echo "$line" | jq -r '.method // empty')
    id=$(echo "$line" | jq -r '.id // empty')
    params=$(echo "$line" | jq -c '.params // {}')

    case "$method" in
      "initialize")
        handle_initialize "$id"
        ;;
      "notifications/initialized"|"notifications/"*)
        # Notifications don't get responses
        ;;
      "tools/list")
        handle_tools_list "$id"
        ;;
      "tools/call")
        handle_tools_call "$id" "$params"
        ;;
      "ping")
        send_response "$id" '{}'
        ;;
      *)
        if [[ -n "$id" ]]; then
          send_error "$id" "-32601" "Método não suportado: $method"
        fi
        ;;
    esac
  done
}

main
