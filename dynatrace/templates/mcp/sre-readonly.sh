#!/usr/bin/env bash
# MCP Server — SRE Read-Only Shell
# Executa comandos aws, az e dtctl somente em modo leitura.
# Protocolo: JSON-RPC 2.0 over stdio (MCP spec)
# Segurança: allowlist de subcomandos, bloqueio de mutações.
# Compatível com Linux e macOS (bash 3.2+).
set -uo pipefail

# Ignorar SIGPIPE — evita morte prematura se VS Code fechar conexão
trap '' PIPE

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
DTCTL_ALLOWED="^(query|get|describe|doctor|version)$"
AZ_ALLOWED="^(show|list|get-credentials|get-upgrades|get-versions)$"
AZ_SERVICES_ALLOWED="^(aks|monitor|resource|network|vm|vmss|account|graph)$"
AZ_BLOCKED_SUBCMDS="^(create|delete|update|start|stop|restart|scale|upgrade|rotate-certs|run-command)$"
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

validate_az() {
  local cmd="$1"
  # az <service> [subgroup] <action> [args...]
  local svc subcmd action
  svc=$(echo "$cmd" | awk '{print $2}')
  subcmd=$(echo "$cmd" | awk '{print $3}')
  action=$(echo "$cmd" | awk '{print $4}')

  # Caso especial: az account show / az account list
  if [[ "$svc" == "account" ]]; then
    if [[ "$subcmd" == "show" || "$subcmd" == "list" || "$subcmd" == "list-locations" ]]; then
      return 0
    fi
    echo "BLOCKED: subcomando 'az account $subcmd' não permitido (somente show/list)"
    return 1
  fi

  # Caso especial: az monitor (activity-log list, metrics list, log-analytics query)
  if [[ "$svc" == "monitor" ]]; then
    if [[ "$subcmd" == "activity-log" || "$subcmd" == "metrics" || "$subcmd" == "log-analytics" || "$subcmd" == "diagnostic-settings" ]]; then
      if [[ "$action" == "list" || "$action" == "show" || "$action" == "query" ]]; then
        return 0
      fi
      echo "BLOCKED: 'az monitor $subcmd $action' não permitido (somente list/show/query)"
      return 1
    fi
    echo "BLOCKED: 'az monitor $subcmd' não permitido"
    return 1
  fi

  # Verificar que o serviço está na lista permitida
  if ! echo "$svc" | grep -qE "$AZ_SERVICES_ALLOWED"; then
    echo "BLOCKED: serviço Azure '$svc' não está na allowlist (somente: aks, monitor, resource, network, vm, vmss, account, graph)"
    return 1
  fi

  # Determinar a ação efetiva: pode ser $subcmd direto ou $action (se $subcmd for sub-grupo)
  # Sub-grupos conhecidos: nodepool, addon, command, mesh (para aks); nsg, vnet, public-ip (para network)
  local effective_action="$subcmd"
  local known_subgroups="nodepool|addon|command|mesh|nsg|vnet|public-ip|rule|subnet|nic|lb"
  if echo "$subcmd" | grep -qE "^($known_subgroups)$"; then
    effective_action="$action"
  fi

  # Bloquear ações de escrita/mutação
  if echo "$effective_action" | grep -qE "$AZ_BLOCKED_SUBCMDS"; then
    echo "BLOCKED: ação Azure '$effective_action' é de escrita/mutação (não permitido)"
    return 1
  fi

  # Ação efetiva deve ser read-only
  if ! echo "$effective_action" | grep -qE "$AZ_ALLOWED"; then
    echo "BLOCKED: ação Azure '$effective_action' não está na allowlist (somente: show, list, get-credentials, get-upgrades, get-versions)"
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

  # Para aws, aplicar todas as verificações de segurança
  check_blocked_patterns "$cmd" || return 1

  # Para aws, validar pipes (se houver)
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
    az)      validate_az "$first_cmd" ;;
    kubectl)
      echo "BLOCKED: kubectl não permitido. Use dtctl para dados K8s via Dynatrace DQL."
      return 1
      ;;
    pandoc)  validate_pandoc "$cmd" ;;
    ls|stat|cat|file|wc|head|tail|date)
      # Ferramentas read-only básicas — permitidas sem restrição
      return 0
      ;;
    *)
      echo "BLOCKED: ferramenta '$tool' não permitida (apenas aws, az, dtctl, pandoc, ls, stat, cat, file)"
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
  local output exit_code=0
  output=$(_timeout "${timeout}" "$cmd") || {
    exit_code=$?
    if [[ $exit_code -eq 124 ]]; then
      echo "TIMEOUT: comando excedeu ${timeout}s"
      return $exit_code
    fi
  }

  # Detecta falhas de autenticação e adiciona instrução de remediação
  local output_lower="${output,,}"
  if [[ $exit_code -ne 0 || "$output_lower" == *"expired"* || "$output_lower" == *"please run"* ]]; then
    # Azure CLI auth failure
    if [[ "$output_lower" == *"az login"* || "$output_lower" == *"az account set"* || "$output_lower" == *"refresh token"* || "$output_lower" == *"token has expired"* || "$output_lower" == *"AADSTS"* ]]; then
      echo "$output"
      echo ""
      echo "AUTH_EXPIRED: Sessão Azure expirada. Execute no terminal: az login"
      echo "Após login, tente novamente o comando."
      return $exit_code
    fi
    # AWS SSO token expired
    if [[ "$output_lower" == *"sso session"* || "$output_lower" == *"token has expired"* || "$output_lower" == *"the sso"*"expired"* || "$output_lower" == *"unauthorized"*"sso"* || "$output_lower" == *"invalididentitytoken"* || "$output_lower" == *"expiredtoken"* ]]; then
      echo "$output"
      echo ""
      echo "AUTH_EXPIRED: Sessão AWS SSO expirada. Execute no terminal: aws sso login --profile <PROFILE>"
      echo "Após login, tente novamente o comando."
      return $exit_code
    fi
    # Dynatrace SSO failure (dtctl/API)
    if [[ "$output_lower" == *"sso authentication"* || "$output_lower" == *"403"*"sso"* || "$output_lower" == *"token is expired"* || "$output_lower" == *"unauthorized"*"sso"* ]]; then
      echo "$output"
      echo ""
      echo "AUTH_EXPIRED: Token Dynatrace SSO expirado. Execute no terminal: dtctl auth login"
      echo "Após login, tente novamente o comando."
      return $exit_code
    fi
  fi

  echo "$output"
  return $exit_code
}

# ─── MCP Protocol (JSON-RPC 2.0 over stdio) ─────────────────────────────────

TOOLS_JSON='[
  {
    "name": "sre_run_command",
    "description": "Executa um comando read-only de SRE (aws, az, dtctl, pandoc). kubectl NÃO permitido. Bloqueia automaticamente qualquer operação de escrita/mutação.",
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
  while IFS= read -r line || [[ -n "$line" ]]; do
    # Strip \r (Windows line endings) and skip empty/whitespace-only lines
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*$ ]] && continue

    # Skip Content-Length headers (some MCP clients send HTTP-style framing)
    [[ "$line" =~ ^Content-Length: ]] && continue

    # Validate JSON before processing
    if ! echo "$line" | jq -e . >/dev/null 2>&1; then
      continue
    fi

    local method id params
    method=$(echo "$line" | jq -r '.method // empty') || continue
    id=$(echo "$line" | jq -r '.id // empty') || continue
    params=$(echo "$line" | jq -c '.params // {}') || continue

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
