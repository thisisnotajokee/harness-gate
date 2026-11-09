#!/usr/bin/env bash
# gate_snapshot.sh — zwarty snapshot bramki dla dziennego raportu Flat-MOS.
#
# DLACZEGO ISTNIEJE: crony działają bez człowieka, więc terminal agenta bywa blokowany
# ("Command flagged as dangerous ... cron jobs run without a user present to approve it").
# Log z 2026-10-14 07:40 pokazuje, że planowy przebieg Flat-MOS tracił w ten sposób
# polecenia. Ten skrypt uruchamia SCHEDULER (pole `script` joba), nie agent, więc
# uprawnienia terminala przestają mieć znaczenie — stdout jest wstrzykiwany do promptu.
#
# WAŻNE (2026-11-09): snapshot woła JEDEN STABILNY ENTRYPOINT (`bin/harness-gate.sh`), a nie
# pojedyncze pliki checków. Zaszyta ścieżka `checks/check_panel_integrity.py` pękła, gdy F3
# przeniosło checki do `checks/core/` i `packs/<nazwa>/checks/` — do promptu dziennego raportu
# poszedł surowy błąd Pythona. Entrypoint przeżywa zmiany układu; tej klasy błędu pilnuje
# `tests/test_consumer_paths.py` w drzewie bramki.
set -u
# Dom bramki: `HERMES_HOME` wygrywa, `$HOME/.hermes` to tylko wartość domyślna.
# Zaszyte `$HOME/.hermes` psuło snapshot u każdego z niestandardowym domem (naprawa 2026-11-09).
E="${HERMES_HOME:-$HOME/.hermes}/scripts/harness_eval"
echo "SNAPSHOT BRAMKI $(date '+%Y-%m-%d %H:%M')"
# Sekcja panelu zastępuje dawne osobne `panel_lock.py --check` + `check_panel_integrity.py`:
# A-005 niesie ten sam fakt („panel, runner, checki i panel.lock zgodne z kotwicą Git”),
# a preflight bramki zgłasza brak kotwicy/rozjazd manifestu PRZED uruchomieniem checków.
echo "--- panel Tier A (entrypoint; A-005 = kotwica Git + manifest) ---"
if [ -x "$E/bin/harness-gate.sh" ]; then
  "$E/bin/harness-gate.sh" --panel --verbose 2>&1
else
  echo "(brak launchera — wołam runner wprost)"
  python3 "$E/harness_eval.py" --panel --verbose 2>&1
fi
echo "--- ledger + okno obserwacji ---"
python3 "$E/ledger_check.py" 2>&1
echo "--- zmiany skilli poza bramką (background review / drift) ---"
python3 "$E/skill_drift.py" --check --log 2>&1
echo "--- aktywacja skilli ---"
python3 "$E/skill_telemetry.py" --summary 2>&1
