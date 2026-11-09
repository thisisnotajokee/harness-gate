#!/usr/bin/env python3
"""A-007: spójność modelu i dostawcy — jedno źródło prawdy, zero cichych pinów.

Po co: 2026-10-27 job „Monitor pamięci" padał 13 razy przez 10 dni na `HTTP 402`, bo wybór
modelu był **skopiowany w ośmiu miejscach** o różnych nazwach i formatach
(`config.yaml: model.default`, `delegation.model`, `cron.model`, piny w `cron/jobs.json`,
`DEFAULT_MODEL` w `.env`, domyślne `EVAL_MODEL`/`EVAL_PROVIDER` w runnerze). Każda kopia
starzała się osobno, a awaria była widoczna tylko na tej jednej ścieżce, która akurat działała.

Ten check jest tani (0 tokenów) i patrzy na to, co da się zweryfikować statycznie:

  * ŹRÓDŁO PRAWDY: `config.yaml → model.provider` + `model.default`.
  * W1 — nazwa modelu bez przedrostka dostawcy (`deepseek-v4-flash` vs `deepseek/…`): ten sam
    model u dwóch dostawców to dwa różne konta, więc identyfikator musi być jednoznaczny.
  * W2 — pin wskazujący innego dostawcę niż główny (delegacja, cron, pin joba, domyślne w runnerze).
  * W3 — martwy duplikat wyboru modelu (`DEFAULT_MODEL` w `.env`; nic go nie czyta, a myli).

Brak/nieczytelność `config.yaml` albo brak `model.provider` = `fail` (nie da się ustalić prawdy,
a „nie wiem" nie znaczy „czysto"). Próg w `gate.config.json → limits.a007_max_warnings` (od .21) —
plik konfiguracji bramki objęty manifestem, więc zmiana progu bez przezbrojenia locka to `fail`
A-005; wcześniej klucz siedział w `GATE_PARAMS` (usunięty, żeby nie było dwóch prawd o progu).
"""
import argparse
import json
import os
import re
import sys



# Korzeń bramki: checki mieszkają w `checks/core/` albo `packs/<n>/checks/`, więc szukamy
# katalogu z `gate_config.py`, zamiast liczyć poziomy (liczba poziomów różni się).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
for _ in range(5):
    if os.path.isfile(os.path.join(_ROOT, "gate_config.py")):
        break
    _ROOT = os.path.dirname(_ROOT)
sys.path.insert(0, _ROOT)

def read(path):
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def gate_max_warnings():
    """(próg, błąd) — próg czytany z konfiguracji BRAMKI, nigdy z drzewa mierzonego."""
    try:
        import gate_config
        cfg = gate_config.for_checks()
    except Exception as e:
        return None, f"nie mogę wczytać konfiguracji bramki: {type(e).__name__}: {e}"
    if not cfg["ok"]:
        return None, cfg["error"]
    return int(cfg["limits"]["a007_max_warnings"]), ""


def _section(txt, name):
    """Blok top-level `name:` (linie z wcięciem) — bez YAML-a, żeby nie zależeć od bibliotek."""
    m = re.search(rf"(?m)^{re.escape(name)}:\s*\n((?:[ \t]+.*\n|\s*\n)*)", txt)
    return m.group(1) if m else ""


def _one(block, key):
    m = re.search(rf"(?m)^[ \t]+{re.escape(key)}:\s*([^\s#]+)", block)
    return m.group(1).strip().strip("\"'") if m else ""


def main_config(home):
    """(provider, model, błędy) — źródło prawdy."""
    txt = read(os.path.join(home, "config.yaml"))
    if not txt:
        return "", "", ["config.yaml nieodczytywalny — nie ma jak ustalić aktywnego dostawcy"]
    block = _section(txt, "model")
    prov, mod = _one(block, "provider"), _one(block, "default")
    errs = []
    if not prov:
        errs.append("config.yaml: brak `model.provider` (źródło prawdy nieokreślone)")
    if not mod:
        errs.append("config.yaml: brak `model.default`")
    return prov, mod, errs


