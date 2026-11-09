#!/usr/bin/env python3
"""A-005: fail-closed integralność całego panelu i kotwicy Git."""
import argparse
import hashlib
import json
import os
import subprocess
import sys

# Domyślna kotwica: BRAK. Kotwicę podaje runner z `gate.config.json` (F2) — check nie może
# zakładać, że użytkownik ma akurat nasze repozytorium kotwicy.
REPO = ""
REPO_PATH = "panel.lock"
LOCKED_SUFFIXES = {".py", ".sh", ".json"}
REQUIRED_FILES = {
    "panel.json",
    "harness_eval.py",
    "gate_config.py",
    "panel_lock.py",
    "eval_version.py",
    "tier_b.py",
    "sandbox_landlock.py",
    "sandbox_selftest.py",
    "ledger_check.py",
    "skill_telemetry.py",
    "skill_drift.py",
    "gate_snapshot.sh",
    # F3: checki mieszkają w rdzeniu (`checks/core/`), zadania paczek w `packs/<n>/checks/`.
    "checks/core/check_panel_integrity.py",
}


def panel_checks(base):
    """(ścieżki checków z panelu rdzenia i WSZYSTKICH paczek na dysku, problemy).

    Bierzemy także paczki nieaktywne: integralność dotyczy plików w drzewie, a nie tego, co
    akurat włączone — inaczej wyłączona paczka byłaby szarą strefą poza manifestem.
    """
    checks, problems = [], []
    files = [os.path.join(base, "panel.json")]
    pdir = os.path.join(base, "packs")
    if os.path.isdir(pdir):
        files += [os.path.join(pdir, n, "panel.d.json") for n in sorted(os.listdir(pdir))]
    for path in files:
        if not os.path.isfile(path):
            continue
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            problems.append(f"{os.path.relpath(path, base)}: nieczytelny ({type(e).__name__})")
            continue
        for t in data.get("tasks") or []:
            if t.get("check"):
                checks.append(t["check"])
    return checks, problems


def emit(status, summary, details=None, code=""):
    """Kontrakt: status + powód + `code`, po którym warstwa komunikatów dobiera język i instrukcję.

    `TAMPERING` zarezerwowane dla ROZJAZDU TREŚCI. Brak manifestu i brak kotwicy to nie atak —
    to stan do naprawy jedną komendą, więc mają własne kody (R-UX5).
    """
    print(json.dumps({"check": "A-005-panel-integrity", "status": status, "code": code,
                      "summary": summary, "details": details or []}, ensure_ascii=False))
    return 1 if status == "fail" else 0


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def expected_paths(base):
    paths = set()
    for root, dirs, filenames in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in {".git", "__pycache__"})
        for filename in filenames:
            if filename == "panel.lock":
                continue
            if os.path.splitext(filename)[1] not in LOCKED_SUFFIXES:
                continue
            paths.add(os.path.relpath(os.path.join(root, filename), base).replace(os.sep, "/"))
    return paths


