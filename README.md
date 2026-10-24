# Como criar um padrão de solução com prompt, agente, MCP e instruções

Este artigo descreve um padrão claro para construir uma solução de investigação automatizada com:

- **Script de bootstrap**: prepara o ambiente e gera contexto
- **Agente Copilot**: orquestra a investigação
- **Prompt estruturado**: define o passo a passo cognitivo
- **MCP**: controla a execução e garante segurança
- **Documentação objetiva**: orienta o usuário com clareza

O padrão é repetível e aplicável a casos como Dynatrace e AWS Lambda, reduzindo trabalho manual e melhorando governança.

---

## Estrutura do projeto

Uma estrutura modular e padronizada facilita a replicação:

```text
.
├── README.md
├── dynatrace/
│   ├── README.md
│   ├── install.sh
│   ├── view_dyna_incidents
│   ├── templates/
│   │   ├── github/
│   │   │   ├── copilot-instructions.md
│   │   │   ├── agents/
│   │   │   │   └── analisar-incidente.agent.md
│   │   │   └── prompts/
│   │   │       └── analisar-incidente.prompt.md
│   │   └── mcp/
│   │       └── sre-readonly.sh
├── lambda/
│   ├── README.md
│   ├── investigar-lambda
│   ├── templates/
│   │   ├── github/
│   │   │   ├── copilot-instructions.md
│   │   │   ├── agents/
│   │   │   │   └── investigar-lambda.agent.md
│   │   │   └── prompts/
│   │   │       └── investigar-lambda.prompt.md
│   │   └── mcp/
│   │       └── sre-readonly.sh
├── python/
│   ├── README.md
│   ├── install.sh
│   ├── investigar-code
│   └── templates/
│       ├── github/
│       │   ├── copilot-instructions.md
│       │   ├── agents/
│       │   │   └── investigar-code.agent.md
│       │   └── prompts/
│       │       └── investigar-code.prompt.md
│       └── vscode/
│           └── settings.json
```

---

## 1. Script de bootstrap

O script de bootstrap é o ponto de entrada. Ele prepara o workspace, copia templates, gera contexto e abre o VS Code.

Exemplo:

```bash
#!/usr/bin/env bash
set -euo pipefail

WORKDIR="$HOME/Documents/investigacao/lambda"
mkdir -p "$WORKDIR/.github/agents" "$WORKDIR/.github/prompts" "$WORKDIR/.mcp"

cp templates/github/copilot-instructions.md "$WORKDIR/.github/copilot-instructions.md"
cp templates/github/agents/investigar-lambda.agent.md "$WORKDIR/.github/agents/"
cp templates/github/prompts/investigar-lambda.prompt.md "$WORKDIR/.github/prompts/"
cp templates/mcp/sre-readonly.sh "$WORKDIR/.mcp/"

cat > "$WORKDIR/.investigation-context" <<EOF
lambda_name="$1"
window="${WINDOW:-24h}"
EOF

code --new-window "$WORKDIR"
```

---

## 2. `copilot-instructions.md`

Esse arquivo orienta como usar o agente no workspace e define regras básicas.

Exemplo:

```md
# Investigar Lambda AWS

Use este workspace para rodar o agente `@investigar-lambda`.
Todos os comandos devem ser executados via MCP `sre_run_command`.

## Passos
1. Execute `@investigar-lambda <nome-da-lambda>`
2. Aguarde a coleta de dados AWS
3. Leia o relatório final e gere insights

## Regras
- Não use `run_in_terminal` direto
- Não use `kubectl`; use apenas ferramentas permitidas pelo MCP
```

---

## 3. `agent.md`

O agente declara a identidade, o prompt e as ferramentas permitidas.

Exemplo:

```md
name: investigar-lambda
description: "Investiga uma Lambda AWS por nome, varrendo contas e relatando alarme, logs, métricas e dependências."
prompt: "lambda/investigar-lambda.prompt.md"
tools:
  - sre_run_command
```

---

## 4. `prompt.md`

O prompt define o objetivo, as regras e o fluxo da investigação.

Exemplo:

```md
## Objetivo
Investigar Lambda AWS pelo nome fornecido, coletar evidências e gerar um relatório objetivo.

## Regras
- Execute os comandos necessários via `sre_run_command`
- Use AWS CLI e dados de contas já permitidos
- Foque em causa raiz, estado de execução, erros e dependências

## Fluxo
1. Pesquisar Lambda por nome
2. Verificar CloudWatch Logs e erros recentes
3. Verificar métricas de invocação e duração
4. Montar resumo em Markdown
```

---

## 5. MCP para controle e segurança

O MCP garante que o agente execute apenas comandos permitidos e opere em um ambiente seguro.

Exemplo `mcp.json`:

```json
{
  "name": "sre-readonly",
  "description": "Permite executar comandos AWS e dtctl em modo somente leitura",
  "tools": ["sre_run_command"]
}
```

Exemplo `sre-readonly.sh`:

```bash
#!/usr/bin/env bash
if [[ "$1" == "kubectl" ]]; then
  echo "BLOCKED: use dtctl ou AWS CLI"
  exit 1
fi
exec "$@"
```

---

## 6. Documentação enxuta

O README principal deve ser direto e apontar para guias especializados.

Exemplo:

```md
- `dynatrace/README.md` — Guia de uso para Dynatrace
- `lambda/README.md` — Guia de uso para Lambda AWS
- `python/README.md` — Guia de uso para revisao de projetos Python
```

Cada README específico deve conter:

- o que o script faz
- como executar rapidamente
- onde configurar tokens/credenciais
- como diagnosticar problemas
- o fluxo macro da investigação

---

## 7. Exemplos de uso

### AWS Lambda

```bash
lambda/investigar-lambda minha-lambda
lambda/investigar-lambda minha-lambda --window=48h
lambda/investigar-lambda minha-lambda --from="20-05-2026 10:00" --to="20-05-2026 14:00"
```

### Dynatrace

```bash
cd dynatrace
./install.sh
export PATH="$HOME/bin:$PATH"
view_dyna_incidents service-name
view_dyna_incidents service-name --window=1h
view_dyna_incidents service-name --diagnose
```

---

## Por que este padrão funciona

- separa preparação do ambiente da execução cognitiva
- torna a solução reutilizável em diferentes casos
- mantém a segurança com MCP
- reduz a curva de aprendizado
- diminui erros em incidentes

---

## Conclusão

O padrão **Bootstrap → Agent → Prompt → MCP → Documentação** oferece um caminho consistente para entregar soluções de investigação automatizada. Ele transforma documentação estática em um fluxo operacional vivo, seguro e fácil de adotar.

Pronto para replicar esse padrão em novos casos? Comece com um script de bootstrap simples e copie o modelo de agente/prompt para cada novo domínio.
