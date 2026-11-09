#!/usr/bin/env python3
"""A-001: kompilacja wszystkich skryptów Pythona w skills/ (bez .archive).
Kompilacja w pamięci (compile()) — nie tworzy __pycache__, więc jest bezpieczna
również pod sandboxem z zablokowanym zapisem."""
import argparse, os, sys

def emit(name, status, summary, details=None):
    """Kontrakt checka: jedna linia JSON na stdout, exit 1 gdy fail."""
    import json as _j, sys as _s
    print(_j.dumps({"check": name, "status": status, "summary": summary,
                    "details": details or []}, ensure_ascii=False))
    _s.exit(1 if status == "fail" else 0)

SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=os.path.expanduser("~/.hermes"))
    ap.add_argument("--variant", default="")
    a = ap.parse_args()
    root = a.variant or a.home
    roots = [os.path.join(root, "skills"), os.path.join(root, "scripts")]
    roots = [r for r in roots if os.path.isdir(r)]
    if not roots:
        emit("A-001-skill-scripts-compile", "skip", "brak katalogów skills/ i scripts/")
    fails, checked = [], 0
    for dirpath, dirnames, filenames in [x for r in roots for x in os.walk(r)]:
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        parts = dirpath.split(os.sep)
        # Pomijamy wyłącznie WŁASNY katalog checków bramki (część testów celowo zawiera
        # niepoprawny kod). Wcześniej warunek był dopasowaniem PODCIĄGU, więc każdy katalog
        # typu `checks_extra` — a nawet ścieżka zawierająca ten ciąg gdziekolwiek — znikał
        # z egzaminu razem z błędnymi skryptami w środku.
        if ".archive" in parts or parts[-2:] == ["harness_eval", "checks"]:
            continue
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            checked += 1
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    src = f.read()
                compile(src, p, "exec")
            except SyntaxError as e:
                fails.append(f"{os.path.relpath(p, root)}:{e.lineno}: {e.msg}")
            except Exception as e:
                fails.append(f"{os.path.relpath(p, root)}: {type(e).__name__}: {e}")
    if fails:
        emit("A-001-skill-scripts-compile", "fail",
             f"{len(fails)} z {checked} skryptów nie kompiluje się", fails)
    # Pusty zbiór to nie sukces, a brak egzaminu: kandydat mógł skasować wszystkie skrypty,
    # a raport mówiłby „0 skryptów kompiluje się" i przechodziłby jako czysto.
    if checked == 0:
        emit("A-001-skill-scripts-compile", "fail",
             "nie znaleziono żadnego skryptu .py — nic nie zostało zweryfikowane")
    emit("A-001-skill-scripts-compile", "pass", f"{checked} skryptów kompiluje się")


if __name__ == "__main__":
    main()
