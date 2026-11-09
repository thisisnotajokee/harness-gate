#!/bin/sh
# harness-gate — jedna komenda do zapamiętania (R-UX6).
#
# Po co dowiązanie, a nie „uruchom python3 scripts/harness_eval/harness_eval.py":
# użytkownik ma zapamiętać JEDNO słowo i nie musi wiedzieć, gdzie leży kod ani z jakiego
# katalogu go wołać. Skrypt liczy własną ścieżkę (`$0`), więc działa z dowolnego katalogu.
#
# Użycie:
#   harness-gate              # panel po ludzku (5-6 linii)
#   harness-gate --verbose    # pełna tabela kontroli
#   harness-gate --help       # pomoc
#
# Uwaga: bramka mierzy WYŁĄCZNIE drzewo, z którego sama pochodzi (tree_guard). W repo
# źródłowym uruchom ją z zainstalowanej kopii (`~/.hermes/scripts/harness_eval`) — samo
# `bin/harness-gate.sh` w repo odmówi z FOREIGN_BASE. Naprawa (`--fix`) wchodzi z F4.
#
# Kod wyjścia jest kodem runnera: 0 = nic nie wymaga uwagi, 1 = coś jest do poprawy.
set -eu

# `$0` bywa DOWIĄZANIEM (`~/.local/bin/harness-gate` → tu), więc bez rozwiązania symlinku
# `dirname $0` daje katalog linku i skrypt szuka bramki o poziom wyżej niż trzeba
# (`~/.local/harness_eval.py`). `readlink -f` rozwiązuje łańcuch; fallback dla systemów,
# gdzie go nie ma (wtedy `$0` — ścieżka bez dowiązania).
SELF="$0"
if command -v readlink >/dev/null 2>&1; then
    SELF=$(readlink -f -- "$0" 2>/dev/null || printf '%s' "$0")
fi
HERE=$(CDPATH= cd -- "$(dirname -- "$SELF")" && pwd)
GATE="$HERE/../harness_eval.py"

if [ ! -f "$GATE" ]; then
    echo "harness-gate: nie znajduję bramki ($GATE)" >&2
    exit 2
fi

PY=${HARNESS_GATE_PYTHON:-python3}
# Wektor `.pyc`: podmieniony bytecode w drzewie bywa importowany mimo zgodnego `.py`.
# Cache przekierowany poza drzewo + zapis wyłączony (druga linia obrony jest w runnerze).
PYTHONDONTWRITEBYTECODE=1
PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/hermes-gate-pycache"
export PYTHONDONTWRITEBYTECODE PYTHONPYCACHEPREFIX
export HARNESS_GATE_PYCACHE=1
exec "$PY" "$GATE" "$@"
