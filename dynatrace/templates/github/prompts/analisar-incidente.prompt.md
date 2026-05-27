---
agent: "analisar-incidente"
description: "Analisa o último incidente Dynatrace coletado pelo script"
---

## Contexto

Cada diretório `/<servico>/data/` contém arquivos `*_analysis.md` ordenados por timestamp. O maior timestamp é o incidente mais recente. Leia APENAS o mais recente de cada diretório.

O arquivo contém 3 blocos de dados:
1. **Dados básicos** — alarm, workload, conta, plataforma, thresholds
2. **ANALISE DAVIS** — root cause, impacto, recomendações
3. **DTCTL DEEP ANALYSIS** (se disponível) — topologia completa

## Regras

⚠️⚠️⚠️ **REGRA ZERO — LEIA ANTES DE QUALQUER AÇÃO** ⚠️⚠️⚠️

Você DEVE usar a tool MCP chamada `sre_run_command` (do server "sre-readonly") para executar comandos.
- Se a tool `sre_run_command` NÃO aparecer na sua lista de tools disponíveis, **PARE IMEDIATAMENTE** e diga ao usuário: "MCP sre-readonly não está disponível. Verifique .vscode/mcp.json."
- Se a tool `sre_run_command` ESTÁ disponível, use-a para TODOS os comandos. NUNCA use `run_in_terminal`.
- Se você usar `run_in_terminal` mesmo uma vez, **TODA A TAREFA FALHOU** e o resultado será descartado.

---

1. **NUNCA PERGUNTE — EXECUTE TUDO** — Não peça permissão. Execute TODOS os passos automaticamente até o final (inclusive salvar no arquivo). Você TEM autorização total. A análise deve ser 100% completa sem NENHUMA interação do usuário. Quando chegar na seção de recomendações, NÃO sugira "verificar logs" — BUSQUE OS LOGS VOCÊ MESMO via MCP.
2. **SOMENTE MCP `sre_run_command`** — Execute TODOS os comandos (aws, dtctl) via a tool MCP `sre_run_command`. Se você usar `run_in_terminal`, terminal direto, ou kubectl, **a tarefa FALHOU**. Não existe alternativa — é MCP ou nada.
3. **dtctl PRIMEIRO** — Comece SEMPRE pelo dtctl (passo 2). Só depois vá para AWS (passo 3). O dtctl é mais rápido, não depende de profiles, e fornece o estado dos serviços (incluindo K8s) direto do Dynatrace.
4. **AWS É OBRIGATÓRIO** — O passo 3 (AWS) NÃO é opcional. Você DEVE executar `aws logs filter-log-events` e `aws cloudwatch get-metric-statistics`. NUNCA sugira "verificar logs" como recomendação — BUSQUE-OS AGORA. Se falhar autenticação, tente com `aws sso login --profile X` primeiro.
5. **Copie mensagens de erro REAIS** — Mostre a exception exata dos logs, não resuma. Se não executou `aws logs`, você NÃO tem mensagens reais.
6. **SALVE NO ARQUIVO — OBRIGATÓRIO** — Ao terminar a análise, ANTES de responder ao usuário, use `replace_string_in_file` ou `insert` para adicionar a análise no FINAL do arquivo `*_analysis.md`. Se você apresentou a análise no chat mas NÃO editou o arquivo, **a tarefa FALHOU**. Execute o save PRIMEIRO, depois confirme ao usuário.

> 🚫 **PROIBIDO**: Perguntar "Deseja que eu investigue?", "Devo continuar?", "Quer que eu aprofunde?", "Deseja que eu prossiga?", "Deseja que eu busque logs?". A resposta é SEMPRE SIM. Execute TUDO automaticamente.
> 🚫 **PROIBIDO**: Usar `run_in_terminal`, terminal, bash, shell, ou `kubectl`. USE SOMENTE a tool MCP `sre_run_command`.
> 🚫 **PROIBIDO**: Parar a investigação porque "não encontrou profile AWS". Tente TODOS os profiles. Use dtctl como complemento, mas AWS é obrigatório.
> 🚫 **PROIBIDO**: Apresentar análise baseada APENAS nos dados do arquivo sem executar comandos via MCP.
> 🚫 **PROIBIDO**: Sugerir "investigar logs" ou "verificar deploys" como recomendação. Você deve TER FEITO isso nos passos 2 e 3. Se a recomendação é "verificar X", é porque você NÃO fez seu trabalho.
> 🚫 **PROIBIDO**: Gerar seção "Recomendações" com ações que VOCÊ deveria ter executado (ex: "verificar CloudWatch", "buscar CloudTrail"). Execute-as ANTES de escrever recomendações.
> 🚫 **PROIBIDO**: Mostrar a análise no chat sem ter escrito no arquivo. O arquivo DEVE ser editado ANTES de apresentar o resultado.

