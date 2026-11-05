# Integração Davis Root Cause Analysis (via dtctl & DQL)

**Status**: ✅ Ativo por padrão (autenticação SSO via `dtctl auth login`)

---

## ⚡ Quick Start (Autenticação Zero Token)

Com o suporte a `dtctl auth login`, **não é mais necessário gerar nem gerenciar tokens estáticos** (`DYNATRACE_PLATFORM_TOKEN` / `sdt0c60...`). O acesso é concedido diretamente com base nas **roles e permissões (RBAC)** atribuídas ao seu usuário no Dynatrace.

### Passo 1: Instale o `dtctl`

```bash
# Script oficial (Linux / macOS)
curl -fsSL https://raw.githubusercontent.com/Dynatrace/dtctl/main/install.sh | sh

# Ou via Homebrew (macOS / Linux)
brew install dynatrace-oss/tap/dtctl
```

Repositório oficial: [https://github.com/Dynatrace/dtctl](https://github.com/Dynatrace/dtctl)

### Passo 2: Faça Login via SSO no Dynatrace

```bash
dtctl auth login --context <seu-tenant> --environment "https://<seu-tenant>.apps.dynatrace.com"
```

O comando abrirá uma janela no seu navegador para autenticação SSO. O token OAuth resultante será gerenciado de forma segura pelo `dtctl` no keyring local e renovado automaticamente.

### Passo 3: Valide a Autenticação e Permissões

```bash
# Valida conectividade e ambiente
dtctl doctor

# Confirma o usuário autenticado e contexto
dtctl auth whoami --plain
```

### Passo 4: Execute a Análise com Davis

```bash
view_dyna_incidents credit --window=3h
```

---

## 🧠 O que é Davis AI?

Davis® é o mecanismo de Inteligência Causal do Dynatrace que correlaciona automaticamente bilhões de dependências em tempo real, fornecendo:
- **Event Causation**: Cadeia de causa e efeito de eventos correlacionados.
- **Root Cause Analysis (RCA)**: Identificação automática da entidade primária que causou a degradação ou falha.
- **Impact Analysis**: Propagação de anomalias em serviços consumidores (callers) e dependências.
- **Correlation**: Agrupamento automático de alertas e eventos disparados em cascata.

---

## 🛡️ Permissões e Roles Necessárias no Dynatrace (RBAC)

Como a autenticação agora utiliza sua identidade corporativa via SSO, a capacidade do `dtctl` de consultar os dados do Grail e do Davis depende das **roles atribuídas ao seu usuário ou grupo** no Dynatrace Platform:

### Permissões Mínimas Recomendadas

| Permissão | Escopo / Finalidade |
|---|---|
| `storage:events:read` | **Obrigatória para Davis**: Permite consultar tabelas `dt.davis.events` e `events`. |
| `storage:entities:read` | **Obrigatória para Topologia**: Permite consultar `dt.entity.service`, `dt.entity.host`, etc. |
| `storage:metrics:read` | Permite consultar métricas de infraestrutura e performance via DQL. |
| `storage:logs:read` | Opcional: Permite consultar logs correlacionados no Grail. |

> [!TIP]
> Caso receba erro `403 / NOT_AUTHORIZED_FOR_TABLE` ao rodar queries `fetch dt.davis.events`, solicite ao administrador do Dynatrace a atribuição de uma role com permissão `storage:events:read` e `storage:entities:read` ao seu usuário.

---

## 🔍 Exemplos de Consultas DQL Executadas

O `view_dyna_incidents` e o agente Copilot executam automaticamente queries DQL estruturadas via `dtctl`:

### 1. Detalhes do Problema Davis
```dql
fetch dt.davis.problems, from:-48h
| filter event.id == "PROBLEM-ID" or display_id == "P-XXXXXX"
| limit 1
```

### 2. Cadeia de Causa-Raiz e Eventos Davis da Entidade
```dql
fetch dt.davis.events, from:-48h
| filter in("SERVICE-XXXXXXXXXXXX", dt.entity.service)
| sort timestamp desc
| limit 20
```

### 3. Resolução de Callers (Quem chama o serviço afetado?)
```dql
fetch dt.entity.service
| filter in("SERVICE-XXXXXXXXXXXX", calls)
| fields entity.name, id
| limit 10
```

### 4. Resolução de Dependências (O que o serviço afetado consome?)
```dql
fetch dt.entity.service
| filter id == "SERVICE-XXXXXXXXXXXX"
| fields entity.name, toRelationships.calls
| limit 1
```

---

## 🛠️ Troubleshooting

### Sessão SSO Expirada (`AUTH_EXPIRED`)
Os tokens de SSO possuem tempo de expiração padrão. Se o `view_dyna_incidents` ou o MCP alertar que a sessão expirou:

```bash
dtctl auth login
```

### Ambiente Headless / WSL (Sem Keyring)
Em ambientes como WSL, containers ou servidores sem ambiente gráfico, o keyring do sistema operacional pode não estar disponível. O `view_dyna_incidents` configura isso automaticamente, mas você também pode exportar no shell:

```bash
export DTCTL_TOKEN_STORAGE=file
```

### Fallback Legado: `DYNATRACE_PLATFORM_TOKEN`
Caso esteja em um pipeline CI/CD ou ambiente onde a autenticação interativa via SSO (`dtctl auth login`) não possa ser executada, ainda é possível fornecer um token Platform estático:

```bash
export DYNATRACE_PLATFORM_TOKEN="dt0s16.EXAMPLE..."
```

---

## 📚 Referências

- [Repositório dtctl no GitHub](https://github.com/Dynatrace/dtctl)
- [Dynatrace DQL - Davis Problems](https://docs.dynatrace.com/docs/platform/dynatrace-query-language)
- [Davis AI Causation & Root Cause Analysis](https://docs.dynatrace.com/docs/platform/ai-powered-analytics/root-cause-analysis)
