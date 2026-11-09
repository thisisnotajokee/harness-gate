#!/usr/bin/env python3
"""Dziecko uruchamiane POD sandboxem Landlock. Bada, co wolno, i raportuje JSON-em.
Uruchamiane przez harness_eval.py --test-sandbox, nigdy bezpośrednio w produkcji.

POPRAWKA F2.3 — sonda zapisu nie może mylić „brak katalogu" z „wolno".
    Do panelu 2.0 sonda robiła `open(<home>/kb/.__sb_probe)` i oczekiwała `PermissionError`.
    W domu BEZ katalogu `kb/` (świeża instalacja u obcego) rozwiązywanie ścieżki kończy się
    `ENOENT` — ZANIM kernel dojdzie do kontroli uprawnień — więc sonda raportowała
    `FileNotFoundError`, a negatywna kontrola bramki ogłaszała `pass: False`. Zmierzone
    w świecie `w7_v2_selftest`: fałszywy alarm instrumentu, nie luka sandboxa.
    Teraz sonda najpierw próbuje UTWORZYĆ katalog (to też jest zapis w niedozwolonej ścieżce,
    więc Landlock odmawia), a `EACCES/EPERM/EROFS` — także przy tworzeniu — znaczy „blocked".
    Nieznany błąd nadal jest raportowany jako `TypeErrorName`, więc kontrola pozostaje fail-closed.
"""
import errno
import json
import os
import socket
import sys

HOME = os.path.expanduser("~")
DENIED = (errno.EACCES, errno.EPERM, errno.EROFS)


def write_probe(path):
    """`allowed` / `blocked` / nazwa nieoczekiwanego błędu — dla JEDNEJ ścieżki."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as e:
        return "blocked" if isinstance(e, PermissionError) or e.errno in DENIED else type(e).__name__
    try:
        with open(path, "w") as f:
            f.write("x")
    except OSError as e:
        return "blocked" if isinstance(e, PermissionError) or e.errno in DENIED else type(e).__name__
    try:
        os.unlink(path)
    except OSError:
        pass
    return "allowed"


def net_probe(port):
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", port))
        return "allowed"
    except PermissionError:
        return "blocked"
    except ConnectionRefusedError:
        return "passed_no_listener"
    except OSError as e:
        return "blocked" if isinstance(e, PermissionError) or e.errno in DENIED else type(e).__name__
    finally:
        s.close()


def main():
    scratch = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    # DOM MIERZONY bierzemy z argumentu (runner zna `HERMES_HOME`), a nie z `~/.hermes`:
    # przy niestandardowym domu sonda mierzyła CUDZY katalog, więc „dowód izolacji" dotyczył
    # ścieżki, w której rollout nigdy nie pracuje (naprawa 2026-11-09).
    home = (sys.argv[2] if len(sys.argv) > 2 else "") or os.environ.get("HERMES_HOME") \
        or os.path.join(HOME, ".hermes")
    probes = {
        "home":    os.path.join(home, ".__sb_probe"),
        "kb":      os.path.join(home, "kb", ".__sb_probe"),
        "panel":   os.path.join(home, "scripts", "harness_eval", ".__sb_probe"),
        "runs":    os.path.join(home, "eval", ".__sb_probe"),
        "scratch": os.path.join(scratch, "__sb_probe"),
    }
    results = {f"write_{label}": write_probe(path) for label, path in probes.items()}
    for port, label in ((22, "ssh"), (443, "https")):
        results[f"net_{label}"] = net_probe(port)
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
