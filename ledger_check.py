#!/usr/bin/env python3
"""ledger_check.py — walidacja dziennika przyjęć + OKNO OBSERWACJI z werdyktem.

Zasady (decyzja 2026-10-15):
  * ledger.jsonl jest APPEND-ONLY. Historii nie edytujemy — uzupełnienia wchodzą jako
    osobne wpisy status=AMENDMENT z polem `amends` (ślad: co, kiedy, dlaczego).
  * NIE ma automatycznej promocji PROVISIONAL -> FINAL_ACCEPT. Obserwacja NIE jest
    walidacją (§4.2: dane z realnych sesji są adaptacyjnie recyklingowane), więc werdykt
    liczymy dynamicznie i tylko raportujemy — awans statusu udawałby mocniejszy dowód.
  * Werdykt „do wycofania" wymaga WARUNKU PODWÓJNEGO: wiek >= 7 dni ORAZ >= 15 sesji od
    przyjęcia. Sam wiek dawałby fałszywe alarmy na rzadko używanych skillach narzędziowych
    (sesje są PROXY okazji do użycia — nie samą okazją; patrz `cause` w wyniku).

Werdykty: żywy | do przeglądu | do wycofania | niemierzalny | w obserwacji.
"""
import argparse, collections, glob, json, os, sqlite3, sys, time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
try:
    from skill_telemetry import extract_names, TRACKED
except Exception:                                    # pragma: no cover
    TRACKED = ("skill_view", "skill_manage")

    def extract_names(a):
        return set()

HOME = os.path.realpath(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
LEDGER = os.path.join(HOME, "eval", "ledger.jsonl")
DB = os.path.join(HOME, "state.db")
RUNS = os.path.join(HOME, "eval", "runs")
OUT = os.path.join(HOME, "eval", "observation.json")

REQUIRED = ("ts", "artifact", "klasa", "werdykt", "status", "evaluator_version")
# `INFRA_ERROR` jest legalny: bramka ZAPISUJE przebiegi, w których rollout nie wystartował
# (awaria drogi do modelu). Bez tego każdy taki przebieg zaśmiecał audyt jako „nieznany status"
# (recenzja zewnętrzna 2026-11-09, §1.4).
ALLOWED_STATUS = {"PROVISIONAL", "FINAL_ACCEPT", "REJECT", "TIER_B_REQUIRED", "INFRA_ERROR",
                  "J_MUTATION", "AMENDMENT"}
DEBT_LIMIT = 2


def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def sessions_since(epoch):
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5.0)
        n = con.execute("SELECT COUNT(*) FROM sessions WHERE started_at > ?", (epoch,)).fetchone()[0]
        con.close()
        return n
    except Exception:
        return None


def skill_names(changed_files):
    """Z listy plików wyciąga nazwy skilli (katalog zawierający SKILL.md)."""
    out = []
    for f in changed_files or []:
        parts = str(f).split("/")
        if len(parts) >= 2 and parts[-1] == "SKILL.md":
            out.append(parts[-2])
    return out


def used_since(skill, epoch):
    """Czy skill był ZAŁADOWANY po przyjęciu zmiany (aktywacja, nie inwentarz)."""
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5.0)
        like = " OR ".join(f"tool_calls LIKE '%\"{t}\"%'" for t in TRACKED)
        rows = con.execute(f"SELECT timestamp, tool_calls FROM messages WHERE {like}").fetchall()
        con.close()
    except Exception:
        return None
    for ts, blob in rows:
        if not ts or ts <= epoch:
            continue
        try:
            calls = json.loads(blob or "[]")
        except Exception:
            continue
        for c in calls:
            fn = c.get("function") or {}
            if fn.get("name") not in TRACKED:
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            if skill in extract_names(args):
                return True
    return False


def panel_fail_count():
    """None = brak wyniku panelu w ostatnich runach, czyli stan NIEZNANY.
    Zwracanie 0 byłoby fałszywym „panel czysty": wystarczy pięć runów bez pola `summary`
    (test-sandbox, test-mutacja), żeby każdy sygnał FAIL zniknął z raportu.

    Okno liczymy WYŁĄCZNIE z runów panelu (`mode == "panel"`). Runy kontrolne
    (`test-sandbox`, `test-mutacja`) mają własne klucze i nie niosą `summary`, więc
    wliczanie ich do okna wypychało ostatni panel i zmieniało obserwację na „unknown"
    bez żadnej zmiany w bramce — szum od własnych testów, nie sygnał o stanie."""
    try:
        runs = sorted(glob.glob(os.path.join(RUNS, "*.json")))
        panel_runs = []
        for path in runs:
            # Runy kontrolne odsiewamy po ICH własnym znaczniku (klucz `test` albo nazwa
            # `test-*`), a nie po wymaganiu `mode == "panel"` — panel zapisany bez tego pola
            # (np. w testach regresyjnych) też jest wynikiem panelu i musi być liczony.
            if "test-" in os.path.basename(path):
                continue
            try:
                d = json.load(open(path, encoding="utf-8"))
            except Exception:
                continue
            if d.get("test"):
                continue
            panel_runs.append((path, d))
        for path, d in reversed(panel_runs[-5:]):
            s = d.get("summary") or d.get("incumbent_summary") or {}
            if isinstance(s, dict) and s.get("total"):
                return s.get("fail", 0)
    except Exception:
        return None
    return None


