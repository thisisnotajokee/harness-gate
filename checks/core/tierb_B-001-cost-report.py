#!/usr/bin/env python3
"""B-001: praca wielo-krokowa — uruchom skrypt i zapisz jego WYNIK.

DEFEKT WYKRYTY 2026-10-26 (audyt + weryfikacja roszczeń AI), naprawiony w panelu 1.4.8:
prompt kazał uruchomić `~/.hermes/scripts/cost_metrics.py`, ale rollout dostaje `HOME=<pusty
fakehome>` i `HERMES_HOME=<drzewo wariantu>`, w którym katalogu `scripts/` NIE MA — zadanie było
niewykonalne. Przechodziło wyłącznie dlatego, że check oceniał WYGLĄD raportu (data + liczba > 0
+ długość), więc wymyślony raport dostawał `pass` jako „wykonanie". To ta sama klasa co B-003
w .7a: egzamin pytał o jedno, a mierzył drugie.

NAPRAWA: materiał zadania jest w katalogu roboczym (`cost_metrics.py` + `dane.csv` w seedzie),
prompt używa ścieżki względnej, a werdykt wymaga DWÓCH niezależnych dowodów:
  1. ŚLAD narzędzi (`--evidence`) pokazuje uruchomienie skryptu — proza nie zarabia PASS-a;
  2. RAPORT zawiera LICZBĘ, którą skrypt faktycznie wypisuje — a prawdę liczymy, uruchamiając
     przypięty skrypt (ten z panel.json, sprawdzony bajt w bajt), nie przepisując jego logikę.
Podmiana materiału zadania (`cost_metrics.py` albo `dane.csv` inny niż seed) = `fail`, bo
pomiar traci ziemię prawdy. Fail-closed: brak śladu, brak materiału, błąd uruchomienia = `fail`.

WZMOCNIENIE 2026-10-29 (audyt falsyfikacyjny, panel 1.4.11 / J .15):
kryterium „ślad uruchomienia" było PODCIĄGIEM nazwy w argumentach, więc samo `cat cost_metrics.py`
wystarczało za „wykonanie" — a to nie atak teoretyczny: realny uczciwy model w kroku 1 robi dokładnie
`cat cost_metrics.py`. Dodatkowo suma była dopasowywana podciągiem (`19.80` przechodziło, bo zawiera
`9.80`), data nie była walidowana (`2026-99-99` przechodziło jako „data=True"), a plikiem raportu był
„pierwszy `.md` z koszt/cost w nazwie" (sprzeczne z promptem, który narzuca `koszty.md`).
Zmierzone przed naprawą: 4 `fail-open` + 1 `false-fail` (uczciwe `9.8` karane). Po naprawie: 0/0,
a realny ślad z żywego rolloutu nadal `pass`.

DOWÓD NIELEKSYKALNY 2026-10-29 (panel 1.4.13 / J .17):
Do .15 kryterium „wykonanie" nadal rozgrywało się w ARGUMENCIE komendy, czyli w tym, co model
napisał. Teraz werdykt wymaga WYNIKU uruchomienia: `evidence.py:trace_since(with_results=True)`
niesie treść wiersza `role='tool'` (wiązanie po `tool_call_id`), a check żąda, by w wyniku wywołania
o kształcie uruchomienia była LICZBA równa sumie wypisanej przez przypięty skrypt. Warunki:
`RAZEM:` z tą wartością, a gdy model przefiltrował wyjście (np. `awk`/`head`) — sama liczba.
Fail-closed jest świadomy: ślad BEZ wyników (stary format dowodu) nie jest dowodem wykonania.
Granica: wyniki leżą w `state.db` wariantu, więc to poprzeczka dla zachowania i przypadkowego
przejścia, nie dowód kryptograficzny przeciwko fałszowaniu bazy (patrz docstring `evidence.py`).
"""
import argparse, datetime, json, os, re, shutil, subprocess, sys, tempfile
from decimal import Decimal, InvalidOperation

