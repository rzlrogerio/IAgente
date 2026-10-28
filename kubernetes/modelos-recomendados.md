# Modelos recomendados para rodar no Ollama

Esta lista reúne os modelos mais adequados para uso interno em ambiente local, com foco em equilíbrio entre qualidade, velocidade e consumo de recursos.

## Prioridade 1: melhores para uso geral

- llama3.2
  - Excelente para chat, resumo e apoio operacional.
  - Boa relação custo x desempenho.
  - Leve para rodar em CPU.

- qwen2.5:7b
  - Muito bom para código, instruções e raciocínio.
  - Excelente para assistentes internos e automações.

- mistral:7b
  - Ótima qualidade geral em conversação.
  - Bom desempenho em tarefas de instrução e suporte.

## Prioridade 2: melhor qualidade com mais recurso

- qwen2.5:14b
  - Melhor resposta e maior capacidade analítica.
  - Requer mais memória e tempo de inferência.

- gemma2:9b
  - Boa qualidade geral e boa estabilidade.
  - Bom para uso em ambiente de assistente conversacional.

- deepseek-r1:7b
  - Boa capacidade de raciocínio.
  - Mais exigente em recursos e em tempo.

## Prioridade 3: modelos leves para hardware menor

- phi3:mini
  - Muito útil em ambientes com limite de CPU/memória.
  - Boa opção para suporte rápido e tarefas simples.

- llama3.2:3b
  - Muito leve e muito bom para uso local enxuto.
  - Boa opção quando o objetivo é baixo consumo.

## Recomendação prática para este ambiente

Para começar em uma máquina sem GPU, os melhores candidatos são:

1. llama3.2
2. qwen2.5:7b
3. mistral:7b
4. gemma2:9b

Se o ambiente tiver mais memória e CPU, vale testar:

1. qwen2.5:14b
2. deepseek-r1:7b

## Atualização dos modelos configurados

```bash
./update-models.sh
./update-models.sh --schedule weekly
./update-models.sh --schedule monthly
```

## Observações

O Ollama carrega o modelo em tempo de execução. Você pode trocar o modelo na
interface Open WebUI sem precisar recriar o deployment, desde que o modelo já
esteja baixado. O manifesto permite até `16Gi` de memória por padrão; ajuste
os recursos pelo ambiente conforme o hardware disponível.
