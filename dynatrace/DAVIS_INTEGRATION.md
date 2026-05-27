# Davis Root Cause Analysis Integration

**Status**: ✅ Implementado (requer token DQL/Grail)

## ⚡ Quick Start (5 minutos)

1. **Acesse**: https://myaccount.dynatrace.com/profile
2. **Vá para**: Security → Tokens
3. **Clique**: "Create token"
4. **Configure**: Nome `davis-incidents-analysis`, scopes `storage:events:read` + `storage:entities:read`
5. **Copie o token** (aparece apenas uma vez, começa com `sdt0c60...`)
6. **Configure**:
   ```bash
   export DYNATRACE_PLATFORM_TOKEN="sdt0c60.SV3U5WQXAMPLE..."
   ```
7. **Teste**:
   ```bash
   view_dyna_incidents credit --window=3h --davis
   ```

---

## O que é Davis?

Davis é o mecanismo de IA do Dynatrace que fornece:
- **Event Causation**: Cadeia de causa de eventos correlacionados
- **Root Cause Analysis**: Identificação automática da entidade primária que causou a falha
- **Impact Analysis**: Propagação de falhas em serviços dependentes
- **Correlation**: Agrupamento de eventos relacionados por contexto

## Como Configurar

### 1. Criar Token DQL/Grail

Acesse https://myaccount.dynatrace.com/profile:

1. Clique em **Profile** (se necessário)
2. Vá para **Security** (opções de segurança)
3. Localize a seção de **Tokens** ou **API Tokens**
4. Clique em **Create token** (ou similar)
5. Configure:
   - **Name**: `davis-incidents-analysis`
   - **Scopes necessários**:
     - `storage:events:read`
     - `storage:entities:read`
     - Opcional: `events.read, entities.read` (para Classic API)
6. Gere e **copie o token** imediatamente (não será exibido novamente)

### 2. Configurar Variável de Ambiente

**Opção A: Via linha de comando**

```bash
# Copie o token gerado em myaccount.dynatrace.com/profile
export DYNATRACE_PLATFORM_TOKEN="sdt0c60.SV3U5WQXAMPLE..."
export DYNATRACE_ENV_URL="https://seu-tenant.live.dynatrace.com"
```

**Opção B: Via arquivo `.env` (recomendado)**

No diretório do projeto:

```bash
cd /path/to/your/project/dynatrace/
cp .env.example .env
nano .env
```

Conteúdo de `.env`:
```env
DYNATRACE_ENV_URL=https://seu-tenant.live.dynatrace.com
DYNATRACE_API_TOKEN=dt0c01...  # Token Classic (opcional, para /api/v2)
DYNATRACE_PLATFORM_TOKEN=sdt0c60.SV3U5WQXAMPLE...  # Token DQL/Grail (necessário para Davis)
DYNATRACE_AUTH_MODE=auto
```

**Opção C: Variável de ambiente permanente**

Adicione ao shell profile (`~/.bashrc`, `~/.zshrc`, etc):
```bash
export DYNATRACE_PLATFORM_TOKEN="sdt0c60.SV3U5WQXAMPLE..."
export DYNATRACE_ENV_URL="https://seu-tenant.live.dynatrace.com"
```

## Como Usar

### Análise Básica com Davis

```bash
view_dyna_incidents credit --window=3h --davis
```

**Output**:
```
[1] P-2605153 - Failure rate increase (CLOSED)
  URL alarme : https://...
  Origem     : AWS
  ...

[DAVIS - CAUSATION & CORRELATION]
============================================================

  [1] Failure rate increase: The error rate increased to 91.43 %.
       Timestamp: 1778250360000
       Entidade: credit-caas-contract-manager-service
       Cadeia de Causacao: cdc-btg -> credit-caas-contract-manager-service -> DynamoDB timeout
       Eventos Correlacionados: P-2605151 (Lambda error), P-2605160 (SQS throttling)

[DAVIS - ROOT CAUSE ANALYSIS]
============================================================

  [1] Failure rate increase
       Tipo de Evento: Failure rate increase
       Quantidade: 3
       Latencia P99: 5432ms
       Taxa de Falha Maxima: 93.81%
       Entidade Primaria: SERVICE/cdc-btg
```

### Análise em Período Específico

```bash
view_dyna_incidents credit \
  --from "08-05-2026 12h50m" \
  --to "08-05-2026 15h30m" \
  --davis
```

### Exportar Insights Davis em JSON

