#!/usr/bin/env python3
"""A-901: PRZYKŁADOWA kontrola paczki — wzorzec do skopiowania, nie kontrola produktu.

Co pokazuje (wszystko, czego potrzebuje nowy check):
  1. **Znajdowanie korzenia bramki** po sentinelu (`gate_config.py`), nie po liczbie poziomów —
     check paczki leży o poziom głębiej niż checki rdzenia.
  2. **Czytanie konfiguracji HOSTA** (`gate_config.for_checks()`) zamiast zaszytych ścieżek:
     katalog konwencji to `paths.kb` (u nas `kb`, u kogoś innego `notes`, `vault`…).
  3. **Trzy statusy zamiast dwóch**: `na` („nie dotyczy" z POWODEM, kod wyjścia 0) jest jawne —
     brak katalogu konwencji nie jest ani `fail` (nie ma czego łamać), ani cichym `skip`.
  4. **Kontrakt**: jedna linia JSON na stdout; kod wyjścia 1 wyłącznie przy `fail`.

Zadanie tego checka: konwencja hosta jest UŻYWANA i nie jest pusta. Pusty katalog to nie
„wszystko w porządku" — to brak materiału (ta sama klasa co „zero plików = pass" z audytu
falsyfikacyjnego Tier A, patrz `docs/PACKS.md`).
"""
import argparse
import json
import os
import sys

# --- 1. korzeń bramki po sentinelu -----------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
for _ in range(5):
    if os.path.isfile(os.path.join(_ROOT, "gate_config.py")):
        break
    _ROOT = os.path.dirname(_ROOT)
sys.path.insert(0, _ROOT)

CHECK_ID = "A-901-example-conventions"


def emit(status, summary, details=None):
    print(json.dumps({"check": CHECK_ID, "status": status, "summary": summary,
                      "details": details or []}, ensure_ascii=False))
    return 1 if status == "fail" else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=os.path.expanduser("~/.hermes"))
    ap.add_argument("--variant", default="")
    args = ap.parse_args()

    # --- 2. konwencja hosta z KONFIGURACJI, nie z kodu ---------------------------------------
    try:
        import gate_config
        cfg = gate_config.for_checks()
        if not cfg.get("ok"):
            # Fail-closed: nieczytelna konfiguracja to „nie wiem, co mierzyć", a nie „czysto".
            return emit("fail", f"reguł bramki nie da się odczytać: {cfg.get('error')}")
        # `layout` = WZGLĘDNA nazwa katalogu konwencji (`paths.kb`). NIE używaj tu
        # `cfg["paths"]` — to ścieżki ROZWIĄZANE WOBEC DOMU BRAMKI, a check ma mierzyć drzewo
        # WSKAZANE argumentem (`--home`/`--variant`). Inaczej u obcego mierzyłbyś swój dom.
        rel = (cfg.get("layout") or {}).get("kb") or "kb"
    except Exception as e:
        return emit("fail", f"nie mogę wczytać konfiguracji bramki: {type(e).__name__}: {e}")

    root = args.variant or args.home
    katalog = os.path.join(root, rel)

    # --- 3. `na` jest JAWNE i ma powód -------------------------------------------------------
    if not os.path.isdir(katalog):
        return emit("na", f"katalog konwencji '{rel}' nie istnieje — ta konwencja nie jest "
                          f"używana, więc nie ma czego mierzyć",
                    [f"szukano: {katalog}"])

    pliki = sorted(f for f in os.listdir(katalog)
                   if f.endswith(".md") and os.path.isfile(os.path.join(katalog, f)))
    if not pliki:
        return emit("fail", f"katalog '{rel}' istnieje, ale nie ma w nim ani jednego pliku .md "
                            f"— konwencja jest pusta, więc nie dowodzi niczego",
                    [f"katalog: {katalog}"])

    return emit("pass", f"konwencja '{rel}': {len(pliki)} plików .md",
                [f"katalog: {katalog}", f"przykłady: {', '.join(pliki[:3])}"])


if __name__ == "__main__":
    sys.exit(main())