---

## Como executar comandos

Use **EXCLUSIVAMENTE** a tool MCP `sre_run_command` (server: sre-readonly).

**Chamada correta (use EXATAMENTE este formato):**
```
sre_run_command(command: "dtctl query 'fetch dt.entity.service | filter entity.name == \"cdc-btg\" | limit 1' -o json", timeout: 90)
```

```
sre_run_command(command: "aws logs filter-log-events --profile <aws-profile> --region us-east-1 --log-group-name /aws/lambda/<log-group> --filter-pattern ERROR --limit 20 --output json", timeout: 90)
```

A tool aceita `command` (string) e `timeout` (int, padrão 60s, máx 120s).

**ERRADO (NUNCA faça isso):**
```
❌ run_in_terminal: aws logs filter-log-events ...
❌ run_in_terminal: dtctl query ...
❌ Ran terminal command: aws ...
```

> 🚫 **Se você usar `run_in_terminal` ou "Ran terminal command", PARE e REFAÇA usando `sre_run_command`.** O terminal NÃO é aceito.
> 🚫 **Sintaxe dtctl**: Use `dtctl query '...'` (NUNCA `dtctl dql query`). O subcomando é `query`, não `dql query`.
> 🚫 **Sem redirects**: Não use `2>&1` nos comandos. O MCP já captura stdout+stderr.

---

## Workflow

### 1. Leia o arquivo de incidente

Leia o `*_analysis.md` mais recente. Extraia:
- **Serviço afetado** (campo `Workload`)
- **Conta AWS** (campo `Conta`)
- **Plataforma** (AWS Lambda, Kubernetes, etc.)
- **Root Cause** (pode estar VAZIO — isso é normal, significa que Davis não identificou)
- **Entity IDs** (se houver na seção DTCTL)
- **Período** (início/fim do problema)

### 2. PRIMEIRO — Investigue via dtctl (OBRIGATÓRIO)

O dtctl é a ferramenta PRINCIPAL. Execute SEMPRE, independente de ter acesso AWS ou não.

#### 2.1 Resolver entity ID do serviço
```bash
dtctl query 'fetch dt.entity.service
| filter entity.name == "NOME-DO-SERVICO"
| fields id, entity.name
| limit 5' -o json
```

#### 2.2 Descobrir QUEM CHAMA este serviço (callers/upstream)
```bash
dtctl query 'fetch dt.entity.service
| fieldsAdd calls[dt.entity.service]
| filter in("SERVICE-ID", calls[dt.entity.service])
| fields entity.name, id
| limit 20' -o json
```

#### 2.3 Descobrir O QUE este serviço CHAMA (dependências/downstream)
```bash
dtctl query 'fetch dt.entity.service
| filter id == "SERVICE-ID"
| fieldsAdd calls[dt.entity.service]
| limit 1' -o json
```

#### 2.4 Eventos Davis do serviço no período
```bash
dtctl query 'fetch dt.davis.events, from:-48h
| filter in("SERVICE-ID", dt.entity.service)
| sort timestamp desc
| limit 20' -o json
```

#### 2.5 Detalhes do problema Davis
```bash
dtctl query 'fetch dt.davis.problems, from:-48h
| filter display_id == "P-XXXXXXX"
| limit 1' -o json
```

#### 2.6 Se a plataforma for Kubernetes — investigar workload
```bash
# Encontrar workload K8s
dtctl query 'fetch dt.entity.cloud_application
| filter contains(entity.name, "NOME-DO-SERVICO")
| fieldsAdd belongs_to[dt.entity.cloud_application_namespace], clustered_by[dt.entity.kubernetes_cluster]
| limit 5' -o json

# Eventos K8s (OOM, CrashLoop, probes)
dtctl query 'fetch dt.davis.events, from:-48h
| filter isNotNull(dt.entity.cloud_application)
| filter in("CLOUD_APPLICATION-ID", dt.entity.cloud_application[])
| sort timestamp desc
| limit 10
| fields timestamp, event.name, event.type, event.description' -o json

# Métricas CPU
dtctl query 'timeseries cpu = avg(dt.kubernetes.container.cpu_usage), from:-48h, interval:1h, filter: { in(dt.entity.cloud_application, classicEntitySelector("type(CLOUD_APPLICATION),entityName(WORKLOAD-NAME)")) } | limit 1' -o json

# Métricas memória
dtctl query 'timeseries mem = avg(dt.kubernetes.container.memory_working_set), from:-48h, interval:1h, filter: { in(dt.entity.cloud_application, classicEntitySelector("type(CLOUD_APPLICATION),entityName(WORKLOAD-NAME)")) } | limit 1' -o json
```