def threshold_drift(rows):
    """Zmiana hasha progów jest długiem TYLKO wtedy, gdy wpis, który ją niesie, nie jest
    sobą zmianą reguł gry. Wpis J_MUTATION/AMENDMENT *jest* tą zgodą — flagowanie go dawało
    dług-widmo na zatwierdzonej zmianie (audyt 2026-10-25: 1 fałszywy alarm, 0 realnych).
    Sprawdzamy status wpisu niosącego nowy hash, nie tylko różnicę wartości."""
    drift, prev = [], None
    for d in rows:
        h = d.get("thresholds_hash")
        if prev and h and prev != h and d.get("status") not in ("J_MUTATION", "AMENDMENT"):
            drift.append(f"{d.get('ts')}: {prev} -> {h} (wpis: {d.get('status')})")
        prev = h or prev
    return drift


def annulled_refs(rows):
    """Zbiory wpisów wyłączonych korektą (AMENDMENT z annuls=True).

    Wydzielone, bo tej samej wiedzy potrzebuje obserwacja ORAZ licznik długu.
    Wcześniej dług liczył surowe `dlug_regresji == "otwarty"`, więc **nie dało się go
    domknąć**: korekta anulowała wpis w obserwacji, ale dług zostawał otwarty na zawsze
    i raport wiecznie wołał o promocjach wstrzymanych bez powodu. Ta sama klasa defektu,
    co dryf-widmo z .8 — metryka nie zna własnego mechanizmu korekty.
    """
    annulled_ts = {((r.get("amends") or {}).get("ts"), (r.get("amends") or {}).get("artifact"))
                   for r in rows
                   if r.get("annuls") and isinstance(r.get("amends"), dict)
                   and (r.get("amends") or {}).get("ts")}
    annulled_name = {(r.get("amends") or {}).get("artifact") for r in rows
                     if r.get("annuls") and isinstance(r.get("amends"), dict)
                     and not (r.get("amends") or {}).get("ts")}
    return annulled_ts, annulled_name


def open_debt(rows):
    """Wpisy z otwartym długiem regresji, których nie zamknęła korekta."""
    annulled_ts, annulled_name = annulled_refs(rows)
    return [r for r in rows if r.get("dlug_regresji") == "otwarty"
            and (r.get("ts"), r.get("artifact")) not in annulled_ts
            and r.get("artifact") not in annulled_name]