def audit(base, repo, repo_path):
    errors, details = [], []
    lock_path = os.path.join(base, "panel.lock")
    if not os.path.isfile(lock_path):
        return {"status": "fail", "code": "LOCK_MISSING",
                "summary": "brak manifestu integralności (panel.lock) — przezbrój bramkę: "
                           "`harness-gate --fix`",
                "details": [f"szukano: {lock_path}"]}

    try:
        with open(lock_path, "rb") as f:
            raw_lock = f.read()
        lock = json.loads(raw_lock.decode("utf-8"))
    except Exception as e:
        return {"status": "fail", "summary": f"TAMPERING_DETECTED: panel.lock nieparsowalny ({type(e).__name__})",
                "details": [str(e)]}
    if not isinstance(lock, dict):
        return {"status": "fail", "summary": "TAMPERING_DETECTED: panel.lock musi być obiektem JSON",
                "details": []}

    if lock.get("schema_version") != 2:
        errors.append("panel.lock nie ma wymaganego schema_version=2")
    files = lock.get("files")
    if not isinstance(files, dict):
        files = {}
        errors.append("panel.lock nie zawiera mapy files")

    try:
        expected = expected_paths(base)
        missing_required = sorted(REQUIRED_FILES - expected)
        if missing_required:
            errors.extend(f"brak wymaganego pliku: {p}" for p in missing_required)
        # KOMPLETNOŚĆ ZADAŃ (F3): każdy check wskazany przez panel (rdzeń + paczki) musi leżeć
        # w drzewie objętym manifestem. Bez tego zadanie mogło wskazywać kod poza kontrolą
        # integralności, a panel wyglądał na kompletny (ta sama klasa co „lock bez strażnika").
        task_checks, dc_problems = panel_checks(base)
        errors.extend(dc_problems)
        for rel in sorted(set(task_checks)):
            if rel not in expected:
                errors.append(f"panel wskazuje check poza manifestem albo nieistniejący: {rel}")
        lock_paths = set(files)
        errors.extend(f"brak w locku: {p}" for p in sorted(expected - lock_paths))
        errors.extend(f"wpis nie odpowiada plikowi: {p}" for p in sorted(lock_paths - expected))
        for rel in sorted(expected & lock_paths):
            path = os.path.join(base, *rel.split("/"))
            try:
                got = sha(path)
            except OSError as e:
                errors.append(f"nie można odczytać {rel}: {type(e).__name__}")
                continue
            if got != files[rel]:
                errors.append(f"TAMPERING_DETECTED {rel}: {got[:12]} != {str(files[rel])[:12]}")
        details.append(f"chronione pliki kodu/konfiguracji: {len(expected)}")
    except Exception as e:
        errors.append(f"nie można wyliczyć plików bramki: {type(e).__name__}: {e}")

    try:
        with open(os.path.join(base, "panel.json"), encoding="utf-8") as f:
            panel = json.load(f)
        if lock.get("panel_version") != panel.get("panel_version"):
            errors.append("panel_version w locku różni się od panel.json")
        details.append(f"panel_version: {panel.get('panel_version')}")
    except Exception as e:
        errors.append(f"panel.json nieczytelny: {type(e).__name__}")

    if not repo:
        # Kotwica wyłączona JAWNYM wpisem konfiguracji (`anchor.mode: "none"`) — porównania
        # z git HEAD nie ma, a fakt jest widoczny w werdykcie, nie przemilczany.
        details.append("kotwica git wyłączona jawnym wpisem konfiguracji (anchor.mode = none) "
                       "— integralność liczona wyłącznie z panel.lock")
    else:
        try:
            head = subprocess.run(
                ["git", "-C", repo, "show", f"HEAD:{repo_path}"],
                capture_output=True, timeout=20, check=False,
            )
            if head.returncode != 0:
                errors.append(f"panel.lock nieobecny w git HEAD: {repo_path} "
                              f"(naprawa: `harness-gate --fix`)")
            elif head.stdout != raw_lock:
                errors.append("panel.lock na dysku różni się od wersji w git HEAD")
            else:
                details.append("panel.lock zgodny z git HEAD")
        except Exception as e:
            errors.append(f"nie można zweryfikować kotwicy Git: {type(e).__name__}: {e}")

    if errors:
        return {"status": "fail", "code": "LOCK_DRIFT",
                "summary": "TAMPERING_DETECTED / LOCK_DRIFT: " + "; ".join(errors),
                "details": details}
    anchor_note = "zgodne z kotwicą Git" if repo else "zgodne z manifestem (bez kotwicy git)"
    return {"status": "pass", "code": "OK",
            "summary": f"panel, runner, checki i panel.lock {anchor_note}",
            "details": details}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=os.path.expanduser("~/.hermes"))
    ap.add_argument("--repo", default=REPO, help="repozytorium będące kotwicą Git")
    ap.add_argument("--repo-path", default=REPO_PATH, help="ścieżka panel.lock wewnątrz repo")
    args = ap.parse_args()
    base = os.path.join(args.home, "scripts", "harness_eval")
    result = audit(base, args.repo, args.repo_path)
    return emit(result["status"], result["summary"], result["details"],
                result.get("code", ""))


if __name__ == "__main__":
    sys.exit(main())
