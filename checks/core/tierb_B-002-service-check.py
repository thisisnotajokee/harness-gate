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


def _wlasny_proces(entry, variant):
    """Czy proces należy do DOMU WARIANTU (czyli do naszego rolloutu), a nie do hosta.

    Po co: rollout sam dźwiga bramkę (`hermes -z` → `gateway run --idle-exit`), a jej argv zawiera
    i „hermes", i „gateway". Bez tego filtra zadanie mierzyło NASZĄ infrastrukturę i „prawdą"
    było zawsze `True` — stąd m.in. chwiejność B-002 między powtórzeniami (recenzja 2026-11-09 §2.4).
    """
    if not variant:
        return False
    cel = os.path.realpath(variant)
    for plik in ("environ", "cwd"):
        try:
            if plik == "cwd":
                sciezka = os.path.realpath(os.readlink(f"/proc/{entry}/cwd"))
                if sciezka == cel or sciezka.startswith(cel + os.sep):
                    return True
                continue
            dane = open(f"/proc/{entry}/environ", "rb").read().decode("utf-8", "replace")
        except OSError:
            continue
        for pole in dane.split("\0"):
            if pole.startswith("HERMES_HOME=") and os.path.realpath(pole.split("=", 1)[1]) == cel:
                return True
    return False


def gateway_procs(variant=""):
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
        if _wlasny_proces(entry, variant):
            continue      # bramka NASZEGO wariantu to nie „proces hosta" — patrz _wlasny_proces
        if "hermes" in low and "gateway" in low:
            out.append((int(entry), args[:120]))
    return out, None


def main():
    # Wszystko wykonywalne w `main()` + guard: dzięki temu check da się ZAIMPORTOWAĆ
    # (testy pinują `_wlasny_proces` bez udawania procesu na hoście).
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
    procs, err = gateway_procs(a.variant)
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


if __name__ == "__main__":
    main()