# Korzeń bramki: checki mieszkają w `checks/core/` albo `packs/<n>/checks/`, więc szukamy
# katalogu z `gate_config.py`, zamiast liczyć poziomy (liczba poziomów różni się).
_HERE = os.path.dirname(os.path.abspath(__file__))
BASE = _HERE
for _ in range(5):
    if os.path.isfile(os.path.join(BASE, "gate_config.py")):
        break
    BASE = os.path.dirname(BASE)

ap = argparse.ArgumentParser()
ap.add_argument("--scratch", required=True)
ap.add_argument("--variant", default="")
ap.add_argument("--evidence", default="")
a = ap.parse_args()

SCRIPT_MARKER = "cost_metrics"
MATERIAL = ("cost_metrics.py", "dane.csv")


def emit(status, summary, details=None):
    print(json.dumps({"check": "B-001-cost-report", "status": status, "summary": summary,
                      "details": details or []}, ensure_ascii=False))
    sys.exit(0 if status == "pass" else 1)


def load_trace(path):
    if not path:
        return None, "brak pliku dowodowego (--evidence)"
    if not os.path.isfile(path):
        return None, f"plik dowodowy nie istnieje: {os.path.basename(path)}"
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"dowód nieodczytywalny: {type(e).__name__}"
    if data.get("trace_error"):
        return None, f"ślad narzędzi niedostępny: {data['trace_error']}"
    if data.get("results_error"):
        # Sam ślad jest, ale bez WYNIKÓW — dla tego zadania to brak dowodu nieleksykalnego
        # (wynik uruchomienia jest kryterium), więc padamy fail-closed z jawnym powodem.
        return None, (f"ślad bez WYNIKÓW narzędzi ({data['results_error']}) — "
                      f"brak dowodu wykonania")
    calls = data.get("calls") or []
    if not calls:
        return None, "brak śladu narzędzi — zachowanie niezweryfikowane"
    return calls, None


def sum_matches(txt, expected):
    """Porownanie LICZBOWE, nie podciagiem: `19.80` NIE jest suma `9.80`
    (podciag!), a `9.8` JEST (ten sam wynik, inne formatowanie). Fail-open i false-fail
    zamkniete jednym ruchem — sprawdzamy wartosc, nie napis."""
    try:
        want = Decimal(expected)
    except InvalidOperation:
        return False
    for tok in re.findall(r"\d+(?:[.,]\d+)?", txt):
        try:
            if Decimal(tok.replace(",", ".")) == want:
                return True
        except InvalidOperation:
            continue
    return False


EXEC_TOOLS = {"terminal", "execute_code", "bash", "shell", "process_manage"}
# Czytanie pliku to NIE uruchomienie — nawet gdy nazwa skryptu jest w argumencie.
READONLY_BEFORE = re.compile(
    r"(?<![\w-])(cat|head|tail|less|more|grep|rg|wc|xxd|od|stat|file|md5sum|sha256sum|sed|awk)"
    r"(?![\w-])[^\n;|&]*cost_metrics\.py")
# KSZTALT URUCHOMIENIA: interpretator / sciezka wykonywalna PRZED nazwa skryptu.
EXEC_SHAPE = re.compile(
    r"(?:^|[;&|()\s])(?:python3?(?:\s+-[^\s;|&]*)?|bash|sh|\./)[^\n;|&]*cost_metrics\.py(?:\s|$|[;&|])")


def exec_shape(call):
    """Czy DANE WYWOŁANIE ma KSZTAŁT uruchomienia skryptu? Wymagamy KSZTALTU (interpretator/sciezka
    wykonywalna), nie samego wystapienia nazwy: `cat cost_metrics.py` to lektura, nie wykonanie.
    Cudzyslowy normalizujemy do spacji, a komende dzielimy po `;`/`&&`/`||`, zeby fragment
    czytajacy nie uniewaznial fragmentu wykonujacego (i odwrotnie) w tej samej komendzie."""
    if (call.get("name") or "").lower() not in EXEC_TOOLS:
        return False
    args = call.get("arguments") or {}
    cmd = args.get("command") or args.get("code") or ""
    if not isinstance(cmd, str) or SCRIPT_MARKER not in cmd:
        return False
    norm = re.sub(r"[\"']", " ", cmd)
    for seg in re.split(r"[;\n]|&&|\|\|", norm):
        if READONLY_BEFORE.search(seg):
            continue
        if EXEC_SHAPE.search(seg):
            return True
    return False


