#!/usr/bin/env python3
"""A-004: frontmatter SKILL.md, nazwa == katalog, trigger w description, sygnał o provenance.
Brak provenance to OSTRZEŻENIE (standard wprowadzany od dziś), nie błąd.

PRÓG OSTRZEŻEŃ (od .9): liczba ostrzeżeń ma limit. Powód jest architektoniczny, nie estetyczny —
check, który przy dowolnej liczbie ostrzeżeń mówi „pass", jest dla bramki NIEWIDOCZNY przy
poprawkach: 178 → 177 daje remis, więc realne ulepszenie nie może dać wygranej i zostaje
odrzucone jako „brak dowodu poprawy". Limit zamienia dług w sygnał: przekroczenie = fail,
zejście poniżej = pass → redukcja ostrzeżeń staje się WYGRANĄ kandydata.
Domyślnie 150 (`.21`), nadpisywalne pokrętłem `A004_MAX_WARNINGS`.

ŹRÓDŁO PROGU (od .21): `gate.config.json → limits.a004_max_warnings` — plik KONFIGURACJI BRAMKI,
objęty manifestem, więc zmiana progu bez przezbrojenia locka to `fail` A-005. Wcześniej próg
siedział w `GATE_PARAMS`; trzymanie go w DWÓCH miejscach tworzyłoby drugą prawdę o tym samym
progu, więc klucz zniknął z `GATE_PARAMS` (hash progów sygnalizuje to jako `J_MUTATION`).
Nieczytelna konfiguracja bramki = fail („nie wiem, jakie są reguły" nie znaczy „czysto")."""
import argparse, os, re, sys
from typing import NoReturn

# Korzeń bramki: checki mieszkają w `checks/core/` albo `packs/<n>/checks/`, więc szukamy
# katalogu z `gate_config.py`, zamiast liczyć poziomy (liczba poziomów różni się).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
for _ in range(5):
    if os.path.isfile(os.path.join(_ROOT, "gate_config.py")):
        break
    _ROOT = os.path.dirname(_ROOT)
sys.path.insert(0, _ROOT)


def emit(name, status, summary, details=None) -> NoReturn:
    """Kontrakt checka: jedna linia JSON na stdout, exit 1 gdy fail."""
    import json as _j
    print(_j.dumps({"check": name, "status": status, "summary": summary,
                    "details": details or []}, ensure_ascii=False))
    sys.exit(1 if status == "fail" else 0)


def gate_max_warnings():
    """(próg, błąd) — próg czytany z konfiguracji BRAMKI, nigdy z drzewa mierzonego."""
    try:
        import gate_config
        cfg = gate_config.for_checks()
    except Exception as e:                    # brak modułu reguł = nie znam poprzeczki
        return None, f"nie mogę wczytać konfiguracji bramki: {type(e).__name__}: {e}"
    if not cfg["ok"]:
        return None, cfg["error"]
    return int(cfg["limits"]["a004_max_warnings"]), ""


_MAX_WARNINGS, _CFG_ERR = gate_max_warnings()
if _MAX_WARNINGS is None:
    emit("A-004-skill-frontmatter", "fail", f"reguł bramki nie da się odczytać: {_CFG_ERR}")
# Zmienna środowiskowa zostaje jako jawne pokrętło do eksperymentów (np. pomiar mechaniki
# wygranej przy innym progu); domyślnie rządzi wartość z konfiguracji bramki.
MAX_WARNINGS = int(os.environ.get("A004_MAX_WARNINGS", _MAX_WARNINGS))

SKIP_DIRS = {"__pycache__", ".git", ".archive"}


