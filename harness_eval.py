#!/usr/bin/env python3
"""harness_eval.py — bramka przyjęcia dla zmian w harnessie Hermesa.

Rola: uruchomić panel regresji na wariancie i na incumbencie, porównać PAROWANE
wyniki i wydać werdykt. Werdykt zapisuje PROCES NADRZĘDNY (nigdy sandbox) —
restrykcja Landlock jest nieodwracalna i dziedziczona, więc runner nie nakłada
jej na siebie, tylko na potomka (sandbox_landlock.py + execve).

Przykłady:
  harness_eval.py --panel                      # Tier A na produkcji (inwentarz)
  harness_eval.py --compare --variant cand --klasa 1B --artifact moj-skill
  harness_eval.py --selftest-mutacja            # test 1: negatywna kontrola bramki
  harness_eval.py --test-sandbox                # test 5: izolacja Tier B
  harness_eval.py --certificate --m 12 --delta 0.2

Kody wyjścia: 0 = OK / ACCEPT, 1 = REJECT lub test nieprzeszedł, 2 = błąd użycia.
"""
import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
try:
    from eval_version import EVALUATOR_VERSION, GATE_PARAMS, digest, thresholds
except Exception:  # pragma: no cover
    EVALUATOR_VERSION, digest, thresholds = "unknown", (lambda _t: "unknown"), (lambda: {})
    # Fallback, gdy nie da się wczytać reguł: celowo OSTRZEJSZY niż próg normalny
    # (12 scenariuszy zamiast 6) — fail-closed znaczy „w razie wątpliwości wyżej".
    GATE_PARAMS = {"tier_b_m_min": 12, "tier_b_delta": 0.2}

# --- Ustawienia pochodzą z `gate.config.json` (F2). Brak pliku = wartości domyślne
# (pierwsze uruchomienie zielone bez konfiguracji), plik ZEPSUTY = REJECT — literówka
# w konfiguracji nie może wyglądać jak brak konfiguracji.
import gate_config
import messages              # warstwa komunikatów (pl/en) — R-UX4/R-UX5/R-UX7

CFG = gate_config.load(BASE)
PATHS = gate_config.resolve(CFG, BASE) if CFG["ok"] else gate_config.resolve({}, BASE)

HERMES_HOME = PATHS["hermes_home"]
GIT_REPO = PATHS["anchor_repo"]
ANCHOR_MODE = PATHS["anchor_mode"]
ANCHOR_REPO_PATH = PATHS["anchor_repo_path"]
EVAL_DIR = PATHS["eval_dir"]
LEDGER = os.path.join(EVAL_DIR, "ledger.jsonl")
RUNS = os.path.join(EVAL_DIR, "runs")
VARIANTS = os.path.join(EVAL_DIR, "variants")

# --- Kotwica drzewa: bramka dotyczy TEGO drzewa, w którym leży jej kod (F2 — po
# sparametryzowaniu sprawdzamy SPÓJNOŚĆ, nie konkretną ścieżkę instalacji).
# PROD_HOME wynika z położenia pliku (nie z $HOME), ENV_HERMES_HOME z $HOME.
PROD_HOME = os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                          os.pardir, os.pardir))
ENV_HERMES_HOME = os.path.realpath(os.path.expanduser("~/.hermes"))


def gate_dir(home=None):
    return os.path.join(home or HERMES_HOME, "scripts", "harness_eval")


def tree_guard(home=None, env_home=None, base=None):
    """Fail-closed: bramka sprawdza wyłącznie drzewo, z którego sama pochodzi (F2).

    Niezmiennik: `base` (katalog kodu bramki) == `$HERMES_HOME/scripts/harness_eval` drzewa,
    które bramka mierzy. Sprawdzamy SPÓJNOŚĆ tych dwóch zamiast konkretnej ścieżki instalacji —
    dzięki temu bramka działa w dowolnym miejscu (u obcego: `~/.hermes/scripts/harness_eval`,
    w naszym drzewie cienia: `~/projects/…/v2`), ale nadal nie da się nią mierzyć jednego
    drzewa, wykonując checki z drugiego.

    Różnica drzewa mierzonego od `$HOME/.hermes` nie jest błędem (konfiguracja może wskazywać
    inne drzewo, np. w światach testowych) — jest NOTĄ w werdykcie, bo werdykt musi mówić,
    czego dotyczy.
    """
    home = os.path.realpath(home or HERMES_HOME)
    env_home = os.path.realpath(env_home or ENV_HERMES_HOME)
    base = os.path.realpath(base or BASE)
    real_gate = os.path.realpath(gate_dir(home))
    if base != real_gate:
        return {"status": "fail", "code": "FOREIGN_BASE",
                "summary": f"runner uruchomiony z {base}, a drzewem bramki jest {real_gate} "
                           f"— uruchom bramkę z drzewa, które mierzy (docs/CONFIG.md)"}
    note = ""
    if home != env_home:
        note = (f"mierzone drzewo {home} różni się od $HOME/.hermes ({env_home}) "
                f"— zgodnie z konfiguracją")
    return {"status": "pass", "code": "OK", "note": note,
            "summary": f"drzewo bramki zgodne: {home}" + (f" · {note}" if note else "")}


def paths_line():
    """Każdy werdykt niesie ścieżki ORAZ źródło konfiguracji, na których powstał — inaczej nie
    wiadomo nie tylko czego dotyczy, ale też według jakich reguł (F2: konfig w manifeście)."""
    return {"paths": {"base": BASE, "hermes_home": HERMES_HOME, "git_repo": GIT_REPO,
                      "anchor_mode": ANCHOR_MODE, "anchor_repo_path": ANCHOR_REPO_PATH,
                      "config_source": CFG["source"], "config_hash": CFG["hash"]}}


def resolved_check(rel, home=None):
    """Check musi leżeć w katalogu bramki — panel.json nie może wskazać kodu poza nim."""
    root = os.path.realpath(gate_dir(home))
    path = os.path.realpath(os.path.join(root, rel))
    if path != root and not path.startswith(root + os.sep):
        return None
    return path

def tier_b_tasks():
    """Zadania Tier B (rollout) czytane z panelu — wykonuje je tier_b.py."""
    try:
        # Jedno źródło listy: właścicielem jest `tier_b.load_tier_b_tasks` (pack-aware).
        # Wcześniej ta funkcja i `tier_b.load_tier_b_tasks` odpowiadały RÓŻNIE (7 vs 5), a runner
        # używał tej uboższej — czyli zadania Tier B z paczek nigdy nie biegły.
        import tier_b as _tb
        return _tb.load_tier_b_tasks()
    except Exception:
        return []


# ------------------------------------------------------------------ narzędzia
def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def load_panel():
    return merged_panel()[0]


