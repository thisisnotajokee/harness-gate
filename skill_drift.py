#!/usr/bin/env python3
"""skill_drift.py — wykrywa zmiany w skills/ wprowadzone POZA bramką.

Po co: produkt ma własną pętlę self-ewolucji (agent/background_review.py), która po sesji
dopisuje lekcje do skilli przez `skill_manage`. Ma guardy prewencyjne (brak kasowania,
read-before-write, whitelist narzędzi), ale NIE zostawia śladu przyjęcia. Ten skrypt
dostarcza brakującą warstwę: ślad + okno obserwacji.

Decyzja 2026-10-15 (Piotrek): tryb pracy review zostaje bez zmian (pisze wprost) — my
śledzimy jego efekt, zamiast dokładać drugą warstwę prewencji. Nie ocenia, czy lekcja jest
mądra; sprawia, że zmiana jest widoczna, ma provenance i wchodzi w okno obserwacji.

Użycie:
  skill_drift.py --update              # ustaw/odśwież punkt odniesienia (baseline)
  skill_drift.py --check [--log]       # wykryj drift; --log dopisuje wpis do ledgera
"""
import argparse, hashlib, json, os, shutil, sys, time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
HOME = os.path.realpath(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
SKILLS = os.path.join(HOME, "skills")
MANIFEST = os.path.join(HOME, "eval", "skills-manifest.json")
LEDGER = os.path.join(HOME, "eval", "ledger.jsonl")

# Katalogi pomijane: cache huba, backupy kuratora, archiwum (przenosiny robi kurator)
SKIP_DIRS = {".hub", ".curator_backups", "__pycache__", ".git", "node_modules", "assets",
             ".venv", "venv", "cache"}


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def split_covered(paths, rows, now=None):
    """(residua, pokrycie) — jednolita decyzja dla zapisu i dla testu.

    Wydzielone, bo w `main` ta logika była nieprzetestowalna: test mógł sprawdzić najwyżej, czy
    plik zniknął z ledgera, a nie to, KTÓRE pliki i DLACZEGO zostały wyłączone (wzorzec z punktu 9).
    """
    cover = covered_by_accepted(paths, rows, now=now)
    return [p for p in paths if p not in cover], cover


def collect(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and d != ".archive"]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                out[os.path.relpath(p, root)] = sha(p)
            except OSError:
                pass
    return out


# Statusy, które SĄ śladem przyjęcia (albo świadomą korektą) — tylko one mogą "pokryć" dryft.
ACCEPTED_STATUSES = ("PROVISIONAL", "J_MUTATION", "AMENDMENT")
COVERAGE_WINDOW_H = 24.0


def load_rows(path):
    """Wpisy ledgera jako lista dictów; uszkodzone linie pomijamy (nie wywracamy raportu)."""
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        pass
    return rows


def _ts_epoch(value):
    if not isinstance(value, str):
        return None
    try:
        return time.mktime(time.strptime(value.strip(), "%Y-%m-%dT%H:%M:%S%z"))
    except Exception:
        return None


def covered_by_accepted(paths, rows, within_hours=COVERAGE_WINDOW_H, now=None):
    """Które ścieżki są już udokumentowane wpisem PRZYJĘTYM — i przez który artefakt.

    Po co: promocja 96 plików przez bramkę trafia do ledgera jako `PROVISIONAL` z pełną listą
    `changed_files`, a `skill_drift` raportuje te same pliki drugi raz jako `DRIFT_UNREVIEWED`.
    Okno obserwacji dostawało wtedy dwa wiersze o jednej zmianie, a raport dzienny „do przeglądu"
    bez powodu — duplikat to dług, nie ostrożność.

    Okno czasowe jest KONIECZNE: bez niego akceptacja sprzed miesiąca maskowałaby każdą późniejszą
    zmianę tego samego pliku, czyli powstałaby cicha dziura w oknie obserwacji (klasa F9).
    `now` istnieje wyłącznie po to, żeby dało się to przetestować na syntetycznych wierszach.
    """
    cut = (now if now is not None else time.time()) - within_hours * 3600
    want, cover = set(paths), {}
    for r in rows:
        if r.get("status") not in ACCEPTED_STATUSES:
            continue
        ts = _ts_epoch(r.get("ts"))
        if ts is None or ts < cut:
            continue
        for f in (r.get("changed_files") or []):
            if f in want and f not in cover:
                cover[f] = r.get("artifact")
    return cover


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="zapisz punkt odniesienia")
    ap.add_argument("--check", action="store_true", help="porównaj z punktem odniesienia")
    ap.add_argument("--log", action="store_true", help="dopisz drift do ledgera bramki")
    ap.add_argument("--skills", default=SKILLS)
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--ledger", default=LEDGER)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    if not (a.update or a.check):
        a.check = True

    current = collect(a.skills)
    old = {}
    if os.path.isfile(a.manifest):
        try:
            old = json.load(open(a.manifest, encoding="utf-8")).get("files", {})
        except Exception:
            old = {}

    added = sorted(set(current) - set(old))
    removed = sorted(set(old) - set(current))
    changed = sorted(k for k in set(current) & set(old) if current[k] != old[k])

    if a.check:
        if not old:
            print("DRIFT: brak punktu odniesienia — pierwszy przebieg, ustaw baseline (--update)")
        elif added or removed or changed:
            print(f"DRIFT WYKRYTY: nowe {len(added)}, zmienione {len(changed)}, usunięte {len(removed)}")
            for label, items in (("nowe", added), ("zmienione", changed), ("usunięte", removed)):
                for it in items[:8]:
                    print(f"   [{label}] {it}")
            if len(added) + len(changed) + len(removed) > 24:
                print(f"   … i {len(added) + len(changed) + len(removed) - 24} więcej")
        else:
            print("DRIFT: brak zmian w skills/ od punktu odniesienia")

    if a.log and (added or removed or changed):
        drifted = [os.path.join("skills", x) for x in added + changed + removed]
        # Dedupe wobec wpisów PRZYJĘTYCH: promocja przez bramkę jest już w ledgerze z pełną listą
        # plików, więc raportowanie tych samych plików drugi raz jako DRIFT_UNREVIEWED tworzyło
        # podwójny wiersz obserwacji o jednej zmianie (i „do przeglądu" bez powodu).
        residue, cover = split_covered(drifted, load_rows(a.ledger))
        if not residue:
            if not a.quiet:
                arts = ", ".join(sorted({str(v) for v in cover.values()}))
                print(f"   DRIFT pokryty wpisem przyjętym ({arts}) — bez duplikatu w ledgerze "
                      f"(plików: {len(drifted)}; punkt odniesienia odświeża się poniżej)")
        else:
            if cover and not a.quiet:
                # Stdout jest wstrzykiwany do promptu raportu, więc musi mówić to samo co ledger:
                # inaczej agent raportuje „2 zmienione", a w oknie obserwacji jest jeden wiersz.
                print(f"   pokryte wpisem przyjętym (NIE wchodzą do ledgera, {len(cover)}): "
                      f"{', '.join(sorted(cover))[:160]}")
            entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 # F10b: nazwa z godziną — sama data kolidowała z wcześniejszym wpisem o tej samej
                 # nazwie, a korekta (annuls) mogła wyciszyć cały dzień driftu.
                 "artifact": "skill-drift-" + time.strftime("%Y-%m-%dT%H:%M"),
                 "klasa": "1A",
                 "changed_files": residue[:60],
                 "change_summary": (f"zmiana POZA bramką: nowe {len(added)}, zmienione {len(changed)}, "
                                    f"usunięte {len(removed)}"
                                    + (f"; {len(cover)} plików już pokrytych wpisem przyjętym"
                                       if cover else "")),
                 "trigger_episode": None,
                 "panel_summary": {"added": added[:20], "changed": changed[:20], "removed": removed[:20]},
                 "werdykt": "DRIFT_UNREVIEWED",
                 "uzasadnienie": ("wykryte przez skill_drift (najpewniej background review) — wciągnięte do "
                                  "okna obserwacji; treść zmian nieoceniona, ślad dla człowieka"),
                 "status": "PROVISIONAL", "dlug_regresji": None,
                 "source": "skill_drift"}
            if cover:
                # Nota, nie osobny werdykt: okno obserwacji ma wiedzieć, że część plików jest już
                # udokumentowana, ale nie może dostać drugiego wiersza o tej samej zmianie.
                entry["covered_by"] = sorted({str(v) for v in cover.values()})
            try:
                import harness_eval as he
                entry.update(he.eval_version_line())
                # Zapis WŁASNY, nie he.append_ledger(): ta funkcja pisze do stałej ścieżki
                # i ignorowała --ledger, przez co wpis z testu wpadł do produkcyjnego ledgera
                # (2026-10-15, naprawione korektą AMENDMENT, nie edycją historii).
                os.makedirs(os.path.dirname(a.ledger), exist_ok=True)
                with open(a.ledger, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                if not a.quiet:
                    print(f"   → wpis w ledgerze: {a.ledger} ({entry['artifact']})")
            except Exception as e:
                print(f"   ! nie udało się zapisać wpisu: {type(e).__name__}: {e}", file=sys.stderr)

    if a.update or (a.log and (added or removed or changed)):
        os.makedirs(os.path.dirname(a.manifest), exist_ok=True)
        json.dump({"generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "files": current},
                  open(a.manifest, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        if not a.quiet:
            print(f"   punkt odniesienia: {len(current)} plików → {a.manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
