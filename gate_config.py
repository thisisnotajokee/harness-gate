#!/usr/bin/env python3
"""gate_config.py — jedno źródło prawdy o ustawieniach bramki (panel 2.0, F2).

DLACZEGO OSOBNY MODUŁ, A NIE STAŁE W CHECKACH
    Do 1.4.17 ścieżki i progi były rozsiane po 12 miejscach (`HERMES_HOME`, katalog kotwicy,
    `EVAL_VARIANTS`, limity w `GATE_PARAMS`…). Skutek zmierzony w F1: bramka działa u obcego
    użytkownika tylko wtedy, gdy jego dom wygląda jak nasz. Jedno miejsce audytu = jedno miejsce
    do sprawdzenia, czy reguły się nie zmieniły.

TRZY STANY PLIKU KONFIGURACJI (świadome, nie przypadkowe)
    * BRAK pliku            → wartości domyślne (R-UX2: pierwszy przebieg zielony bez konfiguracji).
    * plik POPRAWNY         → wartości domyślne zagnieżdżone pod wartościami użytkownika.
    * plik ZEPSUTY          → `ok: False` + powód. NIGDY ciche domyślne: literówka w konfiguracji
                              nie może wyglądać jak brak konfiguracji (fail-closed).

    Plik, gdy istnieje, jest objęty `panel.lock` — zmiana konfiguracji bez przezbrojenia manifestu
    jest wykrywana przez A-005 („reguły się zmieniły"), a nie przemilczana.

GDZIE MIESZKA PRAWDA (F2.3 — decyzja o jednym źródle na wartość)
    Limits (A-002/A-004/A-007), `threshold_sources`, kotwica, paczki, `tier_b` operacyjne
    i parytet A-008 → TU (plik w manifeście, zmiana bez przezbrojenia = A-005 fail).
    Parametry DECYZYJNE ewaluatora (`tier_b_m_min`, `tier_b_delta`, reguła werdyktu) → nadal
    `eval_version.GATE_PARAMS`, bo wchodzą do hasha progów i wymuszają wpis `J_MUTATION`.
    Duplikowanie ich tutaj tworzyłoby DRUGĄ PRAWDĘ: dwie liczby o tym samym znaczeniu, z których
    jedna nie ma żadnego czytelnika — dokładnie klasa długu, którą wykrywa A-007.

ZAKRES: rozwiązywanie ścieżek i parametrów. Ten moduł NIE wykonuje niczego poza odczytem
własnej konfiguracji (żadnych komend, żadnych zapisów) — dlatego może go importować każdy check,
także taki, który ma być audytowalny w całości.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os

CONFIG_NAME = "gate.config.json"
ENV_CONFIG = "HARNESS_GATE_CONFIG"

# Wartości domyślne = NASZ PROFIL PRODUKCYJNY. Prostota obsługi nie może oznaczać lżejszej bramki:
# brak konfiguracji daje te same progi, te same kryteria fail-closed i włączoną kotwicę.
DEFAULTS = {
    "schema": 1,
    "lang": "en",
    "hermes_home": "auto",
    "eval_dir": "auto",
    "anchor": {"mode": "in-place", "repo": "auto", "repo_path": "auto"},
    "packs": [],
    "tier_b": {"enabled": False, "model": "", "provider": "", "budget_s": 900, "repeats": 0,
               # F6: `gateway_ports` = porty, na których wolno wstać bramce rolloute (muszą być
               # wpuszczone przez piaskownicę — port losowy jest odrzucany); `proxy` = lokalny
               # pośrednik dla dostawców OAuth (nous/xai), gdzie klucza w `.env` po prostu nie ma.
               "gateway_ports": [], "proxy": {"url": "", "provider": "gate-proxy",
                                              "key_env": "GATE_PROXY_KEY"}},
    "limits": {"memory_char": 2500, "user_char": 1500,
               "a004_max_warnings": 150, "a007_max_warnings": 0},
    "threshold_sources": [],
    # Ścieżki konwencji HOSTA (względne wobec `hermes_home`) — używane przez kontrole paczek
    # (A-003: mapa flat-KB, B-004: liczenie notatek z drzewa). U kogo innego `kb` może się
    # nazywać inaczej; zmiana = plik w manifeście, więc nie jest to przemilczane.
    "paths": {"kb": "kb"},
    "cli": {"bin": "hermes",
            "required_flags": ["--in", "-z", "--yolo", "-m", "--provider"],
            "version_max": ""},
    # A-008 to mechanizm („kotwica odzwierciedla źródło"), a zbiór parytetu jest lokalny (W4).
    # Domyślnie WYŁĄCZONY: u obcego nie ma mirrora, więc brak parytetu musi być jawnym `N/A`
    # (P4/Q2), a nie fałszywym `fail` ani cichym `skip`.
    "parity": {"enabled": False, "repo": "auto",
               "files": ["SOUL.md", "memories/MEMORY.md", "memories/USER.md", "kb/_INDEX.md",
                         "kb/core/*.md", "kb/incidents/*.md"],
               "clean_prefixes": ["skills/", "kb/", "scripts/", "memories/", "agent-hooks/",
                                  "SOUL.md"]},
    # Sondy fail-closed rolloutu: ścieżki, których piaskownica NIE dostaje w `--rw`.
    # `<hermes_home>` = DOM MIERZONY, rozwiązywany przy `resolve()`. Zaszyte `~/.hermes` było
    # błędem użytkownika z niestandardowym `HERMES_HOME`: chroniło CUDZY dom, a własny zostawiało
    # bez osłony (naprawa 2026-11-09 — patrz docs/INSTALL.md „Nietypowy dom").
    "probes_forbidden": ["<hermes_home>", "~/.ssh"],
}

ANCHOR_MODES = ("in-place", "external", "none")
_KNOWN_TOP = set(DEFAULTS)
_KNOWN_NESTED = {k: set(v) for k, v in DEFAULTS.items() if isinstance(v, dict)}


# ------------------------------------------------------------------ odczyt
def config_path(base=None) -> str:
    """Ścieżka pliku konfiguracji: `HARNESS_GATE_CONFIG` → `<base>/gate.config.json`."""
    env = os.environ.get(ENV_CONFIG, "").strip()
    if env:
        return os.path.realpath(os.path.expanduser(env))
    return os.path.join(base or os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME)


def load(base=None) -> dict:
    """Wczytaj konfigurację. Zwraca zawsze dict: {ok, error, source, present, cfg, warnings, hash}."""
    path = config_path(base)
    out = {"ok": True, "error": "", "source": path, "present": os.path.isfile(path),
           "cfg": copy.deepcopy(DEFAULTS), "warnings": [], "hash": ""}
    if not out["present"]:
        out["source"] = "domyślne (brak pliku)"
        out["hash"] = effective_hash(out["cfg"])
        return out
    try:
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        out.update(ok=False, error=f"{CONFIG_NAME} nieczytelny albo niepoprawny JSON: "
                                   f"{type(e).__name__}: {e}")
        return out
    if not isinstance(user, dict):
        out.update(ok=False, error=f"{CONFIG_NAME}: korzeń musi być obiektem JSON")
        return out

    err = _validate(user)
    if err:
        out.update(ok=False, error=err)
        return out
    for key in user:
        if key not in _KNOWN_TOP:
            out["warnings"].append(f"nieznany klucz konfiguracji: {key} (zignorowany)")
            continue
        # Klucz nieznany wewnątrz znanej sekcji też musi być GŁOŚNY: `tier_b.m_min` wygląda
        # jak pokrętło, a nim nie jest (parametr decyzyjny mieszka w hashu progów) — cicha
        # ignorancja uczy, że konfiguracja działa, gdy nie działa.
        if key in _KNOWN_NESTED and isinstance(user[key], dict):
            for sub in user[key]:
                if sub not in _KNOWN_NESTED[key]:
                    out["warnings"].append(f"nieznany klucz konfiguracji: {key}.{sub} "
                                           f"(zignorowany)")
    out["cfg"] = _merge(copy.deepcopy(DEFAULTS), user)
    out["hash"] = effective_hash(out["cfg"])
    return out


def _validate(user: dict) -> str:
    """Fail-closed na typach i trybach. Zwraca komunikat błędu albo pusty string."""
    schema = user.get("schema", 1)
    if isinstance(schema, bool) or schema != 1:
        return f"{CONFIG_NAME}: nieobsługiwana wersja schematu {schema!r} (obsługiwana: 1)"
    anchor = user.get("anchor")
    if anchor is not None:
        if not isinstance(anchor, dict):
            return f"{CONFIG_NAME}: `anchor` musi być obiektem"
        mode = anchor.get("mode", DEFAULTS["anchor"]["mode"])
        if mode not in ANCHOR_MODES:
            return (f"{CONFIG_NAME}: `anchor.mode` = {mode!r} nieobsługiwany "
                    f"(dozwolone: {', '.join(ANCHOR_MODES)})")
    for key, types, label in (("packs", (list,), "lista nazw paczek"),
                              ("threshold_sources", (list,), "lista plików progów"),
                              ("probes_forbidden", (list,), "lista ścieżek sond"),
                              ("lang", (str,), "kod języka"),
                              ("hermes_home", (str,), "ścieżka albo \"auto\""),
                              ("eval_dir", (str,), "ścieżka albo \"auto\"")):
        if key in user and not isinstance(user[key], types):
            return f"{CONFIG_NAME}: `{key}` musi być {label}"
    for key in ("tier_b", "limits", "cli", "parity", "anchor", "paths"):
        if key in user and not isinstance(user[key], dict):
            return f"{CONFIG_NAME}: `{key}` musi być obiektem"
    for key in _KNOWN_NESTED["limits"]:
        val = (user.get("limits") or {}).get(key)
        if val is not None and (isinstance(val, bool) or not isinstance(val, int)):
            return f"{CONFIG_NAME}: `limits.{key}` musi być liczbą całkowitą"
    tb = user.get("tier_b") or {}
    for flag in ("enabled",):
        if flag in tb and not isinstance(tb[flag], bool):
            return f"{CONFIG_NAME}: `tier_b.{flag}` musi być true/false"
    parity = user.get("parity") or {}
    if "enabled" in parity and not isinstance(parity["enabled"], bool):
        return f"{CONFIG_NAME}: `parity.enabled` musi być true/false"
    # F3: zły typ nie może wyjść jako traceback z `int(...)`/pętli po stringu. Każdy klucz
    # o deklarowanym typie jest sprawdzany tutaj — fail-closed ma być tanie, a diagnoza czytelna.
    for key, types in (("budget_s", (int,)), ("repeats", (int,))):
        val = tb.get(key)
        if val is not None and (isinstance(val, bool) or not isinstance(val, types)):
            return f"{CONFIG_NAME}: `tier_b.{key}` musi być liczbą całkowitą"
    # F6: `gateway_ports` (porty bramki rolloute — piaskownica musi je wpuścić) i `proxy`
    # (lokalny pośrednik dla dostawców OAuth). Literówka tutaj = rollout bez portu = cicha
    # awaria drogi do modelu, więc typy są sprawdzane jak reszta.
    gw = tb.get("gateway_ports")
    if gw is not None:
        if not isinstance(gw, list) or any(isinstance(p, bool) or not isinstance(p, (int, str)) for p in gw):
            return f"{CONFIG_NAME}: `tier_b.gateway_ports` musi być listą numerów portów"
    proxy = tb.get("proxy")
    if proxy is not None:
        if not isinstance(proxy, dict):
            return f"{CONFIG_NAME}: `tier_b.proxy` musi być obiektem"
        for key in ("url", "provider", "key_env"):
            val = proxy.get(key)
            if val is not None and not isinstance(val, str):
                return f"{CONFIG_NAME}: `tier_b.proxy.{key}` musi być tekstem"
        if proxy.get("url") and not str(proxy["url"]).startswith(("http://", "https://")):
            return f"{CONFIG_NAME}: `tier_b.proxy.url` musi zaczynać się od http:// lub https://"
    for key in ("required_flags",):
        val = (user.get("cli") or {}).get(key)
        if val is not None and not isinstance(val, list):
            return f"{CONFIG_NAME}: `cli.{key}` musi być listą"
    for key in ("files", "clean_prefixes"):
        val = parity.get(key)
        if val is not None and not isinstance(val, list):
            return f"{CONFIG_NAME}: `parity.{key}` musi być listą"
    for key in ("repo", "repo_path"):
        val = (user.get("anchor") or {}).get(key)
        if val is not None and not isinstance(val, str):
            return f"{CONFIG_NAME}: `anchor.{key}` musi być tekstem"
    return ""


def _merge(base: dict, user: dict) -> dict:
    """Zagnieżdżone scalanie: wartości użytkownika wygrywają, brakujące klucze z domyślnych."""
    for key, val in user.items():
        if key not in base and key not in _KNOWN_TOP:
            base[key] = val                     # nieznany klucz zostaje widoczny w hashu
            continue
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], val)
        else:
            base[key] = val
    return base


def effective_hash(cfg: dict) -> str:
    """Hash efektywnej konfiguracji (16 znaków) — do manifestu i do werdyktu."""
    blob = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ rozwiązanie ścieżek
def resolve(res: dict, base=None) -> dict:
    """Zamień `"auto"` na konkret. Nie rzuca wyjątków: brak kotwicy opisujemy, nie ukrywamy."""
    base = os.path.realpath(base or os.path.dirname(os.path.abspath(__file__)))
    cfg = res.get("cfg") or copy.deepcopy(DEFAULTS)

    home = cfg.get("hermes_home", "auto")
    if not home or home == "auto":
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    home = os.path.realpath(os.path.expanduser(home))

    eval_dir = cfg.get("eval_dir", "auto")
    eval_dir = os.path.join(home, "eval") if not eval_dir or eval_dir == "auto" \
        else os.path.realpath(os.path.expanduser(eval_dir))

    anchor = cfg.get("anchor") or {}
    mode = anchor.get("mode", "in-place")
    repo = (anchor.get("repo") or "auto").strip()
    repo_path = (anchor.get("repo_path") or "auto").strip()
    if mode == "in-place":
        repo = base if repo == "auto" else os.path.realpath(os.path.expanduser(repo))
        repo_path = "panel.lock" if repo_path == "auto" else repo_path
    elif mode == "external":
        repo = "" if repo == "auto" else os.path.realpath(os.path.expanduser(repo))
        repo_path = "scripts/harness_eval/panel.lock" if repo_path == "auto" else repo_path
    else:                                     # none
        repo, repo_path = "", ""

    cli = cfg.get("cli") or {}
    return {
        "base": base, "hermes_home": home, "eval_dir": eval_dir,
        "gate_dir": os.path.join(home, "scripts", "harness_eval"),
        "variants_dir": os.path.join(eval_dir, "variants"),
        "anchor_mode": mode, "anchor_repo": repo, "anchor_repo_path": repo_path,
        "cli_bin": (cli.get("bin") or "hermes"),
        "cli_flags": list(cli.get("required_flags") or []),
        # Sondy piaskownicy: `<hermes_home>` znaczy „dom, który MIERZYMY" — jedno źródło wartości,
        # więc przeniesienie domu nie zostawia osłony na starej ścieżce.
        "probes_forbidden": [
            home if p == "<hermes_home>" else os.path.realpath(os.path.expanduser(p))
            for p in (cfg.get("probes_forbidden") or DEFAULTS["probes_forbidden"])],
        "packs": list(cfg.get("packs") or []),
        "lang": cfg.get("lang") or "en",
    }


def parity_repo(paths: dict, parity: dict) -> str:
    """Kotwica parytetu: jawna ścieżka albo rodzeństwo domu Hermesa (katalog `anchor`)."""
    repo = (parity.get("repo") or "auto").strip()
    if not repo or repo == "auto":
        return os.path.realpath(os.path.join(paths["hermes_home"], os.pardir, "anchor"))
    return os.path.realpath(os.path.expanduser(repo))


def resolve_bin(binname: str) -> str:
    """Binarka Hermesa: `PATH` → `~/.local/bin` → nazwa jak podana (błąd pokaże sandbox)."""
    import shutil as _sh
    if os.sep in binname:
        return os.path.expanduser(binname)
    found = _sh.which(binname)
    if found:
        return found
    local = os.path.expanduser(os.path.join("~", ".local", "bin", binname))
    return local if os.path.isfile(local) else binname


# ------------------------------------------------------------------ parametry dla checków (F2.3)
def for_checks(base=None) -> dict:
    """Parametry reguł dla CHECKÓW — zawsze z drzewa BRAMKI, nigdy z drzewa mierzonego.

    To jest granica, której pilnuje A-002 od .11: strażnik nie może czytać własnej poprzeczki
    od kandydata. Konfiguracja bramki leży w manifeście (`panel.lock`), więc jej zmiana bez
    przezbrojenia locka to `fail` A-005 — inaczej niż plik kandydata.

    Zwraca {ok, error, source, hash, limits, parity, threshold_sources, cli, tier_b, lang, paths}.
    `ok: False` znaczy „reguł nie dało się odczytać" — check MUSI wtedy zwrócić `fail`,
    nie wartości domyślne.
    """
    res = load(base)
    if not res["ok"]:
        return {"ok": False, "error": res["error"], "source": res["source"], "hash": ""}
    cfg = res["cfg"]
    paths = resolve(res, base)
    return {"ok": True, "error": "", "source": res["source"], "hash": res["hash"],
            "limits": dict(cfg["limits"]), "parity": copy.deepcopy(cfg["parity"]),
            "threshold_sources": list(cfg["threshold_sources"]),
            "cli": copy.deepcopy(cfg["cli"]), "tier_b": copy.deepcopy(cfg["tier_b"]),
            # Sondy z ROZWIĄZANYCH ścieżek: surowa konfiguracja niesie `<hermes_home>`, czyli
            # placeholder, a nie ścieżkę (patrz `resolve()`).
            "probes_forbidden": list(paths["probes_forbidden"]),
            # `layout` = WZGLĘDNE nazwy katalogów konwencji hosta (`paths.kb`) — check musi je
            # złożyć z drzewem MIERZONYM (`--home`/`--variant`), a nie z domem bramki.
            "layout": dict(cfg.get("paths") or {}),
            "lang": cfg["lang"], "paths": paths}


# ------------------------------------------------------------------ kotwica: TRZY STANY (W1)
ANCHOR_STATES = {
    "ANCHOR_NONE": "kotwica wyłączona jawnym wpisem konfiguracji (anchor.mode = \"none\")",
    "ANCHOR_MISSING": "brak repozytorium kotwicy — bramka nie ma z czym porównać integralności",
    "ANCHOR_UNREADABLE": "kotwica istnieje, ale nie da się jej odczytać (git albo uprawnienia)",
    "ANCHOR_EMPTY": "repozytorium kotwicy istnieje, ale nie ma jeszcze commita",
    "ANCHOR_OK": "kotwica gotowa",
}


def anchor_status(paths: dict) -> dict:
    """Stan kotwicy rozpoznany PO NAZWIE, nie po treści komunikatu git-a (W1: brak ≠ manipulacja).

    Zwraca {code, state, summary, hint}. `code` jest tym, co widzi użytkownik; `TAMPERING`
    zastrzeżone jest wyłącznie dla rozjazdu treści (to rozstrzyga A-005, nie ten moduł).
    """
    import shutil as _sh
    import subprocess as _sp

    mode, repo = paths.get("anchor_mode"), paths.get("anchor_repo")
    if mode == "none":
        return {"code": "ANCHOR_NONE", "state": "off", "hint": "",
                "summary": ANCHOR_STATES["ANCHOR_NONE"]}
    hint = 'uruchom `harness-gate --fix` (albo `install.sh --init-anchor`)'
    if not repo:
        return {"code": "ANCHOR_MISSING", "state": "missing", "hint": hint,
                "summary": f"{ANCHOR_STATES['ANCHOR_MISSING']}: tryb `{mode}` bez ścieżki repo"}
    if not os.path.isdir(os.path.join(repo, ".git")):
        return {"code": "ANCHOR_MISSING", "state": "missing", "hint": hint,
                "summary": f"{ANCHOR_STATES['ANCHOR_MISSING']}: {repo} nie jest repozytorium git"}
    if not _sh.which("git"):
        return {"code": "ANCHOR_UNREADABLE", "state": "unreadable", "hint": "zainstaluj git",
                "summary": f"{ANCHOR_STATES['ANCHOR_UNREADABLE']}: brak komendy `git` w PATH"}
    p = _sp.run(["git", "-C", repo, "rev-parse", "--verify", "HEAD"],
                capture_output=True, text=True)
    if p.returncode != 0:
        return {"code": "ANCHOR_EMPTY", "state": "empty", "hint": hint,
                "summary": f"{ANCHOR_STATES['ANCHOR_EMPTY']}: {repo}"}
    return {"code": "ANCHOR_OK", "state": "ok", "hint": "", "summary": f"kotwica: {repo}"}


if __name__ == "__main__":                    # diagnostyka: `python3 gate_config.py`
    res = load()
    paths = resolve(res)
    print(json.dumps({"ok": res["ok"], "error": res["error"], "source": res["source"],
                      "present": res["present"], "hash": res["hash"],
                      "warnings": res["warnings"], "paths": paths,
                      "anchor": anchor_status(paths)}, ensure_ascii=False, indent=1))
