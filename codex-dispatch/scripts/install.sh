#!/bin/sh
set -eu

scripts_dir=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd)
bin_dir=${CODEX_WORKER_BIN:-$HOME/.local/bin}
worker=$scripts_dir/codex-worker
link=$bin_dir/codex-worker

mkdir -p -- "$bin_dir"
if [ -e "$link" ] && [ ! -L "$link" ]; then
  printf '无法安装，已有同名文件或目录 %s\n' "$link" >&2
  exit 1
fi

chmod +x "$worker"
ln -sfn -- "$worker" "$link"
printf '已安装 %s -> %s\n' "$link" "$worker"

case ":${PATH:-}:" in
  *":$bin_dir:"*) ;;
  *) printf '请将 %s 加入 PATH\n' "$bin_dir" ;;
esac