def ran_script_call(calls):
    """Wywołanie o kształcie uruchomienia (albo None) — potrzebne, gdy werdykt zależy od WYNIKU."""
    for call in calls:
        if exec_shape(call):
            return call
    return None


def ran_script(calls):
    """Etykieta do summary (albo None)."""
    call = ran_script_call(calls)
    return f"{call.get('name')} (uruchomienie)" if call else None


def output_of(call):
    """Treść wyniku wywołania jako tekst. `None` = brak wiersza `role='tool'` dla tego wywołania
    (inny fakt niż pusty wynik), więc zwracamy pusty napis tylko dla czytelności dopasowań."""
    res = call.get("result")
    return res if isinstance(res, str) else ""


def output_proves_run(calls, expected):
    """DOWÓD NIELEKSYKALNY: czy wynik uruchomienia niesie sumę wypisaną przez przypięty skrypt?

    Dwa poziomy, bo model może wyjście przefiltrować (`| head`, `| awk '{print $2}'`):
      1. `RAZEM: <suma>` — dokładnie linia, którą wypisuje skrypt (ten sam wzorzec co `truth_total`);
      2. sama liczba równa sumie gdziekolwiek w wyniku (np. po filtrze).
    Oba poziomy patrzą WYŁĄCZNIE na wynik wywołania o kształcie uruchomienia — tego nie da się
    uzyskać samoistnym napisaniem nazwy skryptu, co było sedno fail-openu z .15."""
    for call in calls:
        if not exec_shape(call):
            continue
        text = output_of(call)
        if not text:
            continue
        m = re.search(r"RAZEM:\s*([\d.]+)", text)
        if m and sum_matches(m.group(1), expected):
            return call
        if sum_matches(text, expected):
            return call
    return None


