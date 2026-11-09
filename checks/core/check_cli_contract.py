#!/usr/bin/env python3
"""A-009: kontrakt z Hermesem — binarka odpowiada i ma flagi, na których stoi rollout.

Po co: rollouty (Tier B) wołają `hermes` z konkretnymi flagami (`--in`, `-z`, `--yolo`, `-m`,
`--provider`). Gdy binarki nie ma, jest nieuruchamialna albo po aktualizacji zgubiła flagę,
dotychczasowy skutek to cichy `INFRA_ERROR` w rolloutcie — czyli „nie wiem", które wygląda jak
brak problemu. Ten check zamienia to w jawny werdykt na wejściu: albo kontrakt trzyma, albo
`fail` z nazwą brakującej flagi.

TRZY DECYZJE (architekt A6, P9):
  * BINARKA Z KONFIGURACJI BRAMKI (`gate.config.json → cli.bin`, domyślnie `hermes`), a nie
    zaszyta ścieżka — bramka działa u każdego użytkownika Hermesa, nie tylko u nas.
  * `cli.version_max` to WYŁĄCZNIE OSTRZEŻENIE. Wyższa wersja = `pass` z notą w `details`.
    Hard pin zrobiłby z bramki czerwoną tapetę po każdym legitimate update Hermesa.
  * NIECZYTELNA konfiguracja = `fail`. „Nie wiem, jakie są wymagania" nie jest „zgodne" —
    tak samo jak brak binarki nie jest „brakiem problemu".

Drzewo mierzone (`--variant` gdy podane, inaczej `--home`) musi być instalacją Hermesa: katalog
`skills/` i plik `state.db`. Bez tego nie ma czego egzaminować — to nie jest dom Hermesa.

Zero sieci, zero zależności poza stdlib, żadnych `shell=True`.
"""
import argparse
import json
import os
import re
import subprocess
import sys


# Korzeń bramki: checki mieszkają w `checks/core/` albo `packs/<n>/checks/`, więc szukamy
# katalogu z `gate_config.py`, zamiast liczyć poziomy (liczba poziomów różni się).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
for _ in range(5):
    if os.path.isfile(os.path.join(_ROOT, "gate_config.py")):
        break
    _ROOT = os.path.dirname(_ROOT)
sys.path.insert(0, _ROOT)

NAME = "A-009-cli-contract"
TIMEOUT_S = 20          # ~20 s jak w specyfikacji; rollout nie może wisieć na zawieszonej binerce


def emit(status, summary, details=None):
    """Kontrakt checka: jedna linia JSON na stdout, exit 1 tylko dla fail."""
    print(json.dumps({"check": NAME, "status": status, "summary": summary,
                      "details": details or []}, ensure_ascii=False))
    return 1 if status == "fail" else 0


def gate_cli():
    """(cli, błąd) — `cli` z konfiguracji BRAMKI, nigdy z drzewa mierzonego. Fail-closed."""
    try:
        import gate_config
        cfg = gate_config.for_checks()
    except Exception as e:                      # brak modułu reguł = nie znam wymagań
        return None, f"nie mogę wczytać konfiguracji bramki: {type(e).__name__}: {e}"
    if not cfg["ok"]:
        return None, cfg["error"]
    cli = cfg.get("cli") or {}
    return {"bin": (cli.get("bin") or "hermes"),
            "required_flags": list(cli.get("required_flags") or []),
            "version_max": (cli.get("version_max") or "")}, ""


def resolve_bin(binname):
    """Ścieżka binarki: `PATH` → `~/.local/bin` → nazwa jak podana (błąd pokaże uruchomienie)."""
    try:
        import gate_config
        return gate_config.resolve_bin(binname)
    except Exception:
        import shutil
        found = shutil.which(binname)
        return found or binname


def run(cmd, env=None):
    """(rc, tekst, błąd_uruchomienia) — bez powłoki, z przechwyceniem wyjścia i twardym timeoutem.

    `HOME` dla binarki: gdy harness pomiarowy podstawia `HOME` na mierzone drzewo (światy
    testowe), binarka Hermesa nie może wystartować w obcym domu (zmierzone: `--help` → rc=1,
    88 znaków błędu) i kontrakt CLI staje się niemierzalny. Dlatego harness może JAWNIE podać
    prawdziwy dom zmienną `HARNESS_GATE_MACHINE_HOME`; użytkownik jej nie ustawia i wtedy
    binarka dostaje jego własny `HOME`.
    """
    child_env = dict(os.environ)
    machine_home = os.environ.get("HARNESS_GATE_MACHINE_HOME", "").strip()
    if machine_home:
        child_env["HOME"] = machine_home
    if env:
        child_env.update(env)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S,
                           shell=False, env=child_env)
    except subprocess.TimeoutExpired:
        return None, "", f"przekroczył limit {TIMEOUT_S} s"
    except (OSError, ValueError) as e:
        return None, "", f"{type(e).__name__}: {e}"
    text = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
    return p.returncode, text, ""


def flag_present(flag, text):
    """Flaga jako osobny token — `-m` NIE może zaliczyć się na `--model`.

    Granice `(?<![\\w-])…(?![\\w-])`: sąsiad nie może być znakiem słowa ani myślnikiem, więc
    `-m` wewnątrz `--model` (poprzedzone `-`) nie łapie, a `-m,` i `[-m MODEL]` łapią.
    """
    return re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", text) is not None


