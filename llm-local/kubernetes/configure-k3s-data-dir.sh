#!/usr/bin/env bash
set -euo pipefail

readonly SOURCE="/home/rancher/k3s/data"
readonly TARGET="/general-use/1-lab/k8s/k3s"
readonly LINK="/var/lib/rancher/k3s"
readonly OVERRIDE="/etc/systemd/system/k3s.service.d/override.conf"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Execute este script como root: sudo $0" >&2
  exit 1
fi

if ! mountpoint -q /general-use; then
  echo "O volume /general-use não está montado; nenhum dado foi alterado." >&2
  exit 1
fi

if [[ "$(realpath -m "$TARGET")" != "$TARGET" ]]; then
  echo "O caminho de destino contém um link simbólico inesperado: $TARGET" >&2
  exit 1
fi

if systemctl is-active --quiet k3s; then
  echo "O serviço k3s está ativo. Pare-o antes de migrar os dados." >&2
  exit 1
fi

if [[ ! -d "$SOURCE" || ! -L "$LINK" || ! -f "$OVERRIDE" ]]; then
  echo "Instalação atual do k3s diferente do esperado; nenhum dado foi alterado." >&2
  exit 1
fi

current_link="$(readlink "$LINK")"
if [[ "$current_link" != "$SOURCE" && "$current_link" != "$TARGET" ]]; then
  echo "O link $LINK aponta para '$current_link', não para a origem ou destino esperados." >&2
  exit 1
fi

if ! grep -Fq -- "--data-dir $SOURCE" "$OVERRIDE" &&
   ! grep -Fq -- "--data-dir $TARGET" "$OVERRIDE"; then
  echo "A configuração $OVERRIDE não contém o data-dir esperado; revise-a manualmente." >&2
  exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync é necessário para copiar os dados preservando metadados." >&2
  exit 1
fi

mkdir -p "$TARGET"
chown root:root "$TARGET"
chmod 0755 "$TARGET"

if [[ "$SOURCE" != "$TARGET" ]]; then
  if [[ -n "$(find "$TARGET" -mindepth 1 -print -quit)" ]]; then
    verification="$(rsync -aHAXn --numeric-ids --delete --itemize-changes "$SOURCE/" "$TARGET/")"
    if [[ -n "$verification" ]]; then
      echo "O destino já contém dados diferentes da origem; nenhum arquivo foi sobrescrito." >&2
      exit 1
    fi
  else
    rsync -aHAX --numeric-ids "$SOURCE/" "$TARGET/"
  fi
fi

verification="$(rsync -aHAXn --numeric-ids --delete --itemize-changes "$SOURCE/" "$TARGET/")"
if [[ -n "$verification" ]]; then
  echo "A verificação da cópia falhou; a configuração do serviço não foi alterada." >&2
  printf '%s\n' "$verification" >&2
  exit 1
fi

timestamp="$(date +%Y%m%d%H%M%S)"
cp -a "$OVERRIDE" "$OVERRIDE.bak.$timestamp"

cat >"$OVERRIDE.tmp" <<EOF
[Unit]
RequiresMountsFor=$TARGET

[Service]
ExecStart=
ExecStart=/usr/local/bin/k3s server --write-kubeconfig-mode 644 --data-dir $TARGET
EOF
chmod 0644 "$OVERRIDE.tmp"
mv "$OVERRIDE.tmp" "$OVERRIDE"

if [[ "$current_link" != "$TARGET" ]]; then
  ln -sfnT "$TARGET" "$LINK"
fi

systemctl daemon-reload

echo "Dados copiados e verificados em: $TARGET"
echo "Origem preservada para rollback: $SOURCE"
echo "Link atualizado: $LINK -> $TARGET"
echo "Configuração ativa após a próxima inicialização do serviço:"
systemctl show k3s -p ExecStart -p RequiresMountsFor --no-pager
echo "O serviço não foi iniciado. Para iniciá-lo depois: sudo systemctl enable --now k3s"