def parse_frontmatter(text):
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    block = text[3:end]
    name = re.search(r"^name:\s*(.+)$", block, re.M)
    desc = re.search(r"^description:\s*(.+)$", block, re.M)
    return {"name": (name.group(1).strip().strip("\"'") if name else None),
            "description": (desc.group(1).strip().strip("\"'") if desc else None),
            "provenance": bool(re.search(r"^provenance:", block, re.M))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=os.path.expanduser("~/.hermes"))
    ap.add_argument("--variant", default="")
    a = ap.parse_args()
    root = a.variant or a.home
    skills = os.path.join(root, "skills")
    if not os.path.isdir(skills):
        emit("A-004-skill-frontmatter", "skip", f"brak {skills}")
    bad, warn, total = [], [], 0
    unreadable = []
    seen_names = {}

    def _on_walk_error(err):
        """`os.walk` MILCZY o katalogu, którego nie może odczytać — skill w nim jest wtedy
        niemierzony, a panel pokazuje „sprawdzono N” jak przy pełnym przejściu. Audyt
        falsyfikacyjny Tier A (29.10.2026): katalog `chmod 000` przechodził jako czysty."""
        rel = os.path.relpath(err.filename, root) if err.filename else "?"
        unreadable.append(f"{rel}: katalog nieczytelny ({err.strerror}) — skill w nim niemierzony")

    for dirpath, dirnames, filenames in os.walk(skills, onerror=_on_walk_error):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if "SKILL.md" not in filenames:
            continue
        total += 1
        p = os.path.join(dirpath, "SKILL.md")
        rel = os.path.relpath(p, root)
        try:
            text = open(p, "r", encoding="utf-8", errors="replace").read()
        except OSError as e:
            bad.append(f"{rel}: nieodczytywalny ({e})")
            continue
        fm = parse_frontmatter(text)
        if fm is None:
            bad.append(f"{rel}: brak lub uszkodzony frontmatter")
            continue
        if not fm["name"]:
            bad.append(f"{rel}: brak pola name")
        # Porównujemy wyłącznie z WŁASNYM katalogem. Wyjątek „albo z katalogiem nadrzędnym"
        # pozwalał nazwać skill `foo` w katalogu `skills/foo/bar/`, jeśli `foo` było katalogiem
        # wyżej — czyli dokładnie ten rozjazd, który check ma wykrywać, przechodził jako poprawny.
        # Weryfikacja w produkcji: zero SKILL.md korzystało z tego wyjątku, więc jego usunięcie
        # nie tworzy fałszywych FAIL-i.
        elif fm["name"] != os.path.basename(dirpath):
            bad.append(f"{rel}: name='{fm['name']}' nie zgadza się z katalogiem '{os.path.basename(dirpath)}'")
        else:
            # Nazwy tylko z PAR zgodnych z katalogiem: rozjazd nazwa↔katalog raportujemy wyżej
            # osobno, więc dokładanie mu komunikatu o kolizji byłoby szumem.
            seen_names.setdefault(fm["name"], []).append(rel)
        if not fm["description"]:
            bad.append(f"{rel}: brak description")
        elif not fm["description"].lower().startswith("use when"):
            warn.append(f"{rel}: description bez triggera 'Use when ...'")
        if not fm["provenance"]:
            warn.append(f"{rel}: brak provenance")
    # Kolizja nazw (panel 1.4.17): aktywacja idzie po NAZWIE, więc dwie kategorie z tym samym
    # `name` czynią ją niejednoznaczną — `skill_view(name='foo')` nie mówi, którego skilla dotyczy.
    # Zmierzone PRZED zaostrzeniem: produkcja 98 SKILL.md / 0 kolizji (i fikstura bez `skills/`),
    # więc reguła nie tworzy fałszywego FAIL-a na zdrowym drzewie; wpis `J_MUTATION` z 08.11.
    for _name, _paths in sorted(seen_names.items()):
        if len(_paths) > 1:
            bad.append(f"kolizja nazwy '{_name}' w {len(_paths)} miejscach: {', '.join(sorted(_paths))} "
                       f"— aktywacja po nazwie jest niejednoznaczna")
    details = [f"sprawdzono {total} SKILL.md"] + ([f"ostrzeżenia ({len(warn)}): " + "; ".join(warn[:12])] if warn else [])
    # FAIL-CLOSED (panel 1.4.16): nieczytelny katalog to „nie wiem”, a nie „czysto”. Kolejność PRZED
    # kontrolą pustego zbioru, bo to ona tłumaczy zaniżone `total` (inaczej przy jednym ukrytym
    # skillu check raportuje „sprawdzono 1” i nie mówi, że drugiego nie widział).
    if unreadable:
        emit("A-004-skill-frontmatter", "fail",
             f"{len(unreadable)} katalogów nieczytelnych — pomiar nieobjęty", details + unreadable[:20])
    # Pusty zbiór to brak egzaminu, nie wynik egzaminu: „0 SKILL.md poprawnych" przechodziło
    # jako pass, więc kandydat mógł skasować katalogi skilli i wyglądać na czystego.
    if total == 0:
        emit("A-004-skill-frontmatter", "fail", "nie znaleziono żadnego SKILL.md — nic nie zweryfikowano")
    if bad:
        emit("A-004-skill-frontmatter", "fail", f"{len(bad)} błędów frontmattera", details + bad[:20])
    # Dług staje się sygnałem, nie tłem: próg jest twardy, bo inaczej bramka nie widzi poprawy.
    if len(warn) > MAX_WARNINGS:
        emit("A-004-skill-frontmatter", "fail",
             f"próg ostrzeżeń przekroczony: {len(warn)} > {MAX_WARNINGS} (dług +{len(warn) - MAX_WARNINGS})",
             details)
    emit("A-004-skill-frontmatter", "pass",
         f"{total} SKILL.md poprawnych ({len(warn)} ostrzeżeń, próg {MAX_WARNINGS})", details)


if __name__ == "__main__":
    main()