def collect_pins(home):
    """Wszystkie miejsca, które trzymają własną kopię wyboru modelu/dostawcy.

    Zwraca listę dictów: {zrodlo, model, provider}. Piny celowo puste ('' po unset)
    pomijamy — dziedziczenie z configu jest zachowaniem POPRAWNYM, nie pinem.
    """
    pins = []
    txt = read(os.path.join(home, "config.yaml"))
    for sec in ("delegation", "cron", "fallback_model"):
        block = _section(txt, sec)
        if not block:
            continue
        m, p = _one(block, "model"), _one(block, "provider") or _one(block, "model_provider")
        if m or p:
            pins.append({"zrodlo": f"config.yaml:{sec}", "model": m, "provider": p})
    env_txt = read(os.path.join(home, ".env"))
    m = re.search(r"(?m)^DEFAULT_MODEL=(.+)$", env_txt)
    if m and m.group(1).strip().strip("\"'"):
        pins.append({"zrodlo": ".env:DEFAULT_MODEL", "model": m.group(1).strip().strip("\"'"),
                     "provider": "", "stale": True})
    try:
        jobs = json.loads(read(os.path.join(home, "cron", "jobs.json")) or "{}")
    except Exception:
        jobs = {}
    jobs = jobs.get("jobs", jobs) if isinstance(jobs, dict) else jobs
    for j in (jobs or []):
        if isinstance(j, dict) and (j.get("model") or j.get("provider")):
            pins.append({"zrodlo": f"cron/jobs.json:{j.get('name') or j.get('id')}",
                         "model": j.get("model") or "", "provider": j.get("provider") or ""})
    runner = read(os.path.join(home, "scripts", "harness_eval", "tier_b.py"))
    m = re.search(r"""EVAL_MODEL\s*=\s*os\.environ\.get\([^,]+,\s*["']([^"']+)["']""", runner)
    p = re.search(r"""EVAL_PROVIDER\s*=\s*os\.environ\.get\([^,]+,\s*["']([^"']+)["']""", runner)
    if m or p:
        pins.append({"zrodlo": "harness_eval/tier_b.py (domyślne rolloutu)",
                     "model": m.group(1) if m else "", "provider": p.group(1) if p else ""})
    return pins


def inspect(home):
    """(warnings, errors) — czysta funkcja, testowalna na fiksturze."""
    main_prov, main_mod, errors = main_config(home)
    warnings = list(errors)
    if main_prov and main_mod and "/" not in main_mod:
        warnings.append(f"config.yaml:model.default '{main_mod}' bez przedrostka dostawcy "
                        f"(ten sam model u dwóch dostawców = dwa konta)")
    for pin in collect_pins(home):
        src = pin["zrodlo"]
        if pin.get("stale"):
            warnings.append(f"{src}: martwy duplikat wyboru modelu ('{pin['model']}') — "
                            f"nic go nie czyta, a myli przy zmianie dostawcy")
            continue
        if pin.get("provider") and main_prov and pin["provider"] != main_prov:
            warnings.append(f"{src}: pin wskazuje dostawcę '{pin['provider']}', a główny to "
                            f"'{main_prov}' — przy zmianie dostawcy ta ścieżka zostanie na starym")
        if pin.get("model") and "/" not in pin["model"]:
            warnings.append(f"{src}: nazwa modelu '{pin['model']}' bez przedrostka dostawcy")
    return warnings, errors


def emit(status, summary, details=None):
    print(json.dumps({"check": "A-007-provider-coherence", "status": status, "summary": summary,
                      "details": details or []}, ensure_ascii=False))
    return 1 if status == "fail" else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=os.path.expanduser("~/.hermes"))
    ap.add_argument("--variant", default="")
    a = ap.parse_args()
    home = a.variant or a.home

    max_warnings, cfg_err = gate_max_warnings()
    if max_warnings is None:
        return emit("fail", f"reguł bramki nie da się odczytać: {cfg_err}")

    if not os.path.isdir(home):
        return emit("fail", f"brak katalogu {home} — nic nie sprawdzono")

    warnings, errors = inspect(home)
    main_prov, main_mod, _ = main_config(home)
    if errors:
        return emit("fail", f"{len(errors)} błąd(-y) źródła prawdy: {errors[0][:120]}", warnings[:20])
    if len(warnings) > max_warnings:
        return emit("fail", f"rozjazd wyboru modelu: {len(warnings)} ostrzeżeń > próg {max_warnings}",
                    warnings[:20])
    return emit("pass", f"model '{main_mod}' przez '{main_prov}' — jedno źródło prawdy, "
                        f"{len(warnings)} ostrzeżeń (próg {max_warnings})", warnings[:20])


if __name__ == "__main__":
    sys.exit(main())