#### 2.7 Investigar o estado de CADA caller e dependência

Para CADA serviço encontrado nos passos 2.2 (callers) e 2.3 (dependências), execute:

```bash
# Eventos Davis de cada serviço na cadeia
dtctl query 'fetch dt.davis.events, from:-48h
| filter in("SERVICE-ID-DO-CALLER-OU-DEP", dt.entity.service)
| sort timestamp desc
| limit 10' -o json
```

Registre o **estado** de cada serviço:
- `HEALTHY` — sem eventos Davis no período
- `DEGRADED` — eventos de response time ou throughput
- `FAILING` — failure rate increase
- `UNKNOWN` — não foi possível consultar

> 🎯 **Objetivo**: Produzir um MAPA DE ESTADO mostrando a saúde de cada serviço que usa ou é usado pelo recurso que alarmou. Isso mostra se o problema é isolado ou se propagou pela cadeia.

Exemplo de saída esperada:
```
[CALLER] service-a → HEALTHY (sem eventos)
[CALLER] service-b → FAILING (failure rate 95% desde 13:30)
[AFETADO] cdc-btg → FAILING (failure rate 100%)
[DEP] database-service → DEGRADED (response time +2600%)
```

### 3. OBRIGATÓRIO — Investigue via AWS CLI

⚠️ **Este passo é OBRIGATÓRIO. Não pule. Não sugira como recomendação. EXECUTE AGORA.**

#### 3.1 Encontrar o profile correto
```bash
aws configure list-profiles 2>&1 | while read p; do
  acct=$(aws configure get sso_account_id --profile "$p" 2>/dev/null)
  echo "$p → $acct"
done
```

Procure um profile cuja conta corresponda à `Conta` do arquivo.

#### 3.2 Se encontrou o profile — buscar logs e métricas
```bash
# Encontrar log group
aws logs describe-log-groups --profile "$profile" --region us-east-1 \
  --query "logGroups[].logGroupName" --output text | tr '\t' '\n' | grep -i "NOME-SERVICO"

# Logs de erro no período do incidente
aws logs filter-log-events --profile "$profile" --region us-east-1 \
  --log-group-name "/aws/lambda/NOME-LAMBDA" \
  --start-time EPOCH_MS_START --end-time EPOCH_MS_END \
  --filter-pattern "?ERROR ?Error ?Exception ?ECONNRESET ?ETIMEDOUT ?failed ?Traceback" \
  --limit 30 --output json | jq -r '.events[]?.message // empty' | head -50

# Métricas Lambda
for metric in Errors Throttles Invocations; do
  aws cloudwatch get-metric-statistics --profile "$profile" --region us-east-1 \
    --namespace AWS/Lambda --metric-name "$metric" \
    --dimensions Name=FunctionName,Value="NOME-LAMBDA" \
    --start-time ISO_START --end-time ISO_END --period 3600 --statistics Sum
done

# Duration
aws cloudwatch get-metric-statistics --profile "$profile" --region us-east-1 \
  --namespace AWS/Lambda --metric-name Duration \
  --dimensions Name=FunctionName,Value="NOME-LAMBDA" \
  --start-time ISO_START --end-time ISO_END --period 300 --statistics Average Maximum

# Deploys recentes
aws cloudtrail lookup-events --profile "$profile" --region us-east-1 \
  --lookup-attributes AttributeKey=ResourceName,AttributeValue="NOME-LAMBDA" \
  --start-time ISO_3DAYS_AGO --end-time ISO_NOW --max-results 10
```

#### 3.3 Se o Root Cause está em OUTRA conta

O campo Root Cause do Davis pode indicar um serviço em conta diferente. Busque-o:
```bash
service_name="NOME-ROOT-CAUSE"
for profile in $(aws configure list-profiles); do
  found=$(aws logs describe-log-groups --profile "$profile" --region us-east-1 \
    --query "logGroups[].logGroupName" --output text 2>/dev/null | tr '\t' '\n' | grep -i "$service_name")
  if [[ -n "$found" ]]; then
    echo "ENCONTRADO | Profile: $profile | LogGroup: $found"
    break
  fi
done
```

#### 3.4 Se NÃO encontrou profile AWS — tente alternativas

⚠️ **NÃO PARE AQUI.** Tente:
1. `aws sso login --profile PROFILE_NAME` para reautenticar
2. Tente TODOS os profiles disponíveis com `aws configure list-profiles`
3. Se NENHUM funciona, registre "AWS inacessível — SSO expirado" e continue com dados do dtctl

