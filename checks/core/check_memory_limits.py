#!/usr/bin/env python3
"""A-002: budżet pamięci — czy pliki pamięci mieszczą się w budżecie BRAMKI.

Zmiana modelu prawdy (.11) i parametryzacja (.21) — obie J_MUTATION. Limit NIE pochodzi
z `config.yaml` testowanego drzewa: strażnik, który czyta własną poprzeczkę od kandydata,
obchodzi się bez oszustwa — wystarczyło wpisać `memory_char_limit: 99999` w swoim configu
i przejść jako czysty. Od .21 limit mieszka w `gate.config.json` (`limits.memory_char`,
`limits.user_char`) — czyli w pliku KONFIGURACJI BRAMKI objętym manifestem, więc zmiana budżetu
bez przezbrojenia locka jest wykrywana przez A-005, a nie przemilczana. `config.yaml` drzewa
mierzonego służy WYŁĄCZNIE do wykrycia dryfu: deklaracja WYŻSZA niż przypięta = fail
(poprzeczka podniesiona poza bramką), niższa = nota (wolno zaostrzać).
Nieczytelna konfiguracja bramki = fail: „nie wiem, jakie są reguły" nie znaczy „czysto".
"""
import argparse, json, os, re, sys
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
    print(json.dumps({"check": name, "status": status, "summary": summary,
                      "details": details or []}, ensure_ascii=False))
    sys.exit(1 if status == "fail" else 0)


def gate_limits():
    """(limits, error) — budżet z konfiguracji BRAMKI, nigdy z drzewa mierzonego. Fail-closed."""
    try:
        import gate_config
        cfg = gate_config.for_checks()
    except Exception as e:                    # brak modułu reguł = nie znam poprzeczki
        return None, f"nie mogę wczytać konfiguracji bramki: {type(e).__name__}: {e}"
    if not cfg["ok"]:
        return None, cfg["error"]
    lim = cfg["limits"]
    # Zmienne środowiskowe zostają jako jawna furtka do eksperymentów (nie do produkcji).
    return {"memory": int(os.environ.get("A002_MEMORY_CHAR_LIMIT", lim["memory_char"])),
            "user": int(os.environ.get("A002_USER_CHAR_LIMIT", lim["user_char"]))}, ""


_LIMITS, _CFG_ERR = gate_limits()
if _LIMITS is None:
    emit("A-002-memory-limits", "fail", f"reguł bramki nie da się odczytać: {_CFG_ERR}")

PINNED = _LIMITS
DEFAULTS = dict(PINNED)   # brak deklaracji w config.yaml = brak dryfu (porównujemy z przypiętym)


def strip_block_scalars(txt):
    """Usuwa treść bloków skalarnych YAML (`klucz: |` albo `klucz: >`).

    Bez tego zagnieżdżony tekst udawał klucz: `description: |` z linią
    `  memory_char_limit: 999999` w środku wygrywał z prawdziwym wpisem i unieważniał budżet.
    Wcięcia w prawdziwym config.yaml są legalne (klucze siedzą pod sekcją na 2 spacjach),
    więc odrzucenie „wszystkiego z wcięciem" nie wchodzi w grę — trzeba rozumieć blok.
    """
    out, block_indent = [], None
    for line in txt.splitlines():
        if block_indent is not None:
            if line.strip() and (len(line) - len(line.lstrip(" "))) > block_indent:
                continue                      # wciąż treść bloku — nie jest kluczem
            block_indent = None
        m = re.match(r"^(\s*)[A-Za-z_][\w\-]*:\s*[|>][-+]?\s*(#.*)?$", line)
        if m:
            block_indent = len(m.group(1))    # od tego wcięcia zaczyna się treść bloku
        out.append(line)
    return "\n".join(out)


def parse_limit(raw):
    """YAML dopuszcza separatory podkreślenia w liczbach: `1_000` to tysiąc.

    Wcześniejszy regex `(\\d+)` zatrzymywał się na `_` i czytał `1` jako limit — każdy plik
    wyglądał wtedy na przekraczający budżet (fałszywe FAIL-e).
    """
    return int(raw.replace("_", ""))


