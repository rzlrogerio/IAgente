# Workspace de Investigação Lambda AWS

Este workspace contém investigações de Lambdas AWS coletadas via CLI.

## REGRA CRÍTICA — MCP OBRIGATÓRIO

**TODOS os comandos (aws, pandoc) devem ser executados via `sre_run_command` (MCP sre-readonly).**
**NUNCA use `run_in_terminal`. O terminal está DESABILITADO neste workspace.**

Cada `sre_run_command` aceita APENAS UM comando simples. Sem pipes, sem `&&`, sem loops, sem `2>&1`.

## Tratamento de AUTH_EXPIRED

Se qualquer comando retornar `AUTH_EXPIRED`, **PARE** e informe ao usuário:
- AWS: "Execute `aws sso login --profile PROFILE` no terminal e tente novamente"

**NÃO continue análise com tokens expirados — os dados serão incompletos.**

## Estrutura

- Cada pasta em `./investigacao-lambda/` contém relatórios de investigação
- Os arquivos são nomeados com: `NOME_LAMBDA_YYYYMMDD_HHMMSS.md`

## Contexto técnico

- **Plataforma**: AWS Lambda
- **Métricas**: Errors, Throttles, Duration, Invocations, ConcurrentExecutions
- **Eventos**: CloudTrail (deploys, config changes)
- **Logs**: CloudWatch Logs (erros, exceptions, timeouts)

## AWS CLI — Resolução de Profile

Para encontrar o profile de uma conta específica, faça chamadas individuais:

1. `sre_run_command(command: "aws configure list-profiles")` → leia a lista
2. Para cada candidato: `sre_run_command(command: "aws configure get sso_account_id --profile NOME")`
3. Compare o resultado com a conta desejada

## Comandos de investigação (SOMENTE LEITURA via MCP)

```
sre_run_command(command: "aws lambda list-functions --profile PROFILE --region us-east-1 --query \"Functions[?contains(FunctionName, 'NOME')].{Name:FunctionName,Runtime:Runtime,Timeout:Timeout,Memory:MemorySize,LastModified:LastModified}\" --output json", timeout: 60)
```

```
sre_run_command(command: "aws logs filter-log-events --profile PROFILE --region REGION --log-group-name /aws/lambda/NOME --start-time EPOCH_MS --end-time EPOCH_MS --filter-pattern ERROR --limit 50", timeout: 60)
```

```
sre_run_command(command: "aws cloudwatch get-metric-statistics --profile PROFILE --region REGION --namespace AWS/Lambda --metric-name Duration --dimensions Name=FunctionName,Value=NOME --start-time ISO --end-time ISO --period 3600 --statistics Average Maximum", timeout: 30)
```

```
sre_run_command(command: "aws cloudtrail lookup-events --profile PROFILE --region REGION --lookup-attributes AttributeKey=ResourceName,AttributeValue=NOME --start-time ISO --end-time ISO --max-results 20", timeout: 60)
```

```
sre_run_command(command: "aws cloudwatch describe-alarms-for-metric --profile PROFILE --region REGION --namespace AWS/Lambda --metric-name Errors --dimensions Name=FunctionName,Value=NOME", timeout: 30)
```

## ENTREGA — Salvar no arquivo

Após investigar, SALVE o relatório em markdown E gere PDF:
```
sre_run_command(command: "pandoc CAMINHO.md -s -o CAMINHO.pdf --pdf-engine=xelatex -V mainfont=\"DejaVu Sans\" -V geometry:margin=2cm", timeout: 60)
```

**NÃO apresente análise longa no chat.** O chat é apenas para confirmação curta.

Responda sempre em **português brasileiro**.