def build(rows, min_age, min_sessions):
    amend, amend_by_artifact = {}, {}
    for r in rows:
        a = r.get("amends")
        if isinstance(a, dict):
            amend[(a.get("ts"), a.get("artifact"))] = r.get("changed_files")
            # fallback: dopasowanie po samej nazwie artefaktu (znacznik czasu bywa nieprecyzyjny)
            amend_by_artifact[a.get("artifact")] = r.get("changed_files")
    pfail = panel_fail_count()
    # WPISY ANULOWANE: korekta (AMENDMENT z annuls=True) wyłącza wpis z obserwacji bez
    # dotykania historii — tak samo, jak dokument korygujący w księgowości.
    # F10: korekta anuluje KONKRETNY wpis (ts + artifact), nie „wszystko o tej nazwie".
    # Dopasowanie po samej nazwie wyciszało także PRZYSZŁE wpisy o tej samej nazwie (np. kolejny
    # `skill-drift-<data>`), czyli powodowało cichy zanik sygnału — ta sama klasa co F9.
    # Fallback po nazwie zostaje wyłącznie dla korekt bez znacznika czasu.
    annulled_ts, annulled_name = annulled_refs(rows)
    obs, annulled_count = [], 0
    for r in rows:
        if r.get("status") != "PROVISIONAL":
            continue
        if (r.get("ts"), r.get("artifact")) in annulled_ts or r.get("artifact") in annulled_name:
            annulled_count += 1
            continue
        ts = parse_ts(r.get("ts") or "")
        epoch = ts.timestamp() if ts else None
        age = (time.time() - epoch) / 86400 if epoch else None
        changed = r.get("changed_files")
        if changed is None:
            changed = (amend.get((r.get("ts"), r.get("artifact")))
                       or amend_by_artifact.get(r.get("artifact")))
        skills = skill_names(changed)
        sess = sessions_since(epoch) if epoch else None
        activated = any(used_since(s, epoch) for s in skills) if skills else None

        if changed is None:
            v, cause = "niemierzalny", "brak changed_files (wpis sprzed pola i bez AMENDMENT)"
        elif not skills:
            v, cause = "niemierzalny", "zmiana nie dotyczy skilla — brak sygnału aktywacji"
        elif activated:
            if pfail is None:
                v, cause = ("niemierzalny",
                            "używany, ale brak wyniku panelu w runs/ — stan bramki nieznany")
            elif pfail:
                v, cause = "do przeglądu", f"używany, ale ostatni panel ma {pfail} FAIL"
            else:
                v, cause = "żywy", "używany po przyjęciu, panel czysty"
        elif age is None:
            v, cause = "niemierzalny", "nieparsowalny znacznik czasu"
        elif age < min_age:
            v, cause = "w obserwacji", f"za młody ({age:.2f} z {min_age:g} dni)"
        elif sess is not None and sess < min_sessions:
            v, cause = "w obserwacji", f"za mało sesji ({sess} z {min_sessions})"
        else:
            v, cause = "do wycofania", (f"{age:.1f} dni, {sess} sesji, zero aktywacji "
                                        f"{skills} — kandydat, nie automat")
        obs.append({"ts": r.get("ts"), "artifact": r.get("artifact"), "klasa": r.get("klasa"),
                    "changed_files": changed, "skills": skills,
                    "age_days": round(age, 2) if age else None, "sessions_since": sess,
                    "activated": activated, "verdict": v, "cause": cause})
    return obs, annulled_count


def main():
    global LEDGER, DB
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=LEDGER)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--min-age-days", type=float, default=7)
    ap.add_argument("--min-sessions", type=int, default=15)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    LEDGER, DB = a.ledger, a.db

    if not os.path.isfile(LEDGER):
        print("ledger pusty (brak pliku)")
        return 0

    rows, bad, status_count = [], [], collections.Counter()
    for i, line in enumerate(open(LEDGER, encoding="utf-8"), 1):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError as e:
            bad.append(f"linia {i}: nieparsowalny JSON ({e})")
            continue
        missing = [k for k in REQUIRED if k not in d]
        if missing:
            bad.append(f"linia {i}: brak pól {missing}")
        if d.get("status") not in ALLOWED_STATUS:
            bad.append(f"linia {i}: nieznany status {d.get('status')!r}")
        status_count[d.get("status")] += 1
        rows.append(d)

    obs, annulled = build(rows, a.min_age_days, a.min_sessions)
    # Dług liczymy TYLKO z wpisów nieanulowanych: korekta (AMENDMENT annuls) zamyka dług,
    # bo dokładnie po to istnieje. Bez tego metryka nie znała własnego mechanizmu korekty
    # i dług zostawał otwarty na zawsze.
    debt = open_debt(rows)
    drift = threshold_drift(rows)

    payload = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "wpisow": len(rows),
               "statusy": dict(status_count), "otwarty_dlug_regresji": len(debt),
               "obserwacja": obs, "wpisy_anulowane": annulled,
               "zmiana_progow_bez_J_MUTATION": drift, "bledy_schematu": bad}
    try:
        json.dump(payload, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except OSError:
        pass

    if a.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"wpisów: {len(rows)} · statusy: {dict(status_count)}")
        if annulled:
            print(f"wpisy anulowane korektą (AMENDMENT annuls): {annulled} — wyłączone z obserwacji")
        print(f"otwarty dług regresji: {len(debt)}"
              + (f" (> {DEBT_LIMIT} = wstrzymane promocje!)" if len(debt) > DEBT_LIMIT else ""))
        if obs:
            print("okno obserwacji (liczone na bieżąco; ledger pozostaje append-only):")
            for o in obs:
                print(f"  {o['verdict']:14s} | {o['artifact'][:36]:36s} | {o['cause']}")
        if drift:
            print("OSTRZEŻENIE: zmiana progów J bez wpisu J_MUTATION: " + "; ".join(drift))
        if bad:
            print("BŁĘDY SCHEMATU:")
            for b in bad:
                print("  ", b)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
