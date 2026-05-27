# Investigador de Lambdas (resumo)

Este diretório contém um script de conveniência (`investigar-lambda`) e templates MCP/agents para preparar uma investigação automatizada de Lambdas AWS e abrir um workspace pronto para análise com GitHub Copilot.

O objetivo: reduzir passos manuais — o script copia templates, prepara um workspace local e abre o VS Code com o contexto da investigação.

Pré-requisitos
- `aws` CLI no PATH
- `code` (VS Code CLI) recomendado para abrir o workspace automaticamente

Uso rápido
```bash
# Preparar e abrir workspace de investigação para a Lambda 'minha-lambda'
lambda/investigar-lambda minha-lambda

# Janela relativa (ex: 48h)
lambda/investigar-lambda minha-lambda --window=48h

# Intervalo absoluto
lambda/investigar-lambda minha-lambda --from="20-05-2026 10:00" --to="20-05-2026 14:00"
```

O que o script faz
- Valida pré-requisitos (`aws`, formatos de data)
- Copia templates MCP/agents para `~/Documents/investigacao/lambda` (se necessário)
- Gera um arquivo `.investigation-context` com o nome da Lambda e intervalo de tempo
- Abre o workspace no VS Code (se `code` estiver disponível)

Após abrir o workspace
- No Copilot Chat do VS Code, invoque o agente com `@investigar-lambda <nome>` para executar a investigação automatizada.
- Os templates geram um relatório Markdown e, opcionalmente, PDF (se `pandoc` + LaTeX estiverem disponíveis).

Personalização
- Os templates estão em `lambda/templates/` — edite `github/agents` e `github/prompts` se quiser adaptar o agente.

Observações
- O script não envia nada para redes externas por si só; ele prepara dados locais para investigação (logs, métricas via AWS APIs) e usa os agentes/templatess no workspace.

Para instruções mais avançadas sobre agents e integração MCP, veja os templates em `lambda/templates/`.
