# investigar-code

Prepara um clone Git de um projeto Python para uso do agente `@investigar-code` no VS Code. O agente revisa qualidade, PEP 8, tipagem, testes, dependencias e seguranca.

## Instalacao

A partir deste clone do repositorio:

```bash
./python/install.sh
```

O comando cria o atalho `~/bin/investigar-code`. Use `--force` para substituir um atalho existente ou `--bin-dir DIR` para escolher outro diretorio.

Garanta que `~/bin` esteja no `PATH`:

```bash
export PATH="$HOME/bin:$PATH"
```

## Uso

Entre em qualquer diretorio de um clone Git Python e execute:

```bash
investigar-code
```

O script encontra a raiz Git mesmo quando executado em um subdiretorio, instala ou atualiza os arquivos `.github/`, `.vscode/settings.json` e `.python-code-context`, e abre essa raiz no VS Code. Use `investigar-code --no-open` para somente preparar os arquivos.

No Copilot Chat, invoque `@investigar-code`.

## Escopo da revisao

O prompt orienta a verificar PEP 8/257/484, lint, formatacao, tipos, testes, dependencias, segredos, validacao de entrada, injecao, desserializacao, travessia de caminho, SSRF, subprocessos, permissoes e tratamento de erros. O agente nao instala ferramentas nem altera codigo sem autorizacao.