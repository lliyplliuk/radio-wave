#!/usr/bin/env bash
# Запуск «Радио из коллекции».
# Прокси-переменные снимаются согласно ТЗ (внешние обращения идут напрямую).
set -euo pipefail

launcher="$(readlink -f -- "${BASH_SOURCE[0]}")"
cd "$(dirname -- "$launcher")"

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY
unset http_proxy https_proxy all_proxy

PYTHON=".venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    PYTHON="/usr/bin/python3"
fi

exec "$PYTHON" -m radio_wave "$@"