> 🚫 **NÃO sugira "verificar logs" como recomendação se você não tentou TODOS os profiles.**
> 🚫 **NÃO use kubectl.** O estado dos serviços K8s deve ser obtido via Dynatrace/dtctl.

### 4. Consolide o diagnóstico

Com base nos dados coletados (dtctl + AWS + kubectl), produza:
- **Mapa de Estado dos Serviços**: OBRIGATÓRIO — para cada serviço na cadeia (callers + afetado + dependências), mostre o estado (HEALTHY/DEGRADED/FAILING) com evidência
- **Tabela de Investigação**: cada serviço, conta, plataforma, métricas, mensagem de erro real, veredicto
- **Causa Raiz**: serviço, conta, mensagem EXATA (se logs disponíveis), tipo (infra/negócio/dependência)
- **Timeline**: eventos em ordem cronológica
- **Impacto**: serviços e usuários afetados
- **Ações de Remediação**: imediatas, curto prazo, preventivas

### 5. OBRIGATÓRIO — Salve no arquivo (FAÇA ANTES de responder ao usuário)

⚠️⚠️⚠️ **ESTA É A AÇÃO MAIS IMPORTANTE. EXECUTE ANTES DE QUALQUER OUTPUT NO CHAT.**

**Sequência obrigatória:**
1. Colete dados (passos 1-4) ✓
2. **ESCREVA a análise no arquivo** ← FAÇA ISSO AGORA
3. Só DEPOIS apresente o resumo no chat

Use `replace_string_in_file` ou a ferramenta de inserção para adicionar a análise no FINAL do arquivo `*_analysis.md`.

**Como fazer:**
1. Leia a última linha do arquivo
2. Use `replace_string_in_file` com `oldString` sendo as últimas 3-5 linhas do arquivo, e `newString` sendo essas mesmas linhas + a análise completa
3. NÃO modifique o conteúdo existente — apenas adicione ao final

**NÃO use terminal ou MCP para escrever.** Use APENAS a ferramenta de edição de arquivos (a mesma usada para editar código).

Formato:
```markdown

---

# Análise Copilot — <DATA_HORA>

## Resumo Executivo
(1-2 parágrafos com o diagnóstico principal)

## Mapa de Estado dos Serviços
| Serviço | Papel | Estado | Evidência |
|---------|-------|--------|-----------|
| service-a | CALLER | HEALTHY | Sem eventos Davis no período |
| cdc-btg | AFETADO | FAILING | Failure rate 100% (P-2605297) |
| service-b | DEPENDÊNCIA | DEGRADED | Response time +2600% |

## Topologia (via dtctl)
(callers → serviço → dependências)

## Tabela de Investigação
| Serviço | Conta | Plataforma | Erros | Mensagem Real | Veredicto |
|---------|-------|------------|-------|---------------|-----------|
...

## Causa Raiz
...

## Timeline
...

## Impacto
...

## Ações de Remediação
...
```

> 🚫 Se apresentou a análise no chat mas NÃO escreveu no arquivo, **PARE AGORA e escreva**. Use `replace_string_in_file`.
> 🚫 Se o arquivo não foi modificado, VOCÊ FALHOU. Verifique com `read_file` que o conteúdo foi adicionado.
> ✅ Confirme ao usuário: "Análise salva em: <caminho do arquivo>"
> ✅ O último tool call antes de responder DEVE ser um `replace_string_in_file` no `*_analysis.md`.

---

## Notas técnicas

- `dtctl` usa DQL que tem `|` como pipe — é sintaxe normal, não command injection
- Use `jq -r '.[]? // empty'` para evitar erros com arrays vazios
- Use `--output text` do AWS CLI quando parsing for simples
- Se um comando retorna vazio, adicione `|| echo "SEM_RESULTADOS"`
- `dtctl` não tem acesso a logs (use AWS CLI para isso)

## O que NÃO colocar em Recomendações

Recomendações são ações FUTURAS para o time, NÃO coisas que você deveria ter investigado.

❌ ERRADO (você deveria ter feito):
- "Investigar logs da Lambda no período X"
- "Verificar se houve deploy antes do incidente"
- "Buscar logs do CloudWatch"

✅ CORRETO (ações futuras para o time):
- "Implementar retry com backoff exponencial no callback BMP"
- "Adicionar alerta de taxa de erro > 5% no endpoint /user-approve"
- "Configurar dead-letter queue para mensagens que falharam"

Se você está sugerindo "investigar logs" é porque NÃO executou o passo 3. Volte e execute.
