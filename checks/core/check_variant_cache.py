#!/usr/bin/env python3
"""A-006: warianty nie duplikują cache/installs.

Powód istnienia: rollout Tier B napompował cache/ (711 MB) i installs/ (159 MB) WEWNĄTRZ
wariantu, przez co każdy wariant kosztował ~1 GB. Złapane przypadkiem po fakcie — ten check
ma to złapać od razu. Warianty mają cache/ i installs/ jako SYMLINKI do .shared.

Check dotyczy infrastruktury ewaluacji, nie treści harnessu, więc zawsze patrzy na katalog
wariantów WYNIKAJĄCY Z KONFIGURACJI BRAMKI (`eval_dir/variants`, od .21) — nie na `--variant`.
Wcześniej była tu zaszyta ścieżka `~/.hermes/eval/variants`, więc poza naszym domem check
mierzył cudzy katalog (albo nieistniejący). Konfiguracja nieczytelna = fail: „nie wiem, gdzie
są warianty" nie jest „warianty są czyste".
"""
import argparse, json, os, sys


# Korzeń bramki: checki mieszkają w `checks/core/` albo `packs/<n>/checks/`, więc szukamy
# katalogu z `gate_config.py`, zamiast liczyć poziomy (liczba poziomów różni się).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
for _ in range(5):
    if os.path.isfile(os.path.join(_ROOT, "gate_config.py")):
        break
    _ROOT = os.path.dirname(_ROOT)
sys.path.insert(0, _ROOT)

PROG_MB = 50
SKIP = {".shared"}


def emit(status, summary, details=None):
    print(json.dumps({"check": "A-006-variant-cache-shared", "status": status,
                      "summary": summary, "details": details or []}, ensure_ascii=False))
    return 1 if status == "fail" else 0


def gate_variants_dir():
    """(katalog, błąd) — katalog wariantów z konfiguracji BRAMKI. Fail-closed."""
    try:
        import gate_config
        cfg = gate_config.for_checks()
    except Exception as e:
        return None, f"nie mogę wczytać konfiguracji bramki: {type(e).__name__}: {e}"
    if not cfg["ok"]:
        return None, cfg["error"]
    return cfg["paths"]["variants_dir"], ""


def dir_size_mb(path):
    total = 0
    for dp, _, fs in os.walk(path):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total / 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=os.path.expanduser("~/.hermes"))
    ap.add_argument("--variant", default="")
    # Jawny `--variants` zostaje dla testów (izolowany katalog), ale domyślną ścieżkę wyznacza
    # konfiguracja bramki — inaczej check był NIE-TESTOWALNY na fiksturze i nieprzenośny.
    ap.add_argument("--variants", default="",
                    help="katalog wariantów (domyślnie z gate.config.json: eval_dir/variants)")
    args = ap.parse_args()
    cfg_dir, cfg_err = gate_variants_dir()
    if cfg_dir is None and not args.variants:
        return emit("fail", f"reguł bramki nie da się odczytać: {cfg_err}")
    variants = args.variants or cfg_dir
    if not variants:
        return emit("fail", f"nie znam katalogu wariantów: {cfg_err or 'brak w konfiguracji'}")
    shared = os.path.join(variants, ".shared")
    shared_real = os.path.realpath(shared)

    # FAIL-CLOSED (panel 1.4.16): brak katalogu wariantów to brak egzaminu Tier B, a nie „nie ma
    # czego pilnować”. Do .15 był to `skip`, więc skasowanie (albo wskazanie nieistniejącej ścieżki)
    # dawało wiersz bez werdyktu — audyt falsyfikacyjny Tier A 29.10.2026, druga dziura tego checka.
    if not os.path.isdir(variants):
        print(json.dumps({"check": "A-006-variant-cache-shared", "status": "fail",
                          "summary": f"brak katalogu {variants} — brak egzaminu nie jest stanem czystym",
                          "details": []}, ensure_ascii=False))
        return 1

    zle, warianty = [], []
    for name in sorted(os.listdir(variants)):
        if name in SKIP:
            continue
        vroot = os.path.join(variants, name)
        if not os.path.isdir(vroot):
            continue
        warianty.append(name)
        for sub in ("cache", "installs"):
            p = os.path.join(vroot, sub)
            if os.path.islink(p):
                # Symlink to jeszcze nie dowód WSPÓŁDZIELENIA: cel musi leżeć w `.shared`. Do .15
                # kod sprawdzał tylko `islink`, więc link do prywatnego katalogu (np. 60 MB cache
                # poza `.shared`) raportował „poprawnie: współdzielone” — obietnica z docstringa
                # bez egzekucji (audyt falsyfikacyjny Tier A, 29.10.2026).
                target = os.path.realpath(p)
                if not (target == shared_real or target.startswith(shared_real + os.sep)):
                    zle.append(f"{name}/{sub}: symlink poza .shared → {target}")
                continue
            if os.path.isdir(p):
                mb = dir_size_mb(p)
                if mb > PROG_MB:
                    zle.append(f"{name}/{sub}: własna kopia {mb:.0f} MB (> {PROG_MB} MB) "
                               f"— powinien być symlink do .shared")
    shared = os.path.join(variants, ".shared")
    details = [f"wariantów: {len(warianty)} ({', '.join(warianty[:6])})",
               f".shared: {'jest' if os.path.isdir(shared) else 'BRAK'}"]
    # „warianty nie duplikują cache" przy ZERZE wariantów to werdykt o niczym. Katalog istnieje
    # (świeża instalacja go zakłada), ale nie ma czego mierzyć — to jawne „nie dotyczy" z powodem,
    # nie `skip` (który blokuje panel) ani fałszywe `pass` o pustym zbiorze.
    if not warianty:
        print(json.dumps({"check": "A-006-variant-cache-shared", "status": "na",
                          "summary": "katalog wariantów istnieje, ale jest pusty — żaden egzamin "
                                     "Tier B jeszcze nie działał, więc nie ma czego pilnować",
                          "details": details}, ensure_ascii=False))
        return 0
    if zle:
        print(json.dumps({"check": "A-006-variant-cache-shared", "status": "fail",
                          "summary": f"{len(zle)} wariantów duplikuje cache", "details": details + zle},
                         ensure_ascii=False))
        return 1
    print(json.dumps({"check": "A-006-variant-cache-shared", "status": "pass",
                      "summary": "warianty nie duplikują cache", "details": details},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