def parse_version(text):
    """(krotka, tekst_wersji) z wyjścia `--version`; `(None, '')` gdy nie da się sparsować."""
    m = re.search(r"\bv?(\d+)\.(\d+)\.(\d+)", text)
    if not m:
        return None, ""
    return tuple(int(g) for g in m.groups()), m.group(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=os.path.expanduser("~/.hermes"))
    ap.add_argument("--variant", default="")
    a = ap.parse_args()
    root = a.variant or a.home                  # mierzone drzewo: wariant ma pierwszeństwo

    cli, cfg_err = gate_cli()
    if cli is None:
        return emit("fail", f"reguł bramki nie da się odczytać: {cfg_err}")

    binname = cli["bin"]
    details = [f"binarka: {binname}", f"drzewo mierzone: {root}"]

    # 4. Drzewo musi być instalacją Hermesa. Brak katalogu/pliku = to nie jest dom Hermesa,
    #    więc kontrakt „z Hermesem" nie ma z czym się zgadzać.
    missing = []
    if not os.path.isdir(os.path.join(root, "skills")):
        missing.append("katalog skills/")
    if not os.path.isfile(os.path.join(root, "state.db")):
        missing.append("plik state.db")
    if missing:
        return emit("fail",
                    f"{root} nie wygląda na instalację Hermesa: brak " + ", ".join(missing),
                    details + [f"brak: {m}" for m in missing])

    # 1. Znajdź binarkę. Nieuruchamialna/nieistniejąca = fail (nie cichy INFRA_ERROR).
    path = resolve_bin(binname)
    details.append(f"rozwiązana ścieżka: {path}")
    if not binname.strip():
        return emit("fail", "konfiguracja bramki nie podaje binarki Hermesa (cli.bin pusty)", details)

    # 2. Wołamy `--version` i `--help`.
    rc_v, out_v, err_v = run([path, "--version"])
    if err_v:
        return emit("fail", f"nie da się uruchomić binarki {path!r}: {err_v}",
                    details + [f"`{path} --version`: {err_v}"])
    rc_h, out_h, err_h = run([path, "--help"])
    if err_h:
        return emit("fail", f"nie da się uruchomić binarki {path!r}: {err_h}",
                    details + [f"`{path} --help`: {err_h}"])

    details.append(f"`--version` rc={rc_v}: {(out_v.strip().splitlines() or ['(brak wyjścia)'])[0][:120]}")
    details.append(f"`--help` rc={rc_h}, {len(out_h)} znaków wyjścia")

    # DIAGNOZA (poprawka po pomiarze w świecie, F3): „binarka nie odpowiedziała" to INNA
    # przyczyna niż „zna help, ale bez flagi". Zmierzone: przy podstawionym `HOME` binarka
    # Hermesa kończy `--help` kodem 1 i 88 znakami błędu — a check mówił wtedy „nie zna
    # wymaganej flagi: --in, -z, …", czyli wysyłał użytkownika szukać niewłaściwej przyczyny.
    def first_line(text):
        return ((text.strip().splitlines() or ["(brak wyjścia)"])[0])[:160]

    if rc_v != 0 or not out_v.strip():
        return emit("fail", f"binarka {path!r} nie odpowiedziała na `--version` (rc={rc_v})",
                    details + [f"`--version`: {first_line(out_v)}"])
    if rc_h != 0 or not out_h.strip():
        return emit("fail",
                    f"binarka {path!r} nie odpowiedziała na `--help` (rc={rc_h}) — "
                    f"nie mogę sprawdzić wymaganych flag",
                    details + [f"`--help`: {first_line(out_h)}"])

    # 3. Każda wymagana flaga musi być w `--help`.
    flags = cli["required_flags"]
    help_text = out_h or ""
    absent = [f for f in flags if not flag_present(f, help_text)]
    if absent:
        return emit("fail",
                    f"binarka {binname} nie zna wymaganej flagi: " + ", ".join(absent),
                    details + [f"brakuje w `--help`: {f}" for f in absent])

    # 5. version_max = tylko ostrzeżenie. Wyższa wersja zostaje `pass`.
    warnings = []
    vmax = cli["version_max"].strip()
    if vmax:
        cur, cur_txt = parse_version(out_v)
        mx, _ = parse_version(vmax)
        if cur is None:
            warnings.append(f"nie mogę sparsować wersji z `--version` {out_v.strip()[:80]!r} "
                            f"— porównania z version_max={vmax} brak")
        elif mx is None:
            warnings.append(f"`version_max`={vmax!r} nieparsowalny — pomijam porównanie")
        elif cur > mx:
            warnings.append(f"wersja {cur_txt} > version_max {vmax} — "
                            f"sprawdź, czy rollouty nie polegają na usuniętym zachowaniu "
                            f"(to ostrzeżenie, nie błąd)")
        else:
            details.append(f"wersja {cur_txt} ≤ version_max {vmax}")

    return emit("pass",
                f"binarka {binname} odpowiada i zna wszystkie wymagane flagi "
                f"({len(flags)})",
                details + warnings)


if __name__ == "__main__":
    sys.exit(main())
