# Dynatrace Incidents Tool (resumo)

Ferramenta CLI para buscar incidentes Dynatrace, enriquecer com dados (Classic API + DQL/dtctl), gerar relatório Markdown/PDF e preparar contexto para análise com GitHub Copilot.

Rápido: instale e execute
```bash
cd dynatrace
./install.sh        # cria `view_dyna_incidents` em $HOME/bin
export PATH="$HOME/bin:$PATH"  # se necessário
view_dyna_incidents <termo>
```

Principais pontos
- Tokens: configure `DYNATRACE_ENV_URL` e `DYNATRACE_API_TOKEN` no seu shell (ex.: `~/.zshrc`). O `DYNATRACE_PLATFORM_TOKEN` é opcional (usado para DQL/storage quando disponível).
- O script detecta automaticamente tokens e oferece instruções se algo faltar; use `--diagnose` para validar.
- `dtctl` é opcional, mas recomendado para consultas DQL profundas (topologia, eventos Davis). Se disponível, o script usa `dtctl` para enriquecer resultados.

Exemplos de export (single-tenant):
```bash
export DYNATRACE_ENV_URL="https://<tenant>.live.dynatrace.com"
export DYNATRACE_API_TOKEN="dt0c01.<seu-token>"
export DYNATRACE_PLATFORM_TOKEN="dt0s16.<seu-token>"   # opcional
```

Uso comum
```bash
# Buscar incidentes por termo (janela padrão definida em config.ini)
view_dyna_incidents service-name

# Janela relativa
view_dyna_incidents service-name --window=1h

# Intervalo absoluto
view_dyna_incidents service-name --from "01-05-2026 10:00" --to "01-05-2026 12:00"

# Diagnóstico de tokens/endpoint
view_dyna_incidents --diagnose
```

PDF e dependências
- A geração PDF é opcional. Para PDF via `pandoc` + LaTeX instale `pandoc` e um conjunto LaTeX (ex.: `texlive-xetex`). O script funciona sem PDF.

Configuração e templates
- `./install.sh` copia `config.ini.example` → `config.ini` e cria o comando `view_dyna_incidents`.
- Customize `dynatrace/config.ini` para ajustar janelas, limites e formatação de saída.

Dica rápida
- Se preferir evitar configuração manual, execute `view_dyna_incidents --diagnose` e siga as instruções rápidas exibidas.

Para detalhes, exemplos de queries dtctl e lista completa de scopes, mantenha este arquivo como referência, mas tente primeiro os comandos rápidos acima.
