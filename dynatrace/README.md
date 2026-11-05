# Dynatrace Incidents Tool

Ferramenta CLI para buscar incidentes Dynatrace, enriquecer com dados (Classic API + DQL/dtctl com Davis AI), gerar relatório Markdown/PDF e preparar contexto para análise automatizada de SRE com GitHub Copilot.

---

## ⚡ Quick Start: Instalação e Execução

```bash
cd dynatrace
./install.sh        # cria o link `view_dyna_incidents` em $HOME/bin
export PATH="$HOME/bin:$PATH"  # se necessário

# Buscar incidentes
view_dyna_incidents <termo>
```

---

## 🔐 Autenticação Moderna: `dtctl auth login` (Zero Token)

O CLI oficial do Dynatrace [**`dtctl`**](https://github.com/Dynatrace/dtctl) suporta autenticação via Single Sign-On (SSO / OAuth2) através do comando **`dtctl auth login`**.

> [!IMPORTANT]
> **Não é mais necessário gerar ou gerenciar tokens estáticos para o `dtctl`!**  
> Basta o usuário possuir as permissões necessárias atribuídas via **role (RBAC)** no ambiente Dynatrace. O `dtctl auth login` abre a autenticação via browser, armazena os tokens de forma segura no keyring do sistema operacional e renova a sessão automaticamente.

### 1. Instalar o `dtctl`

```bash
# Instalação oficial via shell script (Linux / macOS)
curl -fsSL https://raw.githubusercontent.com/Dynatrace/dtctl/main/install.sh | sh

# Ou via Homebrew (macOS / Linux)
brew install dynatrace-oss/tap/dtctl
```

### 2. Autenticar com `dtctl auth login`

Configure o contexto e faça o login via browser SSO:

```bash
dtctl auth login --context <tenant-id> --environment "https://<tenant-id>.apps.dynatrace.com"
```

### 3. Validar a Conexão e Sessão

```bash
# Valida conectividade e configuração
dtctl doctor

# Exibe a identidade autenticada e o contexto atual
dtctl auth whoami --plain
```

### 4. Permissões de Role no Dynatrace (RBAC)

O `dtctl` executa ações respeitando rigorosamente as roles atribuídas à sua conta no Dynatrace. Para que as análises profundas de incidentes, topologia e Davis funcionem plenamente, garanta que suas roles possuam as seguintes permissões de leitura no Grail:

| Permissão | Finalidade |
|---|---|
| `storage:events:read` | Leitura de eventos Davis (`dt.davis.events`) e causas-raízes |
| `storage:entities:read` | Topologia de serviços, hosts e cloud applications (`dt.entity.*`) |
| `storage:metrics:read` | Métricas de performance (CPU, memória, throughput) |
| `storage:logs:read` | Consulta de logs via DQL (opcional) |

---

## ⚙️ Variáveis de Ambiente e Configuração

Configure as variáveis no seu shell (`~/.zshrc` ou `~/.bashrc`) ou no arquivo `.env`:

```bash
# Obrigatório: URL do ambiente Dynatrace
export DYNATRACE_ENV_URL="https://<tenant>.live.dynatrace.com"

# Classic API Token (Api-Token) — usado para chamadas REST v2 diretas (/api/v2/problems)
export DYNATRACE_API_TOKEN="dt0c01.<seu-token>"

# Platform Token estático — DISPENSÁVEL ao utilizar dtctl auth login!
# (Mantido apenas como fallback legado opcional para ambientes sem dtctl)
# export DYNATRACE_PLATFORM_TOKEN="dt0s16.<seu-token>"
```

### Multi-tenant (Seleção Interativa)

Caso gerencie múltiplos ambientes, defina variáveis sufixadas no seu shell profile:

```bash
export DYNATRACE_ENV_URL_PROD="https://prod.live.dynatrace.com"
export DYNATRACE_API_TOKEN_PROD="dt0c01.<token>"

export DYNATRACE_ENV_URL_STG="https://stg.live.dynatrace.com"
export DYNATRACE_API_TOKEN_STG="dt0c01.<token>"
```

O `view_dyna_incidents` detectará os ambientes automaticamente e oferecerá menu interativo (via `fzf`).

---

## 🚀 Uso Comum

```bash
# Buscar incidentes por termo (janela padrão definida em config.ini)
view_dyna_incidents service-name

# Janela de tempo relativa
view_dyna_incidents service-name --window=1h
view_dyna_incidents service-name --window=24h

# Intervalo absoluto
view_dyna_incidents service-name --from "01-05-2026 10:00" --to "01-05-2026 12:00"

# Diagnóstico de tokens, dtctl e endpoints
view_dyna_incidents --diagnose

# Desabilitar enriquecimento dtctl (caso deseje apenas consulta rápida)
view_dyna_incidents service-name --no-dtctl
```

---

## 🤖 Análise Automatizada de SRE com GitHub Copilot (MCP)

Ao finalizar a coleta de incidentes, o `view_dyna_incidents` pode preparar e abrir automaticamente o workspace no VS Code com o MCP Server `sre-readonly` pré-configurado.

1. No Copilot Chat (Ctrl+Shift+I), execute o agente:
   ```
   @analisar-incidente
   ```
2. O agente executará comandos de leitura através do MCP (`sre_run_command`) consultando o `dtctl` para obter topologia, eventos Davis, callers e métricas sem intervenção manual.
3. Se a sessão SSO do `dtctl` expirar durante a investigação, o MCP retornará `AUTH_EXPIRED` e solicitará a execução rápida de `dtctl auth login` no terminal.

---

## 📄 Geração de Relatórios PDF

A geração de relatórios em PDF é opcional:
- Requer `pandoc` e um compilador LaTeX (ex: `texlive-xetex`).
- O relatório em Markdown é sempre gerado e salvo na pasta do incidente (`data/<timestamp>_analysis.md`).

---

## 📚 Referências

- [Repositório Oficial dtctl no GitHub](https://github.com/Dynatrace/dtctl)
- [Documentação Dynatrace Query Language (DQL)](https://docs.dynatrace.com/docs/platform/dynatrace-query-language)
- [Guia de Integração Davis AI](DAVIS_INTEGRATION.md)
