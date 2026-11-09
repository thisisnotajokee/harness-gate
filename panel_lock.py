#!/usr/bin/env python3
"""Twórz i lokalnie weryfikuj panel.lock (Git anchor sprawdza A-005)."""
import argparse
import datetime
import hashlib
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
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
    # F3: checki mieszkają w rdzeniu (`checks/core/`), a zadania paczek w `packs/<n>/checks/`.
    # A-005 jest zadaniem rdzenia — bez niego nie ma strażnika kompletu plików.
    "checks/core/check_panel_integrity.py",
}


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def protected_paths(base):
    """Wszystkie pliki kodu i konfiguracji panelu poza samym lockiem."""
    paths = set()
    for root, dirs, filenames in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in {".git", "__pycache__"})
        for filename in filenames:
            if filename == "panel.lock":
                continue
            if os.path.splitext(filename)[1] not in LOCKED_SUFFIXES:
                continue
            path = os.path.join(root, filename)
            paths.add(os.path.relpath(path, base).replace(os.sep, "/"))
    missing = sorted(REQUIRED_FILES - paths)
    if missing:
        raise FileNotFoundError("brak wymaganych plików bramki: " + ", ".join(missing))
    return sorted(paths)


def collect(base):
    return {rel: sha(os.path.join(base, *rel.split("/")))
            for rel in protected_paths(base)}


def validate(base, lock):
    errors = []
    if not isinstance(lock, dict):
        return ["panel.lock musi być obiektem JSON"]
    if lock.get("schema_version") != 2:
        errors.append("nieobsługiwany schema_version (wymagane 2)")
    files = lock.get("files")
    if not isinstance(files, dict):
        return errors + ["brak mapy files w panel.lock"]
    try:
        expected = set(protected_paths(base))
    except Exception as e:
        return errors + [str(e)]
    actual = set(files)
    errors.extend(f"brak w locku: {p}" for p in sorted(expected - actual))
    errors.extend(f"nieoczekiwany wpis locka: {p}" for p in sorted(actual - expected))
    for rel in sorted(expected & actual):
        path = os.path.join(base, *rel.split("/"))
        got = sha(path)
        if got != files[rel]:
            errors.append(f"{rel}: hash {got[:12]} != lock {str(files[rel])[:12]}")
    try:
        with open(os.path.join(base, "panel.json"), encoding="utf-8") as f:
            panel = json.load(f)
        if lock.get("panel_version") != panel.get("panel_version"):
            errors.append("panel_version różni się od panel.json")
    except Exception as e:
        errors.append(f"panel.json nieparsowalny: {type(e).__name__}")
    return errors


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    base = os.path.abspath(args.base)
    lock_path = os.path.join(base, "panel.lock")

    if args.write:
        try:
            with open(os.path.join(base, "panel.json"), encoding="utf-8") as f:
                panel = json.load(f)
            files = collect(base)
        except Exception as e:
            print(f"BŁĄD: nie można zbudować panel.lock: {e}", file=sys.stderr)
            return 2
        doc = {
            "schema_version": 2,
            "panel_version": panel.get("panel_version"),
            "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "uwaga": "Zmiana pliku lub locka zmienia reguły J; wymaga przeglądu i commita człowieka.",
            "files": files,
        }
        with open(lock_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        print(f"zapisano {lock_path} ({len(files)} plików bramki)")
        return 0

    if not os.path.isfile(lock_path):
        print("TAMPERING_DETECTED: brak panel.lock", file=sys.stderr)
        return 1
    try:
        with open(lock_path, encoding="utf-8") as f:
            lock = json.load(f)
        errors = validate(base, lock)
    except Exception as e:
        print(f"TAMPERING_DETECTED: panel.lock nieparsowalny: {e}", file=sys.stderr)
        return 1
    if errors:
        print("TAMPERING_DETECTED: " + "; ".join(errors))
        return 1
    print(f"OK: {len(lock['files'])} plików zgodnych z panel.lock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