def limits(config_path):
    """Zwraca (limity, czy_plik_odczytany, wartości_nieparsowalne). Ostatni wpis wygrywa —
    tak samo jak w parserze YAML. Dwa różne wpisy tego samego klucza są raportowane."""
    lim = dict(DEFAULTS)
    try:
        txt = open(config_path, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        return lim, False, []
    # `seen` = klucze już widziane. Do 2026-11-09 warunek „duplikatu" był sprzężony z wartością
    # (łapał tylko powtórzenie z nie-domyślną wartością), a lista `duplicates` **nie była nigdzie
    # zwracana** — czyli obietnica z docstringa nie miała egzekucji (recenzja zewnętrzna §5.2/§7.5).
    bad, duplicates, seen = [], [], set()
    for line in strip_block_scalars(txt).splitlines():
        m = re.match(r"^\s*(memory_char_limit|user_char_limit):\s*([^#\s]+)", line)
        if not m:
            continue
        key = "memory" if m.group(1) == "memory_char_limit" else "user"
        raw = m.group(2).strip()
        try:
            value = parse_limit(raw)
        except ValueError:
            bad.append(f"{m.group(1)}: {raw!r}")
            continue
        if key in seen:
            duplicates.append(key)
        seen.add(key)
        lim[key] = value
    # Duplikat klucza to konfiguracja NIEJEDNOZNACZNA („ostatni wygrywa" to nie reguła, to skutek)
    # — idzie tym samym kanałem co wartości nieparsowalne, więc kończy się `fail`.
    bad = bad + [f"{k}: klucz zduplikowany w config.yaml (niejednoznaczny limit)" for k in duplicates]
    return lim, True, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=os.path.expanduser("~/.hermes"))
    ap.add_argument("--variant", default="")
    a = ap.parse_args()
    root = a.variant or a.home
    declared, parsed, bad = limits(os.path.join(root, "config.yaml"))
    mem_dir = os.path.join(root, "memories")
    over, details, seen, unreadable = [], [], [], []
    # Dryf: kandydat deklaruje budżet INNY niż przypięty w bramce. Podniesienie = próba zmiany
    # poprzeczki poza bramką (fail). Obniżenie jest dozwolone — to zaostrzenie, nie luz.
    widened = [f"{k}: zadeklarowane {declared[k]} > przypięte {PINNED[k]}"
               for k in PINNED if declared[k] > PINNED[k]]
    narrowed = [f"{k}: zadeklarowane {declared[k]} < przypięte {PINNED[k]} (zaostrzenie — OK)"
                for k in PINNED if declared[k] < PINNED[k]]
    if narrowed:
        details += narrowed
    if not parsed:
        details.append("config.yaml nieodczytany — budżet bramki i tak obowiązuje, "
                       "ale dryfu nie da się sprawdzić")
    if os.path.isdir(mem_dir):
        for fn in sorted(os.listdir(mem_dir)):
            if not fn.endswith(".md"):
                continue
            p = os.path.join(mem_dir, fn)
            try:
                n = len(open(p, "r", encoding="utf-8", errors="replace").read())
            except OSError as e:
                # Budżetu pliku, którego nie da się odczytać, NIE znamy. Milczące pominięcie
                # zamieniało to na „mieści się w limicie" — czyli werdykt bez podstawy.
                unreadable.append(f"{fn}: nieodczytywalny ({e.__class__.__name__})")
                continue
            key = "user" if fn.upper().startswith("USER") else "memory" if "MEMORY" in fn.upper() else None
            if key is None:
                continue
            limit = PINNED[key]                       # PRAWDA: budżet bramki, nie deklaracja
            pct = 100 * n / limit if limit else 0
            seen.append(f"{fn}: {n}/{limit} znaków ({pct:.0f}%)")
            if n > limit:
                over.append(f"{fn}: {n} > {limit} (przekroczenie o {n - limit})")
            elif pct >= 90:
                details.append(f"{fn}: {pct:.0f}% budżetu — blisko limitu")
    if not seen and not unreadable:
        emit("A-002-memory-limits", "skip", "brak plików pamięci do sprawdzenia — mierzę pliki wg konwencji MEMORY/*MEMORY* oraz USER* w memories/; pliki o innych nazwach NIE są objęte tymi limitami (zmień nazwę albo rozszerz check)")
    if bad:
        emit("A-002-memory-limits", "fail",
             "limit nieparsowalny: " + ", ".join(bad), details + seen)
    if widened:
        emit("A-002-memory-limits", "fail",
             "kandydat podniósł własny budżet pamięci poza bramką: " + "; ".join(widened),
             details + seen)
    if unreadable:
        emit("A-002-memory-limits", "fail",
             "; ".join(unreadable), details + seen)
    if over:
        emit("A-002-memory-limits", "fail", "; ".join(over), details + seen)
    emit("A-002-memory-limits", "pass", "; ".join(seen), details)


if __name__ == "__main__":
    main()
