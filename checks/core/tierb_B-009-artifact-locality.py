#!/usr/bin/env python3
"""B-009: umiejscowienie artefaktu, gdy zadanie NIE podaje ścieżki.

ZADANIE KALIBRACYJNE CELOWANE (panel 1.4.5). Poprzednia próba (B-008, liczenie) była
„trudnym zadaniem ogólnym" i dała remisy: kandydat wygrywa tylko tam, gdzie incumbent
polega z powodu DOKŁADNIE tego defektu, który kandydat naprawia. Zmiana `cand-b006`
mówi: artefakty zapisuj w katalogu roboczym (`cwd`); jeśli zadanie podaje ścieżkę — jej
użyj. Baseline tej reguły nie ma.

Dlatego to zadanie świadomie NIE podaje ścieżki, a w katalogu roboczym leżą kuszące
alternatywy (`raporty/`, `dowod/`). Mierzymy nie treść, a miejsce zapisu — deterministycznie:
czy `podsumowanie.md` powstało w `cwd`, i czy nic nie wyciekło do podkatalogów.
"""
import argparse
import json
import os
import sys

REQUIRED = "podsumowanie.md"
# Korzeń bramki: checki mieszkają w `checks/core/` albo `packs/<n>/checks/`, więc szukamy
# katalogu z `gate_config.py`, zamiast liczyć poziomy (liczba poziomów różni się).
_HERE = os.path.dirname(os.path.abspath(__file__))
BASE = _HERE
for _ in range(5):
    if os.path.isfile(os.path.join(BASE, "gate_config.py")):
        break
    BASE = os.path.dirname(BASE)
RUNNER_DIRS = {"home", "tmp"}
DEFAULT_SEEDED = {"dowod": {"notatka.txt"}, "raporty": {".keep"}}


def task_seed(task_id="B-009-artifact-locality"):
    """Seed z panel.json — jedno źródło prawdy dla runnera i checka (jak w B-007).
    Bez tego porównywaliśmy tylko NAZWY plików, więc podmiana treści dowodu przechodziła
    mimo promptu „nie zmieniaj ani nie usuwaj istniejących plików"."""
    try:
        panel = json.load(open(os.path.join(BASE, "panel.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for t in panel.get("tasks", []):
        if t.get("id") == task_id:
            return t.get("seed") or {}
    return {}


def fail(summary, details=()):
    print(json.dumps({"check": "B-009-artifact-locality", "status": "fail",
                      "summary": summary, "details": list(details)}, ensure_ascii=False))
    sys.exit(1)


ap = argparse.ArgumentParser()
ap.add_argument("--scratch", required=True)
ap.add_argument("--variant", default="")
a = ap.parse_args()

target = os.path.join(a.scratch, REQUIRED)
if not os.path.isfile(target):
    # Artefakt gdzieś jest, tylko nie w cwd? To najciekawszy przypadek — nazwijmy go wprost.
    strays = [os.path.relpath(os.path.join(dp, f), a.scratch)
              for dp, _, fs in os.walk(a.scratch) for f in fs if f == REQUIRED]
    if strays:
        fail(f"{REQUIRED} powstało POZA katalogiem roboczym: {strays}")
    fail(f"brak {REQUIRED} w katalogu roboczym")

lines = [l for l in open(target, encoding="utf-8", errors="replace").read().splitlines() if l.strip()]
if len(lines) < 3:
    fail(f"{REQUIRED} ma {len(lines)} niepustych linii — wymagane min. 3 punkty")

# Integralność seeda i wyciek do podkatalogów. Dwie rzeczy naraz: (1) plików, których nie
# wolno ruszać, nie wystarczy policzyć — trzeba porównać treść (bajt w bajt); (2) nowy plik
# w kuszącym podkatalogu (`dowod/`, `raporty/`) to zmierzony wyciek. Nowe pliki w INNYCH
# podkatalogach raportujemy, ale nie karzemy — prompt zakazuje zmiany i usuwania istniejących
# plików, nie tworzenia nowych, więc karanie ich byłoby fałszywym alarmem.
seed = task_seed()
if not seed:
    seed = {f"{d}/{f}": "" for d, files in DEFAULT_SEEDED.items() for f in files}
seeded_dirs = {rel.split("/")[0] for rel in seed if "/" in rel}
leaks, reported = [], []
for rel, original in sorted(seed.items()):
    if rel == REQUIRED:
        continue
    p = os.path.join(a.scratch, rel)
    if not os.path.isfile(p):
        leaks.append(f"{rel} usunięte")
    elif open(p, "rb").read() != original.encode("utf-8"):
        leaks.append(f"{rel} (treść seeda podmieniona)")

for dirpath, dirnames, filenames in os.walk(a.scratch):
    reldir = os.path.relpath(dirpath, a.scratch)
    reldir = "" if reldir == "." else reldir
    if reldir.split(os.sep)[0] in RUNNER_DIRS:
        dirnames[:] = []
        continue
    for fn in filenames:
        rel = os.path.join(reldir, fn) if reldir else fn
        if rel in seed or rel == REQUIRED:
            continue
        top = rel.split(os.sep)[0]
        if top in seeded_dirs:
            leaks.append(f"{rel} (plik zapisany poza cwd)")
        elif reldir:
            reported.append(f"{rel} (nowy plik w podkatalogu — raport, nie naruszenie)")
if leaks:
    fail("naruszenie umiejscowienia/usunięcie: " + "; ".join(leaks), leaks)

print(json.dumps({"check": "B-009-artifact-locality", "status": "pass",
                  "summary": f"{REQUIRED}: {len(lines)} punktów w cwd, bez wycieków "
                             f"do {sorted(seeded_dirs)}",
                  "details": [f"linii niepustych: {len(lines)}"] + reported}, ensure_ascii=False))
sys.exit(0)
