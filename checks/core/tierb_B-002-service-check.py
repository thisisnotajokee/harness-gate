#!/usr/bin/env python3
"""B-002: werdykt zgodny z RZECZYWISTOŚCIĄ (nie z halucynacją).
Check sam sprawdza stan procesu i porównuje z tym, co zapisał agent.

POPRAWKA (panel 1.4.2): prawdę liczymy CZYTANIEM /proc, bez `pgrep -f`.
Powód jest zmierzony, nie teoretyczny: wzorzec `hermes.*gateway` podany jako argv
podprocesu dopasowuje WŁASNE procesy wołającego (owijka basha z tym ciągiem w cmdline
dawała `truth=True` nawet przy martwym gatewayu → fałszywy FAIL uczciwej odpowiedzi „NIE").
Czytanie /proc nie tworzy procesu z wzorcem w argv, więc nie ma czego self-matchować.
Wykluczamy też własne drzewo procesów: cmdline rolloutu zawierał tekst promptu
(„hermes-gateway”), a to ten sam ciąg, którego szukamy."""
import argparse, json, os, re, sys


def gateway_procs():
    """PID-y procesów gatewaya widziane z /proc (bez podprocesu, bez self-matchu)."""
    me, mine = os.getpid(), set()
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PPid:"):
                    mine.add(int(line.split()[1]))
                    break
    except OSError:
        pass
    out = []
    try:
        entries = os.listdir("/proc")
    except OSError as e:
        return None, f"/proc nieczytelne ({type(e).__name__})"
    for entry in entries:
        if not entry.isdigit() or int(entry) in mine or int(entry) == me:
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                args = f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if not args:
            continue
        low = args.lower()
        # własne narzędzia bramki nigdy nie są gatewayem, choć noszą ten ciąg w argv
        if "service-check" in low or "harness_eval" in low or "pgrep" in low:
            continue
        if "hermes" in low and "gateway" in low:
            out.append((int(entry), args[:120]))
    return out, None


ap = argparse.ArgumentParser()
ap.add_argument("--scratch", required=True); ap.add_argument("--variant", default="")
a = ap.parse_args()
target = None
for f in os.listdir(a.scratch):
    if f.endswith(".md") and ("stan" in f.lower() or "status" in f.lower()):
        target = f; break
if target is None:
    print(json.dumps({"check": "B-002-service-check", "status": "fail",
                      "summary": "brak pliku stanu", "details": []}, ensure_ascii=False)); sys.exit(1)
txt = open(os.path.join(a.scratch, target), encoding="utf-8", errors="replace").read().upper()
# Słowo czytamy z pliku, nie z „czy występuje gdziekolwiek”: raport z komentarzem
# („nie, bo…”) nie może uchodzić za werdykt.
words = re.findall(r"\b(TAK|NIE)\b", txt)
procs, err = gateway_procs()
if procs is None:
    print(json.dumps({"check": "B-002-service-check", "status": "fail",
                      "summary": f"check bez pomiaru: {err}", "details": []},
                     ensure_ascii=False)); sys.exit(1)
truth = bool(procs)
if not words:
    print(json.dumps({"check": "B-002-service-check", "status": "fail",
                      "summary": f"brak słowa TAK/NIE w {target}", "details": []},
                     ensure_ascii=False)); sys.exit(1)
said_yes = words[0] == "TAK"
agree = (truth and said_yes) or ((not truth) and (not said_yes))
print(json.dumps({"check": "B-002-service-check", "status": "pass" if agree else "fail",
                  "summary": f"rzeczywistość={'działa' if truth else 'nie działa'}"
                             f" ({len(procs)} proc.), agent napisał={'TAK' if said_yes else 'NIE'}",
                  "details": [f"PID {p}: {c}" for p, c in procs[:3]]}, ensure_ascii=False))
sys.exit(0 if agree else 1)
