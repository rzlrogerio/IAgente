# Workspace de Incidentes Dynatrace

Este workspace contém dados de incidentes coletados automaticamente do Dynatrace.

## REGRA CRÍTICA — MCP OBRIGATÓRIO

**TODOS os comandos (aws, az, dtctl, pandoc) devem ser executados via `sre_run_command` (MCP sre-readonly).**
**NUNCA use `run_in_terminal`. O terminal está DESABILITADO neste workspace.**

Cada `sre_run_command` aceita APENAS UM comando simples. Sem pipes, sem `&&`, sem loops, sem `2>&1`.

## Tratamento de AUTH_EXPIRED

Se qualquer comando retornar `AUTH_EXPIRED`, **PARE** e informe ao usuário:
- Azure: "Execute `az login` no terminal e tente novamente"
- Dynatrace: "Execute `dtctl auth login` no terminal e tente novamente"
- AWS: "Execute `aws sso login --profile PROFILE` no terminal e tente novamente"

**NÃO continue análise com tokens expirados — os dados serão incompletos.**

## Estrutura

- Cada pasta em `/<nome-do-servico>/data/` contém análises de incidentes
- Os arquivos são nomeados com timestamp: `YYYYMMDD_HHMMSS_analysis.md`
- O arquivo mais recente (maior timestamp) é o incidente ativo/último coletado

## Contexto técnico

- **Plataforma**: Kubernetes (EKS na AWS) e Lambda
- **Monitoramento**: Dynatrace com Davis AI para detecção de anomalias
- **Métricas**: Response time, CPU, Memory, Throttling, Replicas
- **Evidências Davis**: root cause analysis, deployment changes, impact analysis

## AWS CLI — Resolução de Profile

Para encontrar o profile de uma conta específica, faça chamadas individuais:

1. `sre_run_command(command: "aws configure list-profiles")` → leia a lista
2. Para cada candidato: `sre_run_command(command: "aws configure get sso_account_id --profile NOME")`
3. Compare o resultado com a conta desejada

## Comandos de investigação (SOMENTE LEITURA via MCP)

```
sre_run_command(command: "aws logs filter-log-events --profile PROFILE --region REGION --log-group-name /aws/lambda/NOME --start-time EPOCH_MS --end-time EPOCH_MS --filter-pattern ERROR --limit 50", timeout: 60)
```

```
sre_run_command(command: "aws cloudwatch get-metric-statistics --profile PROFILE --region REGION --namespace AWS/Lambda --metric-name Duration --dimensions Name=FunctionName,Value=NOME --start-time ISO --end-time ISO --period 60 --statistics Average Maximum", timeout: 30)
```

```
sre_run_command(command: "aws cloudtrail lookup-events --profile PROFILE --region REGION --start-time ISO --end-time ISO --max-results 20", timeout: 60)
```

```
sre_run_command(command: "dtctl query 'fetch dt.entity.service | filter entity.name == \"NOME\" | limit 5' -o json", timeout: 90)
```

## ENTREGA — Salvar no arquivo

Após investigar, SALVE a análise no arquivo `*_analysis.md` usando `replace_string_in_file`.
Depois regenere o PDF:
```
sre_run_command(command: "pandoc CAMINHO.md -s -o CAMINHO.pdf --pdf-engine=xelatex -V mainfont=\"DejaVu Sans\"", timeout: 60)
```

**NÃO apresente análise longa no chat.** O chat é apenas para confirmação curta.

Responda sempre em **português brasileiro**.