```bash
view_dyna_incidents credit --window=3h --davis --json > incidents_davis.json
```

## Queries DQL Utilizadas

### 1. Fetch Event Causation

```dql
fetch events
| filter timestamp >= 1778244000000 and timestamp <= 1778260000000
| search "credit"
| fields timestamp, event.type, event.title, event.status,
    entity.name, dt.event.causation_chain, dt.event.correlation_events,
    dt.event.root_cause_entity, dt.event.description
| sort timestamp desc
| limit 20
```

**O que extrai**:
- `dt.event.causation_chain`: Ordem de eventos que levaram à falha
- `dt.event.correlation_events`: Eventos relacionados detectados por Davis
- `dt.event.root_cause_entity`: Entidade que iniciou a cascata

### 2. Fetch Root Cause Analysis

```dql
fetch events
| filter timestamp >= 1778244000000 and timestamp <= 1778260000000
| search "credit"
| fields timestamp, event.type, event.title,
    dt.entity.failure_rate_overall, dt.entity.request_count_total,
    dt.entity.latency_p99, entity.type, entity.name,
    dt.event.baseline.error_rate, dt.event.severity
| stats count() as event_count,
    avg(dt.entity.latency_p99) as avg_latency,
    max(dt.entity.failure_rate_overall) as max_failure_rate
  by event.type, event.title, entity.type, entity.name
```

**O que extrai**:
- Taxa de falha vs baseline
- Latência P99 agregada
- Contagem e distribuição de eventos por tipo/entidade

## Troubleshooting

### Passo 1: Confirmar Token Criado

No Dynatrace, vá para https://myaccount.dynatrace.com/profile:

1. Na aba **Security** (Segurança)
2. Procure por **Tokens** ou **API Tokens**
3. Localize seu token `davis-incidents-analysis` (começa com `sdt0c60` ou similar)
4. Se não estiver lá, crie um novo seguindo a seção "Como Configurar"

### Passo 2: Validar Token no Ambiente

```bash
# Verifique se o token está definido
echo $DYNATRACE_PLATFORM_TOKEN

# Se vazio, configure
export DYNATRACE_PLATFORM_TOKEN="sdt0c60.SV3U5WQXAMPLE..."

# Ou se estiver no .env
source .env && echo $DYNATRACE_PLATFORM_TOKEN
```

### Erro: "Token DQL/Grail nao configurado"

```bash
# 1. Confirme que o token foi criado em myaccount.dynatrace.com/profile
# 2. Copie o token exato (aparece apenas uma vez)
# 3. Configure em .env ou export

# 4. Teste conectividade
view_dyna_incidents --diagnose

# Verifique a seção:
# "dql_execute": { "status": 200, "ok": true, ... }
```

### Erro: "Invalid token" ou "Unauthorized" (401)

- Verifique se o token está ativo e não expirou
- Confirm scopes: `storage:events:read` e `storage:entities:read`
- Teste em: https://seu-tenant.apps.dynatrace.com/platform/storage/query/v1/query:execute

### Sem resultados de Davis

- Davis requer **pelo menos 1 incidente aberto** na janela de tempo
- Eventos devem ter `dt.event.causation_chain` populado (depende de configuração Dynatrace)
- Se usar modo `--json`, resultados aparecem em estruturas vazias

## Exemplo: Postmortem Completo com Davis

```bash
# 1. Coletar incidentes e análise Davis
view_dyna_incidents credit --window=4h --davis --json > incident_analysis.json

# 2. Gerar sumário executivo
view_dyna_incidents credit --window=4h --davis --executive

# 3. Compartilhar em Slack/Teams
cat incident_analysis.json | jq '.[] | [.alarm, .origin, .workload]' | pbcopy
```

## Próximos Passos

- [ ] Configurar DYNATRACE_PLATFORM_TOKEN
- [ ] Testar `view_dyna_incidents --diagnose` para confirmar Davis
- [ ] Rodar `--davis` em incidente recente para validar insights
- [ ] Integrar queries Davis em runbook de SRE
- [ ] Adicionar alertas que triggerem análise Davis automática

## Referências

- [Dynatrace DQL Docs](https://docs.dynatrace.com/docs/platform/dynatrace-query-language)
- [Davis AI Events API](https://docs.dynatrace.com/docs/api-and-integrations/dynatrace-apis/environment-api/events-api)
- [Root Cause Analysis](https://docs.dynatrace.com/docs/platform/ai-powered-analytics/root-cause-analysis)