def merged_panel():
    """(panel, problemy) — scalenie rdzenia z paczkami (F3).

    `panel.json` = zadania rdzenia (generyczne), `packs/<nazwa>/panel.d.json` = zadania paczki,
    włączane przez `packs` w konfiguracji. Kolejność jest DETERMINISTYCZNA: rdzeń w kolejności
    autora, potem paczki alfabetycznie (wewnątrz — kolejność autora paczki). Duplikat `id`
    nie jest cicho nadpisywany: dwa zadania o jednej tożsamości psują parowanie w `--compare`
    i raport, więc trafia jako PROBLEM widoczny w panelu.

    Brak paczki ZADEKLAROWANEJ to `fail` (konfiguracja kłamie), a paczka obecna na dysku, lecz
    nieaktywna, to jawny wiersz `N/A` z powodem — patrz `pack_rows()`.
    """
    path = os.path.join(BASE, "panel.json")
    try:
        panel = json.load(open(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return ({"panel_version": "?", "tasks": []},
                [f"nie można odczytać panelu rdzenia ({type(e).__name__})"])
    tasks = list(panel.get("tasks") or [])
    problems, seen = [], {t.get("id") for t in tasks}
    for name in sorted(PATHS["packs"]):
        pfile = os.path.join(BASE, "packs", name, "panel.d.json")
        if not os.path.isfile(pfile):
            problems.append(f"paczka `{name}` włączona w konfiguracji, a brak `packs/{name}/"
                            f"panel.d.json` — konfiguracja deklaruje coś, czego nie ma")
            continue
        try:
            pdata = json.load(open(pfile, encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            problems.append(f"paczka `{name}`: panel.d.json nieczytelny ({type(e).__name__})")
            continue
        for t in pdata.get("tasks") or []:
            tid = t.get("id")
            if tid in seen:
                problems.append(f"duplikat id zadania `{tid}` (paczka `{name}`) — "
                                f"panel niejednoznaczny")
                continue
            seen.add(tid)
            t = dict(t)
            t["paczka"] = name
            tasks.append(t)
    panel["tasks"] = tasks
    return panel, problems


def pack_rows():
    """Wiersze o PACZKACH: brak zadeklarowanej paczki = `fail`; obecna, lecz nieaktywna = `na`.

    Dlaczego wiersz na PACZKĘ, a nie na każde zadanie: użytkownik bez naszej konwencji nie ma
    `kb/` ani mirrora, więc 4 wiersze `N/A` o pojedynczych zadaniach byłyby szumem. Jedna linia
    mówi to samo i wskazuje, co zrobić (P4: „nie dotyczy" musi być JAWNE, ale nie musi być głośne).
    """
    rows = []
    pdir = os.path.join(BASE, "packs")
    present = sorted(d for d in os.listdir(pdir)) if os.path.isdir(pdir) else []
    present = [d for d in present if os.path.isfile(os.path.join(pdir, d, "panel.d.json"))]
    for name in sorted(set(present) - set(PATHS["packs"])):
        n = len(json.load(open(os.path.join(pdir, name, "panel.d.json"),
                               encoding="utf-8")).get("tasks") or [])
        rows.append({"id": f"PACK-{name}", "status": "na", "code": "PACK_DISABLED",
                     "params": {"pack": name, "n": n},
                     "summary": f"N/A: paczka `{name}` obecna, ale nieaktywna "
                                f"(konfiguracja `packs` jej nie włącza) — pominięto {n} zadań",
                     "details": [f"aby włączyć: dopisz \"{name}\" do `packs` w gate.config.json "
                                 f"i przezbrój manifest (`panel_lock.py --write`)"]})
    for name in sorted(set(PATHS["packs"]) - set(present)):
        rows.append({"id": f"PACK-{name}", "status": "fail", "code": "PACK_MISSING",
                     "params": {"pack": name},
                     "summary": f"paczka `{name}` włączona w konfiguracji, a nie ma jej na dysku",
                     "details": [f"szukano: packs/{name}/panel.d.json"]})
    return rows


def eval_version_line():
    th = thresholds()
    return {"evaluator_version": EVALUATOR_VERSION, "thresholds_hash": digest(th)}


def check_argv(task, root):
    """Pełne argv checka zadania — JEDNO miejsce dla Tier A i Tier B, testowalne bez uruchamiania.

    Rozgałęzienie jest po TIERZE, nie po „nie-A-005". Do 2026-11-09 gałąź domyślna obsługiwała
    razem Tier A i Tier B, więc:
      * każdy check Tier B dostawał argv bez `--scratch` (a to argument `required=True` w każdym
        z nich) — polecenie nie do uruchomienia, niewidoczne, bo ta ścieżka nie biegła w produkcji;
      * próba naprawy przez wspólny `tier_b.check_argv` odebrała `--home` checkom Tier A i dała im
        `--scratch`, którego nie znają → `rc=2` i CAŁY panel czerwony (złapane pełnym zestawem,
        6 testów, 2026-11-09).
    Strażnik `tests/test_tierB_falsification.py::ArgvContractTest` porównuje argv z tego miejsca
    z argumentami ZADEKLAROWANYMI przez każdy check (w obie strony: brak wymaganego = nadmiarowy).
    """
    check = resolved_check(task["check"])
    if task.get("id") == "A-005-panel-integrity":
        # A-005 mierzy DRUGIE drzewo (kotwicę), więc potrzebuje dwóch korzeni, nie jednego.
        własna = os.path.join(root, "scripts", "harness_eval")
        if os.path.realpath(root) == os.path.realpath(HERMES_HOME):
            h, rp = HERMES_HOME, GIT_REPO          # bramka mierzy samą siebie (kotwica z konfiguracji)
        elif os.path.isdir(własna):
            h, rp = root, root                     # wariant KODU: jego własny manifest i kotwica
        else:
            h, rp = HERMES_HOME, GIT_REPO          # wariant TREŚCI: mierzymy bramkę, która pracuje
        return [sys.executable, check, "--home", h, "--repo", rp, "--repo-path", ANCHOR_REPO_PATH]
    if task.get("tier") == "B":
        import tier_b as _tb                        # import lokalny (tier_b → harness_eval też lokalnie)
        return _tb.check_argv(task, root, root)
    return [sys.executable, check, "--variant", root, "--home", root]


def run_check(task, root, timeout=180):
    check = resolved_check(task["check"])
    if check is None:
        return {"id": task["id"], "status": "fail",
                "summary": f"check poza katalogiem bramki: {task['check']}", "details": [], "rc": 2}
    if not os.path.isfile(check):
        return {"id": task["id"], "status": "skip", "summary": f"brak checka {task['check']}",
                "details": [], "rc": 2}
    # Zadanie Tier B mierzące WYKONANIE potrzebuje dowodu (`--evidence`), którego panel nie tworzy:
    # odmawiamy GŁOŚNO, zamiast wysyłać checkowi polecenie nie do uruchomienia.
    if task.get("tier") == "B" and (task.get("evidence_probe") or task.get("trace_probe")):
        return {"id": task["id"], "status": "fail", "rc": 2, "details": [],
                "summary": ("panel nie tworzy dowodu dla zadania mierzącego wykonanie — "
                            "uruchom je przez `--tier-b`, nie przez panel")}
    try:
        cmd = check_argv(task, root)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"id": task["id"], "status": "fail", "summary": f"timeout {timeout}s",
                "details": [], "rc": 124}
    out = (p.stdout or "").strip().splitlines()
    if not out:
        return {"id": task["id"], "status": "fail",
                "summary": f"check bez wyjścia (rc={p.returncode})",
                "details": [(p.stderr or "")[-300:]], "rc": p.returncode}
    try:
        data = json.loads(out[-1])
    except json.JSONDecodeError:
        return {"id": task["id"], "status": "fail", "summary": "wyjście checka nie jest JSON-em",
                "details": out[-3:], "rc": p.returncode}
    if not isinstance(data, dict):
        return {"id": task["id"], "status": "fail", "summary": "wyjście checka nie jest obiektem JSON",
                "details": out[-3:], "rc": p.returncode}
    data["rc"] = p.returncode
    # F3: kontrakt checka obowiązuje w obie strony. `status=pass` przy niezerowym rc
    # to niespełniony kontrakt, nie sukces — inaczej check „kłamie" i panel jest zielony.
    # F2.3: status `na` („nie dotyczy") jest jawnym oświadczeniem checka, więc musi nieść POWÓD
    # i rc=0 — inaczej deklaracja niestosowalności byłaby tańsza niż uczciwy `fail`.
    if data.get("status") not in ("pass", "fail", "skip", "na"):
        return {"id": task["id"], "status": "fail",
                "summary": f"kontrakt checka: nieznany status {data.get('status')!r}",
                "details": data.get("details") or [], "rc": p.returncode}
    if data["status"] == "na" and not (data.get("summary") or "").strip():
        return {"id": task["id"], "status": "fail",
                "summary": "kontrakt checka: status=na bez powodu (nie dotyczy ≠ milczenie)",
                "details": data.get("details") or [], "rc": p.returncode}
    if data["status"] in ("pass", "na", "skip") and p.returncode != 0:
        return {"id": task["id"], "status": "fail",
                "summary": f"kontrakt checka: status={data['status']} przy rc={p.returncode}",
                "details": data.get("details") or [], "rc": p.returncode}
    return data


def run_panel(root, tier="A", verbose=False):
    tasks_panel, problems = merged_panel()
    results = []
    for task in tasks_panel["tasks"]:
        if task["tier"] != tier:
            continue
        t0 = time.time()
        r = run_check(task, root)
        r["id"] = task["id"]          # kontrakt checka zwraca "check"; runner normalizuje na "id"
        r["czas_s"] = round(time.time() - t0, 2)
        if task.get("paczka"):
            r["paczka"] = task["paczka"]
        results.append(r)
        if verbose:
            mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP", "na": "N/A"}.get(r["status"], "?")
            print(f"  [{mark}] {task['id']}: {r['summary']} ({r['czas_s']:.2f}s)")
    if tier == "A":
        # Wiersze o PACZKACH i problemach panelu lądują NA KOŃCU listy i wydruku: są informacją
        # o zakresie egzaminu, nie wynikiem zadania. Milczenie o nich byłoby najgorsze —
        # użytkownik nie odróżniłby „nie dotyczy" od „nie ma" (P4), a brak zadeklarowanej paczki
        # to `fail` (konfiguracja deklaruje coś, czego nie ma), nie cicha luka.
        rows = pack_rows() + [{"id": "PANEL-PROBLEM", "status": "fail", "summary": p,
                               "details": []} for p in problems]
        for row in rows:
            row["czas_s"] = 0.0
            results.append(row)
            if verbose:
                mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP", "na": "N/A"}.get(row["status"], "?")
                print(f"  [{mark}] {row['id']}: {row['summary']}")
        # F3: TOCTOU — A-005 sprawdzony na starcie nie odpowiada za stan PO ostatnim
        # checku. Manifest weryfikujemy drugi raz na końcu; wiersz dokładamy TYLKO gdy
        # wyjdzie `fail`, żeby nie mnożyć szumu w zdrowym przebiegu (cicha zgoda = brak
        # wiersza, bo A-005 na starcie już był widoczny).
        post = [t for t in tasks_panel["tasks"] if t.get("id") == "A-005-panel-integrity"]
        if post:
            t1 = time.time()
            r = run_check(post[0], root)
            if r.get("status") != "pass":
                r["id"] = "A-005-integrity-post"
                r["czas_s"] = round(time.time() - t1, 2)
                results.append(r)
                if verbose:
                    mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP",
                            "na": "N/A"}.get(r["status"], "?")
                    print(f"  [{mark}] {r['id']}: {r['summary']} ({r['czas_s']:.2f}s)")
    return results


def summarise(results):
    return {"pass": sum(r["status"] == "pass" for r in results),
            "fail": sum(r["status"] == "fail" for r in results),
            "skip": sum(r["status"] == "skip" for r in results),
            "na": sum(r["status"] == "na" for r in results),
            "total": len(results)}


def panel_exit_code(results):
    """Nonzero dla każdej niezaliczonej kontroli, by CI/cron nie widział zielonego wyniku.

    `na` (nie dotyczy — świadoma deklaracja checka z powodem, np. parytet wyłączony w konfiguracji)
    nie jest niezaliczeniem: inaczej pierwsza instalacja bez konfiguracji nie mogłaby być zielona
    (R-UX2), a `harness-gate` miałby zawsze kod 1. `skip` ZOSTAJE blokerem — „nie ma czego
    sprawdzić" to brak dowodu, nie brak wymagania.
    """
    return 0 if results and all(r.get("status") in ("pass", "na") for r in results) else 1


# --------------------------------------------------- warstwa komunikatów (R-UX4/5/7)
# KODY maszynowe (stabilne, nadawane przez checki i preflight) → klucze komunikatów.
# Wiążemy po KODZIE, nie po treści zdania: zdanie może się zmienić, kod jest kontraktem.
_CODE_MESSAGES = {"CONFIG_INVALID": "config.invalid",
                  "ANCHOR_MISSING": "anchor.missing", "ANCHOR_UNREADABLE": "anchor.unreadable",
                  "ANCHOR_EMPTY": "anchor.empty",
                  "TAMPERING_DETECTED": "integrity.drift", "LOCK_DRIFT": "integrity.drift",
                  "LOCK_MISSING": "integrity.missing_lock",
                  "PACK_DISABLED": "pack.disabled", "PACK_MISSING": "pack.missing",
                  "TIER_B_DISABLED": "tierb.disabled"}


def lang_of():
    return (PATHS or {}).get("lang") or "en"


def row_message(row):
    """Jeden wiersz panelu PO LUDZKU. Kolejność: kod (jeśli znamy klucz) → id + techniczne
    `summary` jako fallback. Fallback jest wymagany (R-UX7): nieznany klucz nie może zniknąć
    z raportu ani wyglądać jak pustka."""
    lang = lang_of()
    key = _CODE_MESSAGES.get(str(row.get("code") or ""))
    summary = row.get("summary") or ""
    if key:
        return messages.render_fallback(lang, key, summary, **(row.get("params") or {}))
    item = {"fail": "panel.item.fail", "skip": "panel.item.skip",
            "na": "panel.item.na"}.get(str(row.get("status")), "panel.item.fail")
    return messages.render_fallback(lang, item, summary, id=row.get("id"), summary=summary)


def human_lines(results, report_path="", hint=True):
    """Domyślne wyjście panelu: 5–6 linii, bez żargonu i oskarżeń (R-UX4/R-UX5).

    Pełna tabela jest w `--verbose`; tutaj wchodzi wyłącznie to, co użytkownik ma zrobić.
    Wiersze `na` („nie dotyczy") nie trafiają do domyślnego widoku — nie są problemem, a przy
    czystym przebiegu nagłówek już mówi „nic nie wymaga uwagi"; w `--verbose` widać je wszystkie.
    `hint=False` w trybie `--verbose` (nie odsyłamy do trybu, w którym już jesteśmy).
    """
    lang = lang_of()
    s = summarise(results)
    if not s["fail"] and not s["skip"]:
        lines = [messages.render(lang, "panel.all_ok", ok=s["pass"], total=s["total"])]
    else:
        parts = ""
        for key, n in (("panel.part.fail", s["fail"]), ("panel.part.skip", s["skip"])):
            if n:
                parts += messages.render(lang, key, n=n)
        lines = [messages.render(lang, "panel.headline", ok=s["pass"], total=s["total"],
                                 parts=parts)]
    lines += [row_message(r) for r in results
              if r.get("status") in ("fail", "skip")]
    # Wiersze o PACZKACH mówią o ZAKRESIE egzaminu, nie o problemie: „ten zestaw jest, ale
    # nieaktywny — tak go włączysz". Pokazujemy je także w czystym przebiegu (inaczej użytkownik
    # nie dowie się, że w ogóle ma jakiś nieaktywny zestaw). Pozostałe `na` zostają w `--verbose`.
    lines += [row_message(r) for r in results
              if r.get("status") == "na" and str(r.get("id", "")).startswith("PACK-")]
    if report_path:
        lines.append(messages.render(lang, "panel.report", path=report_path))
        if hint:
            lines.append(messages.render(lang, "panel.verbose_hint"))
    return lines


def refusal_human(code, summary):
    """Odmowa startu PO LUDZKU: kod → komunikat z instrukcją, inaczej techniczne `summary`."""
    key = _CODE_MESSAGES.get(str(code or ""))
    if key is None:
        return summary
    params = {"error": CFG.get("error") or summary, "summary": summary,
              "repo": PATHS.get("anchor_repo") or "?"}
    return messages.render_fallback(lang_of(), key, summary, **params)


def anchor_integrity_inline():
    """Niezależne od A-005 sprawdzenie manifestu — chroni przed podmianą samego checkera.

    A-005 jest zwykłym checkiem w tym samym drzewie, więc podmiana jego pliku na stub
    wyłącza strażnika i panel ogłasza sukces na zepsutym systemie. To sprawdzenie żyje
    w runnerze i nie ufa wynikowi A-005: porównuje manifest na dysku z wersją w kotwicy
    Git oraz hash samego checkera A-005 z manifestem. Dla `anchor.mode=none` nie ma z czym
    porównać — zwraca `pass` z kodem ANCHOR_NONE (jawna, nie cicha, luka).
    """
    st = gate_config.anchor_status(PATHS)
    if st["code"] != "ANCHOR_OK":
        return {"status": "pass", "code": st["code"],
                "summary": f"porównanie z kotwicą pominięte: {st['code']}"}
    base = os.path.join(HERMES_HOME, "scripts", "harness_eval")
    rel = PATHS.get("anchor_repo_path") or "panel.lock"   # ścieżka manifestu W REPO kotwicy
    repo = PATHS.get("anchor_repo") or base
    lock_path = os.path.join(base, "panel.lock")          # manifest NA DYSKU jest zawsze tutaj
    try:
        with open(lock_path, "rb") as fh:
            disk = fh.read()
    except OSError as e:
        return {"status": "fail", "code": "LOCK_MISSING",
                "summary": f"brak manifestu integralności ({type(e).__name__})"}
    p = subprocess.run(["git", "-C", repo, "cat-file", "-p", f"HEAD:{rel}"],
                       capture_output=True)
    if p.returncode != 0:
        return {"status": "fail", "code": "LOCK_DRIFT",
                "summary": f"manifest nieobecny w git HEAD: {rel}"}
    if p.stdout != disk:
        return {"status": "fail", "code": "LOCK_DRIFT",
                "summary": "panel.lock na dysku różni się od wersji w git HEAD"}
    try:
        manifest = json.loads(disk.decode("utf-8"))
        files = manifest.get("files") or {}
    except (ValueError, UnicodeDecodeError):
        return {"status": "fail", "code": "LOCK_MISSING", "summary": "manifest nieczytelny"}
    checker = "checks/core/check_panel_integrity.py"
    want = files.get(checker)
    try:
        with open(os.path.join(base, checker), "rb") as fh:
            h = hashlib.sha256(fh.read()).hexdigest()                                      
    except OSError as e:
        return {"status": "fail", "code": "LOCK_MISSING",
                "summary": f"brak checkera A-005 ({type(e).__name__})"}
    if want and h != want:
        return {"status": "fail", "code": "TAMPERING_DETECTED",
                "summary": f"TAMPERING_DETECTED {checker}: {h[:12]} != {want[:12]}"}
    return {"status": "pass", "code": "ANCHOR_OK",
            "summary": "manifest i checker A-005 zgodne z kotwicą Git"}


def gate_integrity_preflight(home=None, repo=None):
    """Weryfikuj konfigurację i kotwicę, potem A-005 — PRZED listą zadań z panel.json.

    Kolejność jest istotna: najpierw mówimy, że reguły są nieczytelne albo że nie ma z czym
    porównać integralności (instrukcja naprawy), a dopiero potem oskarżamy o rozjazd treści.
    """
    if not CFG["ok"]:
        return {"status": "fail", "code": "CONFIG_INVALID", "summary": CFG["error"]}
    st = gate_config.anchor_status(PATHS)
    if st["code"] in ("ANCHOR_MISSING", "ANCHOR_UNREADABLE", "ANCHOR_EMPTY"):
        return {"status": "fail", "code": st["code"],
                "summary": st["summary"] + (f" — {st['hint']}" if st["hint"] else "")}
    home = home or HERMES_HOME
    repo = repo if repo is not None else GIT_REPO
    base = os.path.join(home, "scripts", "harness_eval")
    guard = tree_guard(home=home)
    if guard["status"] != "pass":
        return guard
    inline = anchor_integrity_inline()
    if inline["status"] != "pass":
        return {"status": "fail", "code": inline["code"], "summary": inline["summary"]}
    try:
        panel, _problems = merged_panel()
        tasks = [t for t in panel.get("tasks", []) if t.get("id") == "A-005-panel-integrity"]
    except Exception as e:
        return {"status": "fail", "summary": f"nie można odczytać panelu: {type(e).__name__}: {e}"}
    if len(tasks) != 1 or tasks[0].get("tier") != "A" \
            or tasks[0].get("check") != "checks/core/check_panel_integrity.py":
        return {"status": "fail", "summary": "A-005-panel-integrity brak lub niepoprawna definicja w panel.json"}

    # F1: checker MUSI pochodzić z tego samego drzewa, którego dotyczy werdykt — nie z BASE.
    checker = os.path.join(base, "checks", "core", "check_panel_integrity.py")
    try:
        result = subprocess.run(
            [sys.executable, checker, "--home", home, "--repo", repo,
             "--repo-path", ANCHOR_REPO_PATH],
            capture_output=True, text=True, timeout=30, check=False,
        )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        report = json.loads(lines[-1]) if lines else {}
    except Exception as e:
        return {"status": "fail", "summary": f"A-005 preflight nie powiódł się: {type(e).__name__}: {e}"}
    if not isinstance(report, dict):
        return {"status": "fail", "summary": "A-005 preflight zwrócił wynik inny niż obiekt JSON"}
    if result.returncode != 0 or report.get("status") != "pass":
        # F3: kod A-005 musi dotrzeć do warstwy komunikatów. Bez niego runner nadawał
        # „PREFLIGHT", którego messages.json nie zna, i użytkownik widział surowy tekst.
        return {"status": "fail", "code": report.get("code") or "INTEGRITY_FAIL",
                "summary": report.get("summary") or "A-005 preflight zwrócił niepoprawny wynik"}
    return {"status": "pass", "summary": report.get("summary", "A-005 PASS")}


def tier_b_verdict(tier_b_result, params):
    """Werdykt Tier B liczony na SCENARIUSZACH. Zwraca `(werdykt, uzasadnienie)`.

    Kolejność klas jak w Tier A: **przegrana PRZED awarią infrastruktury**. Uzasadnienie jest
    w kodzie: `paired()` wyrzuca zadanie z nogą `infra_error` w całości, więc przegrana i awaria
    dotyczą RÓŻNYCH zadań — awaria nie unieważnia realnego dowodu regresji.
    """
    import tier_b as _tb                                  # import lokalny (jak w tier_b → sign_test_p)
    pary = tier_b_result.get("pairs") or []
    # Fail-closed jak w `gate_config`: wejście NIESPÓJNE (są policzone wygrane/przegrane, ale brak par,
    # z których one powstają) nie może wyglądać jak „brak danych" — inaczej cicha zmiana kształtu
    # wyniku zamienia się w `TIER_B_REQUIRED` bez śladu przyczyny.
    if not pary and (tier_b_result.get("wins") or tier_b_result.get("losses")):
        return "TIER_B_REQUIRED", (
            "niespójny wynik Tier B: są `wins`/`losses`, ale brak `pairs` — dowód liczy się na "
            "scenariuszach, a te powstają z par; nie zgaduję wyniku z samych liczników")
    rozstrzygnięte, chwiejne, _remisy = _tb.scenario_outcomes(pary)
    przegrane = [t for wynik, t in rozstrzygnięte if wynik == "przegrana"]
    wygrane = [t for wynik, t in rozstrzygnięte if wynik == "wygrana"]
    if przegrane:
        # Ostrzeżenie zostaje z poprzedniej reguły: niepełny przebieg nie może uchodzić za pełny
        # pomiar, ale przegrana JEST dowodem regresji i nie wolno jej schować pod awarią kabli.
        uwaga = ""
        if tier_b_result.get("infra_errors"):
            uwaga = (f" UWAGA: {len(tier_b_result['infra_errors'])} noga/nogi nie wystartowały — "
                     f"przebieg niepełny, choć przegrana pozostaje dowodem regresji.")
        return "REJECT", f"Tier B: przegrane na {len(przegrane)} zadaniach: {przegrane}.{uwaga}"
    if tier_b_result.get("infra_errors"):
        broken = tier_b_result["infra_errors"]
        return "INFRA_ERROR", (
            f"{len(broken)} noga/nogi rolloutu NIE wystartowały ({broken[:3]}) — awaria drogi do "
            f"modelu (klucz/saldo/sieć), nie wynik kandydata; sprawdź provider_health i powtórz serię")
    if chwiejne:
        return "REJECT", (
            f"chwiejne zadania: {chwiejne} — powtórzenia tego samego zadania dają różne wyniki, więc "
            f"egzamin nie jest dowodem (defekt EGZAMINU, nie kandydata)")
    m_min = int(params.get("tier_b_m_min", 12))
    delta = float(params.get("tier_b_delta", 0.2))
    n, w = len(rozstrzygnięte), len(wygrane)
    if n < m_min:
        return "TIER_B_REQUIRED", (
            f"za mało rozstrzygniętych SCENARIUSZY: {n}/{m_min} (powtórzenia tego samego zadania "
            f"nie są niezależnym dowodem) — dobij zadania, nie powtórzenia")
    dolna = _tb.cp_lower(w, n, conf=1 - delta)
    if w == n and dolna > 0.5:
        return "ACCEPT", (
            f"{w}/{n} scenariuszy wygranych, dolna granica {1 - delta:.0%} = {dolna:.3f} > 0.5 "
            f"(test znaków p={sign_test_p(w, n - w):.3f}, m_min={m_min})")
    return "REJECT", (
        f"dowód niewystarczający: {w}/{n} wygranych, dolna granica {1 - delta:.0%} = {dolna:.3f} "
        f"nie przekracza 0.5")


def sign_test_p(wins, losses):
    """Jednostronny dokładny test znaków na parach rozstrzygniętych (remisy pomijane)."""
    n = wins + losses
    if n == 0:
        return 1.0
    return sum(math.comb(n, k) for k in range(wins, n + 1)) / 2 ** n


def hoeffding_threshold(m, delta, k=1, t=1):
    return math.sqrt(2 * math.log(k * t / delta) / m)


def compare(incumbent, candidate):
    inc = {r["id"]: r for r in incumbent}
    wins, losses, ties = [], [], []
    for r in candidate:
        i = inc.get(r["id"])
        if i is None:
            # Wiersz istnieje TYLKO u kandydata. Do 2026-11-09 każdy taki wiersz był `tie`,
            # więc `A-005-integrity-post` (dryf wykryty PO pomiarze) nie blokował niczego —
            # wykryte oszustwo lądowało w remisach i kandydat mógł dostać ACCEPT
            # (recenzja zewnętrzna §3.1). `fail` po jednej stronie to DOWÓD, nie brak porównania.
            (losses if r["status"] == "fail" else ties).append(r["id"])
            continue
        if r["status"] == "skip" or i["status"] == "skip":
            ties.append(r["id"])
            continue
        if i["status"] == "pass" and r["status"] == "fail":
            losses.append(r["id"])
        elif i["status"] == "fail" and r["status"] == "pass":
            wins.append(r["id"])
        else:
            ties.append(r["id"])
    return {"wins": wins, "losses": losses, "ties": ties,
            "blockers": [
                f"{side}:{r.get('id', '?')}={r.get('status', 'brak-statusu')}"
                for side, rows in (("incumbent", incumbent), ("kandydat", candidate))
                for r in rows
                if r.get("status") == "skip"
                # OBA wiersze integralności blokują: `-integrity-post` to dryf wykryty podczas
                # pomiaru (TOCTOU), czyli dokładnie ta manipulacja, przed którą broni bramka.
                or (r.get("id") in ("A-005-panel-integrity", "A-005-integrity-post")
                    and r.get("status") != "pass")
            ] + _applicability_changes(inc, candidate),
            "p_value": sign_test_p(len(wins), len(losses))}


def _applicability_changes(inc, candidate):
    """`na` ≠ `na` między nogami to ZMIANA STOSOWALNOŚCI, nie remis (F2.3).

    Reguły pochodzą z konfiguracji BRAMKI, więc obie nogi widzą tę samą konfigurację: jeśli
    zadanie raz jest stosowalne, a raz nie, różnica dotyczy drzew mierzonych — albo ktoś
    wyłączył zadanie po jednej stronie. `na` jest neutralne dla werdyktu TYLKO symetrycznie,
    inaczej „wyłączenie checka" byłoby najtańszą drogą do ACCEPT.
    """
    out = []
    for r in candidate:
        i = inc.get(r["id"])
        if i is None:
            continue
        if (i.get("status") == "na") != (r.get("status") == "na"):
            out.append(f"stosowalność:{r['id']}={i.get('status')}→{r.get('status')}")
    return out


def verdict(comp, klasa, tier_b=None):
    if comp.get("blockers"):
        return "REJECT", "panel nie jest czysty (FAIL/SKIP lub brak statusu): " + ", ".join(comp["blockers"])
    if comp["losses"]:
        return "REJECT", f"regresja: przegrane na {len(comp['losses'])} zadaniach: {comp['losses']}"
    if klasa in ("1C", "2"):
        if not tier_b_tasks():
            return "TIER_B_REQUIRED", ("klasa " + klasa + " wymaga Tier B, a panel nie ma "
                                       "zdefiniowanych zadań rolloutowych")
        if tier_b is None:
            return "TIER_B_REQUIRED", "Tier B nieuruchomiony"
        # Kolejność ma znaczenie: dowiedziona REGRESJA to REJECT (fail-fast, natychmiast), a dopiero
        # brak mocy to „dobij pary". Odwrotna kolejność zamieniłaby regresję w „za mało danych".
        # Awaria drogi do modelu to NIE werdykt o kandydacie (dodane 2026-10-27 po incydencie 402):
        # gdy dostawca był martwy, nieudane nogi dawały „przegrane" i REJECT, czyli zmiana dostawcy
        # odrzucała dobre zmiany.
        # KOLEJNOŚĆ (zmieniona 2026-11-09, recenzja zewnętrzna §1.4): NAJPIERW przegrana, POTEM
        # awaria. Wcześniej `INFRA_ERROR` miał priorytet, więc jedna nieudana noga unieważniała
        # realne `REJECT` — czyli wystarczyło, by jakakolwiek noga nie wystartowała, i udowodniona
        # regresja znikała. Odwrotna kolejność też była zła, ale w drugą stronę (awaria dostawcy
        # odrzucała dobre zmiany): dlatego przegrana wygrywa, a obecność awarii jest W TRESCI
        # wyroku, żeby nikt nie wziął zaniżonego wyniku za pełny pomiar.
        return tier_b_verdict(tier_b, GATE_PARAMS)
    if comp["wins"]:
        return "ACCEPT", f"bez regresji, wygrane na {len(comp['wins'])}: {comp['wins']}"
    return "REJECT", "brak wygranych — brak dowodu poprawy (remis/identyczny wynik)"


def save_run(payload, artifact):
    os.makedirs(RUNS, exist_ok=True)
    path = os.path.join(RUNS, f"{time.strftime('%Y%m%d-%H%M%S')}-{artifact or 'panel'}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def append_ledger(entry):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return LEDGER


# ------------------------------------------------------------------ wariant
HEAVY = {"assets", "node_modules", ".git", "__pycache__", "cache", "venv", ".venv",
         # zmierzone 2026-10-15: .hub/index-cache trzyma 40 MB indeksu huba (cache!),
         # a .curator_backups kolejne ~6 MB archiwów — w wariancie to czysty balast,
         # niezwiązany z zachowaniem harnessu. Bez tego wariant ważył 60 MB zamiast ~15 MB.
         ".hub", ".curator_backups", "logs", "sessions", "audio_cache", "image_cache",
         "scratch", "vendor"}
HEAVY_FILES = ("models_dev_cache.json", "models_dev_cache.etag")  # cache modeli — symlink


def variant_name_ok(name):
    """F4: nazwa wariantu to NAZWA, nie ścieżka. Bez tej walidacji `os.path.join(VARIANTS, name)`
    z nazwą absolutną zwraca tę nazwę i `shutil.rmtree` kasuje dowolny katalog."""
    if not isinstance(name, str) or not name:
        return False
    if name != os.path.basename(name) or name in (".", "..") or name.startswith("."):
        return False
    if os.sep in name or (os.altsep and os.altsep in name) or os.path.isabs(name):
        return False
    return True


def variant_path(name):
    """Ścieżka wariantu musi leżeć bezpośrednio w eval/variants (realpath, nie na słowo)."""
    if not variant_name_ok(name):
        raise ValueError(f"niedozwolona nazwa wariantu: {name!r} (dozwolone: nazwa bez '/', '..', "
                         f"ścieżek absolutnych i bez kropki na początku)")
    root = os.path.realpath(VARIANTS)
    dst = os.path.realpath(os.path.join(VARIANTS, name))
    if os.path.dirname(dst) != root:
        raise ValueError(f"ścieżka wariantu poza {root}: {dst}")
    return dst


def make_variant(name, source=HERMES_HOME):
    dst = variant_path(name)
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.makedirs(dst, exist_ok=True)
    # Sekretów NIE kopiujemy: auth.json (tokeny nous/openai) i .env zostają w produkcji.
    # Do rolloutu trafia wyłącznie klucz modelu, wstrzyknięty przez środowisko (tier_b.py).
    for item in ("config.yaml", "SOUL.md"):
        src = os.path.join(source, item)
        if os.path.isfile(src):
            shutil.copy2(src, dst)
    # cache/ i installs/ mogą urosnąć do ~1 GB na wariant, a są regenerowalne.
    # Współdzielimy je między wariantami przez symlink — rollout nie płaci drugi raz
    # za instalację narzędzi, a pomiar harnessu pozostaje izolowany (stan sesji,
    # skills, memories i kb żyją w wariancie).
    shared = os.path.join(VARIANTS, ".shared")
    for name in ("cache", "installs", "root"):
        target = os.path.join(shared, name)
        os.makedirs(target, exist_ok=True)
        link = os.path.join(dst, name)
        if not os.path.islink(link):
            os.symlink(target, link)
    for fn in HEAVY_FILES:                    # pliki cache: symlink, nie kopia
        src = os.path.join(source, fn)
        if os.path.isfile(src):
            target = os.path.join(shared, "root", fn)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if not os.path.exists(target):
                shutil.copy2(src, target)
            os.symlink(target, os.path.join(dst, fn))
    for sub in ("skills", "memories", "kb"):
        src = os.path.join(source, sub)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dst, sub),
                            ignore=shutil.ignore_patterns(*HEAVY), symlinks=False)
    # PUSTA baza stanu: każdy prawdziwy dom Hermesa ma `state.db`, a wariant ma być DOMEM dla
    # rolloutu (tier_b czyta z niej sondy: „czy rollout wystartował"). Nie kopiujemy produkcyjnej
    # bazy (prywatność sesji), tylko zakładamy pustą — precedens: `tools/foreign_world.py`.
    # Bez tego A-009 czerwienieje („nie wygląda na instalację Hermesa: brak state.db") i blokuje
    # Tier B przed pierwszym wywołaniem modelu (znalezione pre-flightem 2026-11-09).
    # PUSTA, ale POPRAWNA baza stanu: `sqlite3.connect(...).close()` zostawia plik 0-bajtowy,
    # a Hermes traktuje taki jako USZKODZONY i przenosi go w bok („state.db was empty or damaged")
    # — czyli wariant wita rollout komunikatem błędu. Tworzymy prawdziwy nagłówek SQLite.
    state_db = os.path.join(dst, "state.db")
    if not os.path.isfile(state_db) or os.path.getsize(state_db) == 0:
        import sqlite3
        con = sqlite3.connect(state_db)
        con.execute("CREATE TABLE IF NOT EXISTS _gate_init (x INTEGER)")
        con.execute("DROP TABLE _gate_init")
        con.commit()
        con.close()
    return dst


def _size_mb(path):
    if os.path.islink(path) or not os.path.exists(path):
        return 0.0
    total = 0
    for dp, _, fs in os.walk(path):
        for f in fs:
            fp = os.path.join(dp, f)
            if os.path.islink(fp):
                continue
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total / 1e6


def prune_variants(keep=3, max_total_mb=1500):
    """Retencja: warianty są regenerowalne, więc trzymamy tylko `incumbent` i N ostatnich.
    Zapobiega cichej akumulacji setek MB po kilkudziesięciu testach Tier B."""
    if not os.path.isdir(VARIANTS):
        return []
    cand = []
    for name in os.listdir(VARIANTS):
        p = os.path.join(VARIANTS, name)
        if name in (".shared", "incumbent") or os.path.islink(p) or not os.path.isdir(p):
            continue
        cand.append((os.path.getmtime(p), name, _size_mb(p)))
    cand.sort(reverse=True)
    removed = []
    for _, name, _mb in cand[keep:]:
        shutil.rmtree(os.path.join(VARIANTS, name), ignore_errors=True)
        removed.append(name)
    total = _size_mb(VARIANTS)
    if total > max_total_mb:
        print(f"UWAGA: eval/variants zajmuje {total:.0f} MB (> {max_total_mb} MB) — "
              f"uruchom --clean", file=sys.stderr)
    return removed


def archive_rejected(artifact, meta):
    """Archiwum odrzuconych kandydatów — żeby nie generować w kółko tego samego."""
    d = os.path.join(HERMES_HOME, "skills", ".candidates", artifact,
                     time.strftime("%Y-%m-%d"))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return d


# ------------------------------------------------------------------ testy SPEC-u
def new_sandbox_scratch(root):
    """Utwórz prywatny katalog testowy bez naruszania istniejących scratchy."""
    os.makedirs(root, exist_ok=True)
    return tempfile.mkdtemp(prefix="sandbox-selftest-", dir=root)


def test_sandbox():
    """Test akceptacyjny 5 — izolacja Tier B (Landlock)."""
    scratch_root = os.path.join(EVAL_DIR, "scratch")
    scratch = new_sandbox_scratch(scratch_root)
    wrapper = os.path.join(BASE, "sandbox_landlock.py")
    child = os.path.join(BASE, "sandbox_selftest.py")
    checks, ok = [], True

    def run(extra, label):
        # Dom MIERZONY przekazujemy dziecku jawnie: sonda ma badać tę ścieżkę, którą dostaje
        # rollout, a nie zaszyte `~/.hermes` (naprawa 2026-11-09 — patrz sandbox_selftest.py).
        cmd = [sys.executable, wrapper] + extra + ["--", sys.executable, child, scratch, HERMES_HOME]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        try:
            data = json.loads((p.stdout or "").strip().splitlines()[-1])
        except Exception:
            data = {"_error": (p.stdout or "")[-200:] + (p.stderr or "")[-200:], "_rc": p.returncode}
        checks.append({"case": label, "rc": p.returncode, "result": data})
        return data

    try:
        d = run(["--rw", scratch, "--allow-net-port", "443"], "zapis w scratch + sieć tylko 443")
        expectations = {"write_scratch": "allowed", "write_home": "blocked", "write_kb": "blocked",
                        "write_panel": "blocked", "write_runs": "blocked", "net_ssh": "blocked"}
        for k, want in expectations.items():
            got = d.get(k)
            if got != want:
                ok = False
                checks.append({"case": f"oczekiwano {k}={want}", "got": got})
        if d.get("net_https") not in ("passed_no_listener", "allowed"):
            ok = False
            checks.append({"case": "oczekiwano net_https przepuszczone (443 dozwolony)", "got": d.get("net_https")})

        d2 = run(["--rw", scratch, "--no-net"], "całkowita blokada sieci")
        if d2.get("net_https") != "blocked":
            ok = False
            checks.append({"case": "oczekiwano net_https=blocked przy --no-net", "got": d2.get("net_https")})

        # fail-closed: katalog bez prawa zapisu NIE może przepuścić komendy
        p = subprocess.run([sys.executable, wrapper, "--rw", "/etc", "--", "/bin/echo", "PRZECIEKŁO"],
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 3 or "PRZECIEKŁO" in (p.stdout or ""):
            ok = False
            checks.append({"case": "fail-closed na --rw /etc", "rc": p.returncode,
                           "stdout": (p.stdout or "").strip()[:80]})
        else:
            checks.append({"case": "fail-closed na --rw /etc", "rc": 3, "result": "komenda zablokowana"})
        return ok, checks
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_mutacja():
    """Test akceptacyjny 1 — negatywna kontrola: bramka MUSI wykryć zepsuty harness."""
    variant = make_variant("selftest")
    target = None
    for dirpath, dirnames, filenames in os.walk(os.path.join(variant, "skills")):
        dirnames[:] = [d for d in dirnames if d not in HEAVY]
        for fn in filenames:
            if fn.endswith(".py"):
                target = os.path.join(dirpath, fn)
                break
        if target:
            break
    if target is None:
        target = os.path.join(variant, "skills", "__selftest_broken.py")
        os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "a", encoding="utf-8") as f:
        f.write("\ndef __mutacja(:\n    return 1\n")
    incumbent = run_panel(HERMES_HOME)
    candidate = run_panel(variant)
    comp = compare(incumbent, candidate)
    v, reason = verdict(comp, "1B")
    ok = v == "REJECT" and "A-001-skill-scripts-compile" in comp["losses"]
    report = {"variant": variant, "mutated_file": os.path.relpath(target, variant),
              "verdict": v, "reason": reason,
              "incumbent": summarise(incumbent), "candidate": summarise(candidate),
              "compare": comp, "expected": "REJECT z przegraną na A-001"}
    shutil.rmtree(variant, ignore_errors=True)
    return ok, report


# ------------------------------------------------------------------ main
def fix_installation(verbose=False):
    """R-UX8/R-UX6: naprawa tego, co MUSI być spójne, gdy zmieniono pliki bramki.

    Nie zmienia treści plików — domyka trzy rzeczy: manifest (`panel.lock`) wobec drzewa,
    kotwicę Git wobec manifestu i katalog egzaminu (`eval/variants`). Mówi wprost, co zmienił.
    """
    base = BASE
    zmiany = []
    lock_path = os.path.join(base, "panel.lock")
    try:
        with open(lock_path, "rb") as fh:
            przed = fh.read()
    except OSError:
        przed = b""
    p = subprocess.run([sys.executable, os.path.join(base, "panel_lock.py"), "--write"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        print("BŁĄD: nie udało się przezbroić manifestu: "
              + (p.stderr or p.stdout or "")[-300:].strip(), file=sys.stderr)
        return 1
    try:
        with open(lock_path, "rb") as fh:
            po = fh.read()
    except OSError:
        po = przed
    if przed != po:
        zmiany.append("panel.lock ✓")                      # tokeny neutralne językowo
    if not os.path.isdir(os.path.join(base, ".git")):
        subprocess.run(["git", "init", "-q", "-b", "master"], cwd=base,
                       capture_output=True, text=True)
        zmiany.append("git init (anchor)")
    subprocess.run(["git", "add", "-A"], cwd=base, capture_output=True, text=True)
    c = subprocess.run(["git", "-c", "user.email=fix@local", "-c", "user.name=fix",
                        "commit", "-qm", "harness-gate --fix: manifest and anchor"],
                       cwd=base, capture_output=True, text=True)
    if c.returncode == 0:
        zmiany.append("git commit (anchor == manifest)")

    variants = PATHS.get("variants_dir") or os.path.join(HERMES_HOME, "eval", "variants")
    os.makedirs(variants, exist_ok=True)

    if not zmiany:
        zmiany.append(f"nothing to do ({base})")

    inline = anchor_integrity_inline()
    print(messages.render_fallback(lang_of(), "fix.done",
                                   "Naprawa zakończona ({base}).\n  • {zmiany}",
                                   base=base, zmiany="\n  • ".join(zmiany)))
    if inline["status"] != "pass":
        print(f"Bramka nadal nieprzechodnia: {inline['summary']}", file=sys.stderr)
        return 1
    if verbose:
        print(json.dumps({"naprawa": zmiany, "weryfikacja": inline, **paths_line()},
                         ensure_ascii=False))
    return 0


def _redirect_pycache():
    """Wektor `.pyc`: podmieniony bytecode jest importowany, mimo że `.py` się nie zmienił
    (nagłówek mtime/size da się podrobić). Cache przekierowujemy POZA drzewo i wyłączamy zapis,
    jednorazowym re-exec. Tylko ścieżka `__main__` — import modułu w testach nie może
    podmieniać procesu."""
    if os.environ.get("HARNESS_GATE_PYCACHE"):
        return
    os.environ["HARNESS_GATE_PYCACHE"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONPYCACHEPREFIX"] = os.path.join(tempfile.gettempdir(), "hermes-gate-pycache")
    os.execv(sys.executable, [sys.executable, "-B", os.path.abspath(__file__)] + sys.argv[1:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", action="store_true", help="Tier A na wskazanym rootcie")
    ap.add_argument("--verbose", action="store_true",
                    help="pełna tabela kontroli (domyślnie: 2–3 linie po ludzku)")
    ap.add_argument("--compare", action="store_true", help="parowane porównanie variant vs incumbent")
    ap.add_argument("--variant", default="", help="nazwa wariantu w eval/variants/<name>")
    ap.add_argument("--variant-path", default="", help="albo bezpośrednia ścieżka")
    ap.add_argument("--klasa", default="1B", choices=["1A", "1B", "1C", "2"])
    ap.add_argument("--artifact", default="", help="nazwa zmienianego artefaktu (do ledgera)")
    ap.add_argument("--certificate", action="store_true", help="policz granicę Hoeffdinga")
    ap.add_argument("--m", type=int, default=None,
                    help="m dla certyfikatu Hoeffdinga (domyślnie `tier_b_m_min` z GATE_PARAMS)")
    ap.add_argument("--delta", type=float, default=None,
                    help="δ dla certyfikatu (domyślnie `tier_b_delta` z GATE_PARAMS)")
    ap.add_argument("--selftest-mutacja", action="store_true", help="test 1 SPEC-u")
    ap.add_argument("--test-sandbox", action="store_true", help="test 5 SPEC-u")
    ap.add_argument("--no-ledger", action="store_true", help="nie zapisuj werdyktu do ledgera")
    ap.add_argument("--clean", action="store_true", help="usuń warianty i scratch (regenerowalne)")
    ap.add_argument("--keep-variants", type=int, default=3,
                    help="ile ostatnich wariantów kandydackich zachować (reszta sprzątana)")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="pomiń preflight — WYŁĄCZNIE z --selftest-mutacja (kontrola negatywna "
                         "bramki, której panel.lock nie jest jeszcze w git HEAD)")
    ap.add_argument("--changed-files", default="",
                    help="lista plików zmienionych przez kandydata (przecinek) — obowiązkowa dla ledgera")
    ap.add_argument("--make-variant", default="", help="utwórz izolowaną kopię harnessu")
    ap.add_argument("--fix", action="store_true",
                    help="naprawa: przezbrój manifest i kotwicę Git po zmianie plików bramki")
    ap.add_argument("--json", action="store_true", help="werdykt jako JSON (do skryptów)")
    ap.add_argument("--tier-b", action="store_true", help="parowany rollout Tier B (drogie!)")
    ap.add_argument("--incumbent-variant", default="incumbent", help="wariant odniesienia dla Tier B")
    ap.add_argument("--tier-b-task", default="", help="ogranicz do ID (przecinek), np. B-002")
    ap.add_argument("--tier-b-budget", type=int, default=None,
                    help="budżet sekund na Tier B (domyślnie `tier_b.budget_s` z gate.config.json)")
    ap.add_argument("--tier-b-repeats", type=int, default=None,
                    help="ile razy powtórzyć każdy task Tier B (0 = auto: dobierz do m_min z GATE_PARAMS)")
    args = ap.parse_args()

    # Reguły DECYZYJNE (m, δ) mają JEDNO źródło: `GATE_PARAMS` (hash progów → J_MUTATION).
    # Trzymanie ich drugi raz w domyślnych argparse było drugą prawdą o tym samym progu (F2.3).
    if args.m is None:
        args.m = int(GATE_PARAMS.get("tier_b_m_min", 12))
    if args.delta is None:
        args.delta = float(GATE_PARAMS.get("tier_b_delta", 0.2))
    # Operacyjne (koszt/czas, nie reguły) — z konfiguracji bramki; CLI nadpisuje jawnie.
    _tb_cfg = (CFG.get("cfg") or {}).get("tier_b") or {}
    if args.tier_b_budget is None:
        args.tier_b_budget = int(_tb_cfg.get("budget_s") or 900)
    if args.tier_b_repeats is None:
        args.tier_b_repeats = int(_tb_cfg.get("repeats") or 0)

    if args.fix:
        return fix_installation(verbose=args.verbose)

    if not CFG["ok"]:
        # R-UX4/R-UX5: w trybie domyślnym nie mówimy językiem maszyny — jedno zdanie po ludzku
        # z instrukcją. Zepsuta konfiguracja to najczęstszy „pierwszy kontakt z błędem".
        print(messages.render_fallback(lang_of(), "config.invalid", CFG["error"],
                                       error=CFG["error"]), file=sys.stderr)
        print(messages.render_fallback(lang_of(), "config.broken_hint", ""), file=sys.stderr)
        if args.verbose:
            print(json.dumps({"werdykt": "REJECT", "kod": "CONFIG_INVALID",
                              "uzasadnienie": CFG["error"], **paths_line()}, ensure_ascii=False),
                  file=sys.stderr)
        return 2

    if args.skip_preflight and not args.selftest_mutacja:
        print("BŁĄD: --skip-preflight działa wyłącznie razem z --selftest-mutacja", file=sys.stderr)
        return 2

    # F1/F5: bramka działa tylko we własnym drzewie. Kopia drzewa albo `HOME=...`
    # kończy się REJECT, zamiast sprawdzać jedno i wykonywać drugie.
    if not args.certificate:
        guard = tree_guard()
        if guard["status"] != "pass":
            print(json.dumps({"werdykt": "REJECT", "uzasadnienie": guard["summary"],
                              **paths_line()}, ensure_ascii=False), file=sys.stderr)
            return 1
        # Które drzewo mierzymy — JAWNIE i tylko wtedy, gdy to nie `$HOME/.hermes`. Użytkownik
        # z niestandardowym `HERMES_HOME` musi wiedzieć, czego dotyczy werdykt (i czemu np.
        # „brak skills/" jest prawdą o TYM drzewie). Na stderr, żeby nie zmieniać kontraktu
        # wyjścia panelu (5–6 linii na stdout).
        # Nadpisania progów przez środowisko omijają manifest (`panel.lock` obejmuje
        # `gate.config.json`, nie zmienne procesu), więc reguły mogą być luźniejsze, niż zapisane.
        # Nie blokujemy tego (to nasza furtka diagnostyczna), ale MÓWIMY — inaczej „zmiana reguł
        # bez przezbrojenia locka" przechodzi po cichu (recenzja zewnętrzna 2026-11-09, §5.1).
        _nadpisane = [k for k in ("A002_MEMORY_CHAR_LIMIT", "A002_USER_CHAR_LIMIT",
                                  "A004_MAX_WARNINGS", "HARNESS_GATE_CONFIG",
                                  "EVAL_FORBIDDEN_PROBES", "EVAL_ALLOW_NET_PORTS")
                      if os.environ.get(k)]
        if _nadpisane and not args.verbose:
            print(f"i: aktywne nadpisania środowiskowe poza manifestem: {', '.join(_nadpisane)}",
                  file=sys.stderr)
        if guard.get("note"):
            print(f"i: mierzę drzewo {HERMES_HOME} (z `HERMES_HOME`), a nie {ENV_HERMES_HOME} "
                  f"(`$HOME/.hermes`) — to nie błąd, tylko inny dom", file=sys.stderr)

    if not (args.clean or args.make_variant or args.test_sandbox or args.certificate
            or args.skip_preflight):
        preflight = gate_integrity_preflight()
        if preflight["status"] != "pass":
            code = preflight.get("code", "PREFLIGHT")
            print(refusal_human(code, preflight["summary"]), file=sys.stderr)
            if args.verbose:
                print(json.dumps({"werdykt": "REJECT", "kod": code,
                                  "uzasadnienie": preflight["summary"], **paths_line()},
                                 ensure_ascii=False), file=sys.stderr)
            return 1

    if args.clean:
        freed = 0
        for base_dir in (VARIANTS, os.path.join(EVAL_DIR, "scratch")):
            if not os.path.isdir(base_dir):
                continue
            for name in os.listdir(base_dir):
                if name == ".shared":
                    continue
                d = os.path.join(base_dir, name)
                size = sum(os.path.getsize(os.path.join(dp, f))
                           for dp, _, fs in os.walk(d) for f in fs) if os.path.isdir(d) else 0
                shutil.rmtree(d, ignore_errors=True)
                freed += size
        keep = os.path.join(VARIANTS, ".shared")
        os.makedirs(keep, exist_ok=True)
        print(f"posprzątano {freed / 1e6:.0f} MB (warianty + scratch); współdzielony cache: {keep}")
        return 0

    if args.make_variant:
        try:
            d = make_variant(args.make_variant)
        except ValueError as e:      # F4: nazwa ze ścieżką = odmowa, nigdy rmtree
            print(f"BŁĄD: {e}", file=sys.stderr)
            return 2
        # retencja PO utworzeniu: sprzątanie przed kopiowaniem zostawiało o jeden wariant
        # za dużo w stanie ustalonym (prune widział N-1 kandydatów + nowy)
        removed = prune_variants(keep=args.keep_variants)
        if removed:
            print(f"  retencja: usunięto {len(removed)} starych wariantów: {', '.join(removed)}")
        size = sum(os.path.getsize(os.path.join(dp, f))
                   for dp, _, fs in os.walk(d) for f in fs)
        print(f"wariant: {d} ({size / 1e6:.1f} MB)")
        return 0

    if args.test_sandbox:
        ok, checks = test_sandbox()
        payload = {"ts": now(), "test": "izolacja-sandbox-TierB", "pass": ok, "checks": checks,
                   **paths_line(), **eval_version_line()}
        save_run(payload, "test-sandbox")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    if args.selftest_mutacja:
        ok, report = test_mutacja()
        payload = {"ts": now(), "test": "mutacja-negatywna", "pass": ok, **report,
                   **paths_line(), **eval_version_line()}
        save_run(payload, "test-mutacja")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    if args.certificate:
        thr = hoeffding_threshold(args.m, args.delta)
        payload = {"ts": now(), "m": args.m, "delta": args.delta,
                   "hoeffding_threshold": round(thr, 4),
                   "wymagane_delta_hat": f"< -{thr:.3f}",
                   "uwaga": ("Granica Hoeffdinga przy małym m jest bardzo luźna — werdykt "
                             "opieramy na dokładnym teście znaków, nie na tym certyfikacie."),
                   **eval_version_line()}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.tier_b:
        # Tier B kosztuje tokeny — konfiguracja bramki musi go jawnie włączyć (F2.3, R-UX3).
        # Dzięki temu świeża instalacja nie uruchomi płatnych rolloutów przez nieuwagę, a brak
        # włączenia jest ZAPISANY w manifeście (a nie domyślany z kodu).
        if not _tb_cfg.get("enabled"):
            print(json.dumps({"werdykt": "REJECT", "kod": "TIER_B_DISABLED",
                              "uzasadnienie": ("Tier B wyłączony w konfiguracji bramki "
                                               "(tier_b.enabled=false) — uruchomienie rolloutów "
                                               "wymaga jawnego włączenia w gate.config.json"),
                              **paths_line()}, ensure_ascii=False, indent=2))
            return 1
        import tier_b
        # Pre-flight uprawnień rolloutu (F6, znalezisko smoke'u 2026-11-09): piaskownica dostaje
        # WYŁĄCZNIE klucze z `.env` (auth.json celowo nie jest kopiowany), więc dostawca
        # uwierzytelniany OAuth-em nie ma czym się zalogować — a objawia się to CICHYM
        # `infra_error`, który wygląda jak problem kandydata (dokładnie tak było: model z
        # `config.yaml` = nous, klucze = deepseek). Sprawdzamy PRZED wydaniem tokenów.
        provider = (tier_b.EVAL_PROVIDER or "").strip()
        # Kształt poświadczenia bierzemy z TABELI Hermesa, nie z własnego domysłu: dostawca
        # kluczowy deklaruje swoje zmienne (`huggingface` -> HF_TOKEN, nie `*_API_KEY`),
        # a dostawca OAuth nie deklaruje ŻADNEJ — i wtedy ratunkiem jest wyłącznie `hermes proxy`.
        zmienne, _base_env, znany = tier_b.provider_credentials(provider)
        dostepne = set(tier_b.ENV_WHITELIST) | set(tier_b.model_env())
        ma_klucz = bool([v for v in zmienne if v in dostepne]) or bool(tier_b.PROXY_URL)
        if not provider or not ma_klucz:
            if znany and not zmienne:
                jak = (f"dostawca '{provider}' loguje się przez OAuth (nie ma zmiennej z kluczem), "
                       f"więc uruchom `hermes proxy` i wskaż go w `tier_b.proxy.url` "
                       f"(np. http://127.0.0.1:8645/v1)")
            else:
                jak = (f"ustaw {', '.join(zmienne) or 'klucz dostawcy'} w `.env` "
                       f"albo wskaż innego dostawcę w `tier_b.provider`")
            print(json.dumps(
                {"werdykt": "REJECT", "kod": "TIER_B_NO_CREDENTIALS",
                 "uzasadnienie": (
                     f"rollout nie ma czym się zalogować: dostawca '{provider or 'nieustalony'}'"
                     + ("" if znany else " (tabeli dostawców Hermesa nie udało się odczytać — zgaduję)")
                     + f"; do piaskownicy wchodzą tylko zmienne z listy "
                       f"({', '.join(sorted(dostepne)) or 'brak'}) — {jak}."),
                 **paths_line()}, ensure_ascii=False, indent=2))
            return 1
        # Pośrednik: martwy `hermes proxy` wygląda jak awaria dostawcy, a kosztuje cały budżet
        # (2026-11-09: ~25 min rolloutów na ponowieniach). Odmawiamy w sekundę.
        _proxy_ok, _proxy_why = tier_b.proxy_health()
        if not _proxy_ok:
            print(json.dumps(
                {"werdykt": "REJECT", "kod": "TIER_B_PROXY_DOWN",
                 "uzasadnienie": (f"pośrednik z `tier_b.proxy.url` nie odpowiada: {_proxy_why}. "
                                  f"Uruchom `hermes proxy start --provider <nous|xai>` "
                                  f"(i sprawdź `hermes proxy status`) albo wyłącz `tier_b.proxy`."),
                 **paths_line()}, ensure_ascii=False, indent=2))
            return 1
        inc_root = os.path.join(VARIANTS, args.incumbent_variant)
        cand_root = os.path.join(VARIANTS, args.variant)
        for r in (inc_root, cand_root):
            if not os.path.isdir(r):
                print(f"BŁĄD: brak wariantu {r} — użyj --make-variant", file=sys.stderr)
                return 2
        tasks = tier_b.load_tier_b_tasks()
        if args.tier_b_task:
            wanted = [w.strip() for w in args.tier_b_task.split(",") if w.strip()]
            tasks = [t for t in tasks if any(t["id"].startswith(w) for w in wanted)]
        if not tasks:
            print("BŁĄD: brak zadań Tier B do uruchomienia", file=sys.stderr)
            return 2
        m_min = int(GATE_PARAMS.get("tier_b_m_min", 12))
        # Powtórzenia służą STABILNOŚCI (wykryciu chwiejnego zadania), nie mocy — moc kupujemy
        # liczbą RÓŻNYCH zadań, bo powtórzenia dzielą stan wariantu. Stara formuła
        # ceil(m_min/liczba zadań) kupowała pary, których reguła już nie liczy.
        repeats = args.tier_b_repeats or 2
        print(f"incumbent: {inc_root}\nkandydat:  {cand_root}\nzadania: "
              f"{[t['id'] for t in tasks]} × {repeats} powtórzeń = {len(tasks) * repeats} par "
              f"(rozstrzygniętych wymagane m ≥ {m_min} SCENARIUSZY; δ={GATE_PARAMS.get('tier_b_delta')})\n"
              f"(rollout = realne wywołania API)\n")
        # F2: SPEC §2.4 wymaga „najpierw Tier A (przegrane = 0), potem Tier B". Panel statyczny
        # uruchamiamy TUTAJ, przed płatnymi rolloutami — dzięki temu regresji statycznej nie da się
        # pominąć, a jej wykrycie nie kosztuje ani jednego tokenu.
        print("Tier A (statyczny) na incumbencie i kandydacie — ~0 tokenów")
        panel_inc = run_panel(inc_root, verbose=False)
        panel_cand = run_panel(cand_root, verbose=True)
        comp = compare(panel_inc, panel_cand)
        panel_summary = {"incumbent": summarise(panel_inc), "candidate": summarise(panel_cand),
                         "compare": comp}

        def record(werdykt, reason, tier_b_payload):
            payload = {"ts": now(), "mode": "tier-b", "klasa": args.klasa, "artifact": args.artifact,
                       "zadania": [t["id"] for t in tasks], "panel_tier_a": panel_summary,
                       "m_min": m_min, "repeats": repeats,
                       "tier_b": tier_b_payload, "werdykt": werdykt, "uzasadnienie": reason,
                       **paths_line(), **eval_version_line()}
            path = save_run(payload, args.artifact or "tier-b")
            if not args.no_ledger:
                append_ledger({"ts": now(), "artifact": args.artifact, "klasa": args.klasa,
                               "change_summary": f"Tier A + Tier B: {[t['id'] for t in tasks]}",
                               "trigger_episode": None,
                               "panel_summary": {"tier_a": panel_summary.get("compare"),
                                                 "tier_b": tier_b_payload},
                               "tier_b": tier_b_payload, "werdykt": werdykt, "uzasadnienie": reason,
                               "status": "PROVISIONAL" if werdykt == "ACCEPT" else werdykt,
                               "dlug_regresji": ("otwarty"
                                                 if (comp["losses"] or (tier_b_payload or {}).get("losses"))
                                                 else None),
                               **eval_version_line()})
            return path

        if comp["blockers"] or comp["losses"]:
            reason = ("Tier A nie jest czysty — rolloute NIEuruchomione (oszczędność tokenów): "
                      + (", ".join(comp["blockers"]) if comp["blockers"]
                         else f"regresja: {comp['losses']}"))
            path = record("REJECT", reason, None)
            print(json.dumps({"werdykt": "REJECT", "uzasadnienie": reason}, ensure_ascii=False, indent=2))
            print(f"zapisano: {path}")
            return 1

        tb = tier_b.paired(inc_root, cand_root, tasks, budget_s=args.tier_b_budget, repeats=repeats)
        v, reason = verdict(comp, args.klasa, tier_b=tb)
        path = record(v, reason, tb)
        print(json.dumps({"werdykt": v, "uzasadnienie": reason},
                         ensure_ascii=False, indent=2))
        print(f"zapisano: {path}")
        return 0 if v == "ACCEPT" else 1

    root = args.variant_path or HERMES_HOME
    if args.variant:
        root = os.path.join(VARIANTS, args.variant)
        if not os.path.isdir(root):
            print(f"BŁĄD: brak wariantu {root} (użyj make_variant / --variant-path)", file=sys.stderr)
            return 2

    if args.panel or not args.compare:
        res = run_panel(root, verbose=args.verbose)
        payload = {"ts": now(), "mode": "panel", "root": root, "tier": "A",
                   "summary": summarise(res), "results": res, **paths_line(),
                   **eval_version_line()}
        path = save_run(payload, args.artifact or "panel")
        if args.verbose:
            print(json.dumps(payload["summary"], ensure_ascii=False))
        for line in human_lines(res, path, hint=not args.verbose):
            print(line)
        return panel_exit_code(res)

    print(f"incumbent: {HERMES_HOME}\nkandydat:  {root}\n")
    incumbent = run_panel(HERMES_HOME, verbose=False)
    candidate = run_panel(root, verbose=True)
    comp = compare(incumbent, candidate)
    v, reason = verdict(comp, args.klasa)
    payload = {"ts": now(), "mode": "compare", "klasa": args.klasa, "artifact": args.artifact,
               "incumbent_summary": summarise(incumbent), "candidate_summary": summarise(candidate),
               "compare": comp, "werdykt": v, "uzasadnienie": reason,
               "evaluacja": {"hoeffding_threshold_m": hoeffding_threshold(args.m, args.delta)},
               **paths_line(), **eval_version_line()}
    path = save_run(payload, args.artifact or "compare")
    if not args.no_ledger:
        files = [f.strip() for f in args.changed_files.split(",") if f.strip()] or None
        if files is None and args.klasa in ("1A", "1B", "1C", "2"):
            print("UWAGA: brak --changed-files — wpis dostanie changed_files=null "
                  "(okno obserwacji oznaczy go jako niemierzalny)", file=sys.stderr)
        append_ledger({"ts": now(), "artifact": args.artifact, "klasa": args.klasa,
                       "changed_files": files,
                       "change_summary": f"porównanie wariantu {os.path.basename(root)}",
                       "trigger_episode": None, "panel_summary": comp,
                       "werdykt": v, "uzasadnienie": reason,
                       "status": "PROVISIONAL" if v == "ACCEPT" else v,
                       "dlug_regresji": "otwarty" if comp["losses"] else None,
                       **eval_version_line()})
        if v == "REJECT" and args.artifact:
            archive_rejected(args.artifact, {"werdykt": v, "reason": reason, "panel": comp["losses"],
                                             "ts": now()})
    print(json.dumps({k: payload[k] for k in ("werdykt", "uzasadnienie", "compare")},
                     ensure_ascii=False, indent=2))
    print(f"zapisano: {path}")
    return 0 if v == "ACCEPT" else 1


if __name__ == "__main__":
    _redirect_pycache()
    sys.exit(main())