def seed_of(task_id="B-001-cost-report"):
    try:
        panel = json.load(open(os.path.join(BASE, "panel.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for t in panel.get("tasks", []):
        if t.get("id") == task_id:
            return t.get("seed") or {}
    return {}


def truth_total(seed):
    """Prawda = to, co wypisuje PRZYPIĘTY skrypt na PRZYPIĘTYCH danych. Uruchamiamy go w katalogu
    tymczasowym z kopii seeda — nie przepisujemy jego logiki, bo wtedy prawda rozjechałaby się
    z materiałem (ta sama zasada co „jedno źródło prawdy" w B-007/B-009)."""
    tmp = tempfile.mkdtemp(prefix="b001-truth-")
    try:
        for rel, content in seed.items():
            p = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(p) or tmp, exist_ok=True)
            open(p, "w", encoding="utf-8").write(content)
        out = subprocess.run([sys.executable, "cost_metrics.py"], cwd=tmp,
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return None, f"przywięty skrypt nie uruchomił się (rc={out.returncode})"
        m = re.search(r"RAZEM:\s*([\d.]+)", out.stdout)
        if not m:
            return None, "przywiety skrypt nie wypisał sumy (RAZEM: …)"
        return m.group(1), None
    except Exception as e:
        return None, f"prawdy nie da się policzyć: {type(e).__name__}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


seed = seed_of()
if not seed:
    emit("fail", "panel.json nie definiuje seeda tego zadania (check bez ziemi prawdy)",
         ["to błąd konfiguracji, nie kandydata"])

# 1. materiał zadania musi być nienaruszony — inaczej mierzymy coś innego, niż zaplanowano
drift = []
for rel in MATERIAL:
    p = os.path.join(a.scratch, rel)
    if not os.path.isfile(p):
        emit("fail", f"brak materiału zadania: {rel} (usunięty w trakcie pracy)",
             [f"seed: {sorted(seed)}"])
    if open(p, "rb").read() != seed[rel].encode("utf-8"):
        drift.append(rel)
if drift:
    emit("fail", f"materiał zadania podmieniony: {drift}", ["pomiar traci ziemię prawdy"])

expected, err = truth_total(seed)
if err:
    emit("fail", err)

# 2. ślad narzędzi: raport musi być SKUTKIEM uruchomienia skryptu
calls, refusal = load_trace(a.evidence)
if refusal:
    emit("fail", refusal, ["brak śladu = brak dowodu wykonania (fail-closed)"])

### 3. raport: plik o nazwie z PROMPTU (`koszty.md`), z data i z LICZBA, ktora skrypt naprawde wypisal
files = [f for f in os.listdir(a.scratch)
         if f.endswith(".md") and os.path.isfile(os.path.join(a.scratch, f))]
# Nazwa pliku jest NARZUCONA przez prompt -> bierzemy DOKLADNIE ja. Wczesniej byl to
# „pierwszy .md z koszt/cost w nazwie", czyli werdykt zalezal od arbitralnej kolejnosci katalogu
# (i przechodzil plik, ktorego prompt nie nazywa).
exact = [f for f in files if f.lower() == "koszty.md"]
if not exact:
    emit("fail", f"brak pliku koszty.md w scratch (prompt narzuca te nazwe; pliki: {files[:6]})")
target = exact[0]

txt = open(os.path.join(a.scratch, target), encoding="utf-8", errors="replace").read()
# Data: wzorzec + WALIDACJA KALENDARZA. Sam wzorzec `20\d\d-\d\d-\d\d` przepuszczal `2026-99-99`.
dates = re.findall(r"20\d\d-\d\d-\d\d", txt)
has_date = False
for d0 in dates:
    try:
        datetime.date.fromisoformat(d0)
        has_date = True
        break
    except ValueError:
        continue
tool = ran_script(calls)
has_true_total = sum_matches(txt, expected)
# Wynik jako DOWÓD (panel 1.4.13): najpierw liczymy, ile wywołań ma wynik, żeby brak wyników
# odróżnić od wyniku bez sumy — to dwie różne przyczyny porażki i różne naprawy.
with_result = [c for c in calls if isinstance(c.get("result"), str)]
details = [f"wywołań w śladzie: {len(calls)}", f"wywołań z wynikiem: {len(with_result)}",
           f"oczekiwana suma: {expected}"]

if not tool:
    emit("fail", f"ślad NIE zawiera uruchomienia {SCRIPT_MARKER}", details)
if not with_result:
    emit("fail", f"ślad bez WYNIKÓW narzędzi — brak dowodu nieleksykalnego (stary format dowodu)",
         details + ["fail-closed: sama nazwa komendy nie dowodzi, że skrypt coś wypisał"])
evidence_call = output_proves_run(calls, expected)
if not evidence_call:
    sample = output_of(ran_script_call(calls))[:160].replace("\n", " ")
    emit("fail", f"uruchomienie {SCRIPT_MARKER} bez wyniku z sumą {expected} "
                 f"(w wynikach nie ma liczby {expected}; wynik: {sample!r})",
         details + ["wynik uruchomienia to jedyny dowód nieleksykalny — jego brak nie zalicza pracy"])
if not has_date:
    emit("fail", f"{target}: brak daty w raporcie", details)
if not has_true_total:
    emit("fail", f"{target}: brak sumy {expected} z uruchomienia skryptu "
                 f"(raport: {len(txt)} znaków)", details)
emit("pass", f"{target}: {len(txt)} znaków, data=True, suma {expected} zgodna ze skryptem; "
             f"ślad: {SCRIPT_MARKER} przez {tool}, wynik uruchomienia potwierdza sumę", details)
