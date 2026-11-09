#!/usr/bin/env python3
"""tier_b.py — rollout Tier B w sandboxie Landlock, parowane kandydat vs incumbent.

Każda noga działa na WŁASNEJ kopii harnessu (nigdy na produkcji), z HOME wskazującym
na scratch, więc zapisy lądują w piaskownicy. WRITE do produkcji jest zablokowany przez
kernel, a nie przez dobrą wolę agenta.

Uczciwe granice: to izolacja INTEGRALNOŚCI, nie poufności — rollout czyta pliki
(auth.json, .env, ~/.ssh) i ma dostęp do sieci na porcie 443 (API modelu).
"""
import json, math, os, subprocess, sys, time, tempfile
from evidence import snapshot, trace_since, last_message

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import gate_config                                    # reguły z drzewa bramki (F2.3)

HOME = os.path.expanduser("~")
# Ścieżki i reguły pochodzą z `gate.config.json` BRAMKI (nie z `$HOME` ani z drzewa mierzonego):
# bez tego rollout u obcego szukał naszego domu i naszych sond.
_CFG = gate_config.for_checks(BASE)
_PATHS = _CFG["paths"] if _CFG.get("ok") else gate_config.resolve({}, BASE)
_TIER_B_CFG = _CFG.get("tier_b") or {}
HERMES_HOME = _PATHS["hermes_home"]
EVAL = _PATHS["eval_dir"]
SCRATCH = os.path.join(EVAL, "scratch")
SANDBOX = os.path.join(BASE, "sandbox_landlock.py")
# Kolejność: jawna zmienna (jednorazowy eksperyment) > konfiguracja (`cli.bin`) > `~/.local/bin`.
HERMES_BIN = (os.environ.get("EVAL_HERMES_BIN")
              or gate_config.resolve_bin((_CFG.get("cli") or {}).get("bin") or "hermes"))
# Model pinowany JAWNIE: obie nogi (incumbent/kandydat) muszą używać tego samego,
# dostępnego modelu — inaczej porównanie mierzy różnicę modeli, nie harnessu.
# Domyślnie bierzemy model z `config.yaml` (JEDNO źródło prawdy), a nie literał z kodu:
# zaszyte "deepseek"/"deepseek-v4-flash" trzymało rolloute na kluczu z `.env` także po zmianie
# dostawcy — a nieudany rollout (rc≠0) wyglądał potem jak werdykt o kandydacie.
def _main_model():
    """(model, provider) z config.yaml — patrz A-007 (spójność wyboru modelu)."""
    try:
        with open(os.path.join(HERMES_HOME, "config.yaml"), encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return "", ""
    import re as _re
    # `[ \t]*#.*\n` w alternatywie: komentarz w kolumnie 0 wewnątrz bloku `model:` nie może
    # urywać parsowania — inaczej pola poniżej są niewidoczne dla A-007 (recenzja 2026-11-09, §5.4).
    block = _re.search(r"(?m)^model:\s*\n((?:[ \t]+.*\n|[ \t]*#.*\n|\s*\n)*)", txt)
    block = block.group(1) if block else ""
    one = lambda k: (_re.search(rf"(?m)^[ \t]+{k}:\s*([^\s#]+)", block) or [None, ""])[1].strip().strip("\"'")
    return one("default"), one("provider")


_MAIN_MODEL, _MAIN_PROVIDER = _main_model()
# Kolejność źródeł (F2.3): jawna zmienna (jednorazowy eksperyment) > `tier_b.{model,provider}`
# z konfiguracji BRAMKI > `config.yaml` (nasze jedno źródło prawdy). Konfiguracja jest w tym
# łańcuchu konieczna: bez niej rollout u obcego nie miałby skąd wziąć modelu, a literał w kodzie
# trzymałby go na dostawcy, którego użytkownik może wcale nie używać (patrz A-007).
EVAL_MODEL = os.environ.get("EVAL_MODEL") or _TIER_B_CFG.get("model") or _MAIN_MODEL
EVAL_PROVIDER = os.environ.get("EVAL_PROVIDER") or _TIER_B_CFG.get("provider") or _MAIN_PROVIDER

# Porty sieci przyznawane rolloutowi. Zawsze 443 (API dostawcy/modelu przez proxy) — patrz F6:
# Landlock przyznaje porty GLOBALNIE (per port, nie per adres), więc każde rozszerzenie poszerza
# zasięg sieci rolloutu o dowolny host na tym porcie. Świadoma decyzja, nie domyślne zachowanie.
# `tier_b.gateway_ports` dochodzi z KONFIGURACJI (F6): współczesny `hermes -z` w obcym
# `HERMES_HOME` dźwiga bramkę, która MUSI zbindować port — bez tego każdy rollout pada na
# `Gateway draining: not_ready` i wygląda jak awaria kandydata.
EXTRA_NET_PORTS = [p for p in os.environ.get("EVAL_ALLOW_NET_PORTS", "").replace(" ", "").split(",") if p.isdigit()]
GATEWAY_PORTS = [str(p) for p in (_TIER_B_CFG.get("gateway_ports") or []) if str(p).isdigit()]
# Lokalny pośrednik (`hermes proxy`): dostawca OAuth (nous/xai) nie ma zmiennej z kluczem, więc
# poświadczenie MUSI zostać po stronie proxy; przez granicę piaskownicy przechodzi tylko port.
_PROXY = _TIER_B_CFG.get("proxy") or {}
PROXY_URL = str(_PROXY.get("url") or "").strip()
PROXY_PROVIDER = str(_PROXY.get("provider") or "gate-proxy").strip()
PROXY_KEY_ENV = str(_PROXY.get("key_env") or "GATE_PROXY_KEY").strip()


def _port_from_url(url):
    """Port z adresu pośrednika — jedno źródło wartości (adres), nie druga lista do rozjechania."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        return str(urlparse(url).port or (443 if url.startswith("https://") else 80))
    except Exception:
        return ""


PROXY_PORTS = [p for p in [_port_from_url(PROXY_URL)] if p]

# Dodatkowe nazwy zmiennych środowiskowych wstrzykiwanych do piaskownicy (np. wskazanie
# lokalnego pośrednika). Nadal TYLKO nazwy z tej listy — nie cały `.env`.
EXTRA_ENV_KEYS = tuple(k.strip() for k in os.environ.get("EVAL_ENV_KEYS", "").split(",") if k.strip())


def _host_checkout():
    """Katalog checkoutu Hermesa — źródło TABELI dostawców (nie nasza lista w kodzie)."""
    cands = [os.path.join(HERMES_HOME, "hermes-agent"),
             os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(HERMES_BIN))), "hermes-agent"),
             os.path.expanduser("~/.hermes/hermes-agent")]
    for c in cands:
        if os.path.isdir(os.path.join(c, "hermes_cli")):
            return c
    return ""


def provider_credentials(provider):
    """(zmienne_klucza, zmienna_base_url, znany) — z tabeli dostawców HERMESA.

    Bramka nie zgaduje kształtu poświadczenia: `PROVIDER_REGISTRY` w `hermes_cli.auth` deklaruje
    dla dostawcy kluczowego jego zmienne (np. `deepseek` -> `DEEPSEEK_API_KEY`, `huggingface` ->
    `HF_TOKEN`), a dostawca OAuth (nous, xai) nie deklaruje ŻADNEJ — i wtedy jedyną drogą jest
    `hermes proxy`. Zaszyta para `DEEPSEEK_*` działała u nas i psuła każdego innego użytkownika
    (F6, 2026-11-09). Gdy tabeli nie da się odczytać, zwracamy `znany=False` i ZGADUJEMY —
    ale wołający musi to pokazać w komunikacie, nie udawać wiedzy.
    """
    prov = (provider or "").strip()
    if not prov:
        return (), "", False
    root = _host_checkout()
    if root:
        try:
            if root not in sys.path:
                sys.path.insert(0, root)
            from hermes_cli import auth as _auth
            cfg = getattr(_auth, "PROVIDER_REGISTRY", {}).get(prov)
            if cfg is not None:
                return (tuple(getattr(cfg, "api_key_env_vars", ()) or ()),
                        getattr(cfg, "base_url_env_var", "") or "", True)
        except Exception:
            pass
    return (prov.upper().replace("-", "_") + "_API_KEY",), "", False


_PROVIDER_ENVS, _PROVIDER_BASE_ENV, PROVIDER_KNOWN = provider_credentials(EVAL_PROVIDER)


def probes_forbidden():
    """Ścieżki sond (fail-closed) — z konfiguracji BRAMKI; jawna zmienna ma pierwszeństwo (F2.3).

    Sondy wskazują prawdziwy dom produkcyjny. Gdy `HOME` jest przekierowany (np. smoke na kopii
    drzewa), domyślne sondy trafiłyby do drzewa przyznanego jako `--rw` i sandbox słusznie
    odmówiłby startu — dlatego ścieżki są JAWNE i pochodzą z konfiguracji, a nie z `$HOME`.
    """
    raw = os.environ.get("EVAL_FORBIDDEN_PROBES")
    items = raw.split(",") if raw else (_CFG.get("probes_forbidden") or [])
    return [os.path.expanduser(p.strip()) for p in items if p and p.strip()]


def load_tier_b_tasks():
    """Zadania Tier B z panelu RAZEM z paczkami — jedno źródło dla runnera, panelu i baterii.

    Do 2026-11-09 ta funkcja czytała wyłącznie `panel.json`, więc zadania Tier B z paczek
    (`packs/<n>/panel.d.json`) NIE BYŁY uruchamiane ani razu, choć panel je deklarował:
    `harness_eval.tier_b_tasks()` (przez `load_panel`) widział 7, runner widział 5 — dwie
    odpowiedzi na jedno pytanie. Zadania paczki mają własne checki i sondy (B-003 → `trace_probe`,
    B-004 → `state_probe`), więc rozbieżność trzymała je MARTWE, a nie „nieaktywne".
    """
    from harness_eval import load_panel       # import lokalny (harness_eval importuje tier_b tak samo)
    return [t for t in load_panel()["tasks"] if t.get("tier") == "B"]


# DEFAULT_MODEL usunięty 2026-10-27: martwa zmienna (nikt jej nie czyta), a wstrzykiwana
# do piaskownicy myliła wybór modelu w rolloucie (patrz A-007).
# Do piaskownicy wchodzą WYŁĄCZNIE poświadczenia, których TEN dostawca naprawdę wymaga
# (z tabeli Hermesa) + jawnie wskazane nazwy. Nie cały `.env`, nigdy `auth.json`.
ENV_WHITELIST = (tuple(_PROVIDER_ENVS)
                 + ((_PROVIDER_BASE_ENV,) if _PROVIDER_BASE_ENV else ())
                 + ((PROXY_KEY_ENV,) if PROXY_URL else ())
                 + EXTRA_ENV_KEYS)


def model_env(env_path=None):
    """Tylko klucz modelu przechodzi do piaskownicy — nie cały .env.
    Wariant NIE trzyma sekretów (auth.json też nie jest kopiowany): świadomie
    minimalizujemy to, co rollout może odczytać."""
    keys = {}
    p = env_path or os.path.join(HERMES_HOME, ".env")
    try:
        for line in open(p, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() in ENV_WHITELIST:
                keys[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    # Klucze nieobecne w `.env` (np. wskazanie lokalnego pośrednika) mogą przyjść ze
    # środowiska procesu nadrzędnego — plik ma pierwszeństwo, więc produkcyjny `.env`
    # nie da się przypadkiem nadpisać eksportem w shellu.
    for k in ENV_WHITELIST:
        if k not in keys and os.environ.get(k):
            keys[k] = os.environ[k]
    # Pośrednik: poświadczenie zostaje PO STRONIE proxy (sesja OAuth w `auth.json`), a rollout
    # dostaje wyłącznie token-atrapę — `hermes proxy` przyjmuje dowolny bearer. Dzięki temu sekret
    # NIE przekracza granicy piaskownicy, a dostawca OAuth (nous) staje się w ogóle używalny.
    if PROXY_URL and not keys.get(PROXY_KEY_ENV):
        keys[PROXY_KEY_ENV] = "gate-proxy-local"
    return keys


def prepare_variant_config(variant_root, gateway_port=""):
    """Wpina w `config.yaml` wariantu to, co MUSI mieć infrastruktura rolloutu (idempotentnie).

    Nie dotykamy tego, co porównujemy (SOUL/skills/pamięci). Dwie rzeczy:
    * `webapp.enabled/port` — bramka wstaje na PRZYPIĘTYM porcie, więc piaskownica wpuszcza
      dokładnie ten jeden; bez tego binduje port losowy i `hermes -z` pada na `not_ready` (F6).
    * `providers.<proxy>` — pole na lokalnego pośrednika, gdy poświadczenie jest OAuth.
    Gdy wariant ma WŁASNY blok `providers:`, nie doklejamy po cichu (zły YAML = cudze poświadczenie):
    zwracamy `False` i wołający ma odmówić z komunikatem.
    """
    path = os.path.join(variant_root, "config.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            body = f.read()
    except OSError:
        # Brak `config.yaml` to nie nasza sprawa: nie mamy czego wpinać, a rollout sam to zgłosi.
        # (Wcześniej zwracaliśmy tu odmowę — i wywracało to testy dowodowe, których fixture
        # wariantu nie ma configu, mimo że niczego nie trzeba było wstrzykiwać.)
        return True
    # Blok na POCZĄTKU pliku nie ma przed sobą "\n" — dlatego sprawdzamy oba warianty.
    def _blok(nazwa):
        return f"\n{nazwa}" in body or body.lstrip().startswith(f"{nazwa}")
    ma_nasze = _blok(f"  {PROXY_PROVIDER}:")
    if PROXY_URL and _blok("providers:") and not ma_nasze:
        return False                     # cudzy blok `providers:` — nie scalamy YAML-a po cichu
    import re as _re
    nowy = body
    if gateway_port and _blok("webapp:"):
        # Port bramki MUSI zgadzać się z tym, co wpuściła piaskownica — a bywa, że poprzedni
        # przebieg wpisał inny numer i zostawił go na dysku. Podmieniamy, nie doklejamy.
        # `[ \t]*` (nie `\s*`) na końcu: inaczej wyrażenie zjada kończący znak nowej linii
        # i „idempotentny" zapis jednak zmienia plik.
        nowy = _re.sub(r"(?m)^([ \t]*port:[ \t]*)\d+[ \t]*$",
                       lambda m: m.group(1) + str(gateway_port), nowy, count=1)
    add = ""
    if gateway_port and not _blok("webapp:"):
        add += f"\nwebapp:\n  enabled: true\n  host: 127.0.0.1\n  port: {gateway_port}\n"
    if PROXY_URL and not ma_nasze:
        add += (f"\nproviders:\n  {PROXY_PROVIDER}:\n    base_url: {PROXY_URL}\n"
                f"    key_env: {PROXY_KEY_ENV}\n")
    if not add and nowy == body:
        return True
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(nowy + add)
    except OSError:
        return False
    return True


TRANSPORT_MARKERS = (
    "http 401", "http 402", "http 403", "http 429", "http 500", "http 502", "http 503", "http 504",
    "insufficient balance", "billing", "entitlement", "rate limit", "quota",
    "no usable credentials", "request was not processed", "connectionerror", "connection reset",
    "connection refused", "temporary failure in name resolution", "ssl", "tls",
    "read timed out", "no local fallback", "gateway connection failed", "gateway draining",
)


def transport_error(out, err):
    """Marker czkawki DROGI DO MODELU w wyjściu rolloutu, albo "" (recenzja 2026-11-09, §1.1–1.3).

    Po co: `rollout_started()` rozstrzyga tylko „czy cokolwiek się stało". Gdy w pierwszej turze
    model zdąży wywołać narzędzie (wiersz w `state.db`), a druga tura padnie na 402/429/502/zerwaniu
    TLS — rollout „wystartował", więc bramka liczyła to jako `fail` KANDYDATA (twarda przegrana),
    a nie jako awarię kabli. Odwrotnie też bywało: kandydat, który wpada w pętlę bez wywołania
    narzędzia, dostawał `infra_error`, choć to jego regresja.
    Lista jest CELOWO wąska (sformułowania błędów, nie słowa z zadań): „timeout" bez kontekstu
    występuje w promptach zadań, więc go tu nie ma.
    """
    for tekst in (err or "", out or ""):
        niski = tekst[-4000:].lower()
        for marker in TRANSPORT_MARKERS:
            if marker in niski:
                return marker
    return ""


def free_gateway_port(prefer="inc"):
    """Pierwszy WOLNY port bramki (albo "" gdy żaden) — i posprzątaj po poprzednim rolloucie.

    Port bramki musi być przypięty (piaskownica wpuszcza tylko wymienione), więc dwie nogi
    dzielą dwa numery — a zabłąkana bramka z poprzedniego przebiegu (`--idle-exit` gaśnie
    dopiero po czasie) potrafi zablokować nogę. Wtedy rollout pada w 3 sekundy na bindzie,
    a komunikat mówi o `not_ready` — czyli znowu atrapa awarii kandydata (F6).
    """
    import socket
    ports = GATEWAY_PORTS
    if not ports:
        return ""
    if prefer.startswith("inc"):
        kolejnosc = ports
    else:
        kolejnosc = ports[1:] + ports[:1]
    for port in kolejnosc:
        s = socket.socket()
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", int(port)))
            return str(port)
        except OSError:
            continue
        finally:
            s.close()
    return ""


def stop_variant_gateway(variant_root):
    """Dobija bramkę, którą zostawił rollout tego wariantu (best-effort, tylko ta bramka).

    `gateway.lock` w wariancie trzyma pid procesu; bramka ma `--idle-exit`, ale gaśnie dopiero
    po okresie bezczynności i przez ten czas trzyma przypięty port następnej nogi.
    """
    lock = os.path.join(variant_root, "gateway.lock")
    try:
        with open(lock, encoding="utf-8") as f:
            pid = int(json.load(f).get("pid") or 0)
    except (OSError, ValueError, TypeError):
        return False
    if pid <= 1 or pid == os.getpid():
        return False
    try:
        env = open(f"/proc/{pid}/environ", "rb").read().decode("utf-8", "replace")
    except OSError:
        return False
    # Zabijamy WYŁĄCZNIE bramkę tego wariantu — nigdy bramki produkcyjnej.
    if f"HERMES_HOME={variant_root}" not in env:
        return False
    try:
        os.kill(pid, 15)
        return True
    except OSError:
        return False


def probe_enabled(task):
    """Czy zadanie mierzy zachowanie przez ślad narzędzi? `evidence_probe` = snapshot katalogu
    dowodów + ślad (B-006), `trace_probe` = sam ślad (B-001: nie ma katalogu `evidence/`,
    a `snapshot()` słusznie nie pracuje bez punktu odniesienia)."""
    return bool(task.get('evidence_probe') or task.get('trace_probe'))


def probe_first_id(task, db):
    """Punkt odcięcia śladu. MUSI być liczony dla OBU trybów: zwrócenie `0` sprawia, że
    `trace_since(db, 0)` oddaje CAŁĄ historię wariantu, więc stare wywołanie z poprzedniego
    rolloutu zaliczałoby kryterium „wykonał". Wyciągnięte w funkcję, bo w `run_task` ta linia
    jest nieprzetestowalna, a błąd jest cichy — objawia się tylko przepuszczaniem złego."""
    return last_message(db) if probe_enabled(task) else 0


def state_probe_enabled(task):
    """Trzeci tryb pomiaru: STAN, którego prawda musi zostać zamrożona PRZED rollem (B-004).

    Osobny predykat, bo `state_probe` to NIE `evidence_probe`/`trace_probe` (punkt 28 lekcji):
    nie zbiera śladu i nie robi snapshotu katalogu dowodów, natomiast wymaga dowodu stanu
    sprzed rolloutu. Gdyby wpiąć go do `probe_enabled`, zadanie stanowe zaczęłoby czytać
    `state.db` wariantu i mieszać dwie różne miary (proces vs stan) w jednym werdykcie.
    """
    return bool(task.get('state_probe'))


def state_snapshot(task, variant_root, base=None):
    """Zamraża stan drzewa wariantu jako (state, error) — PRZED rollem.

    Uruchamiamy PRZYPIĘTY check zadania w trybie `--state-only`, zamiast liczyć u siebie:
    jedna implementacja prawdy (check jest w `panel.lock`), ta sama funkcja, którą potem
    woła werdykt. Przepisanie logiki liczenia w runnerze to klasa defektu z B-001 —
    prawda rozjeżdża się z materiałem i nikt tego nie widzi.
    """
    check = os.path.join(base or BASE, task["check"])
    try:
        p = subprocess.run([sys.executable, check, "--variant", variant_root, "--state-only"],
                           capture_output=True, text=True, timeout=120)
        d = json.loads((p.stdout or "{}").strip().splitlines()[-1] or "{}")
    except Exception as e:
        return None, f"pomiar stanu nie wystartował: {type(e).__name__}"
    if d.get("status") != "pass" or not isinstance(d.get("state"), dict):
        return None, str(d.get("error") or d.get("summary") or "brak stanu w odpowiedzi checka")
    return d["state"], None


def rollout_started(root, variant_root, rows_before, task):
    """(started, ślad_rows, artefakty) — czy rollout w ogóle wystartował?

    Lekcja z 2026-10-27: gdy dostawca modelu był martwy (HTTP 402), `run_task` zwracał `fail`
    z summary typu „brak pliku stan.json" — czyli awaria infrastruktury wyglądała jak werdykt
    o kandydacie. Przy regule `losses == 0` taka „przegrana" odrzuca dobre zmiany, a dwie nieudane
    nogi dają remis, który też kończy się REJECT.    Sygnały niewystartowania są tanie i twarde:
    brak nowego śladu w `state.db` wariantu ORAZ brak jakiegokolwiek artefaktu poza seedem.

    UWAGA (złapane na realnym przebiegu): punkt odcięcia MUSI być liczbą WIERSZY PRZED rollem
    policzoną dla KAŻDEGO trybu. Liczony tylko dla sond (`probe_first_id`) dawał dla zadań
    `state_probe` zero, a `trace_since(db, 0)` oddaje CAŁĄ historię wariantu — czyli „ślad
    istnieje" właśnie wtedy, gdy rollout nie wystartował. Ta sama klasa co defekt z punktu 28.
    """
    db = os.path.join(variant_root, "state.db")
    try:
        rows = trace_since(db, rows_before)
    except Exception:
        rows = []
    seeded = set((task.get("seed") or {}).keys())
    produced = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            if rel in seeded or rel.startswith("home" + os.sep) or rel.startswith("tmp" + os.sep):
                continue
            produced.append(rel)
    return bool(rows) or bool(produced), rows, produced


def run_task(task, variant_root, label, timeout=420):
    """Jeden rollout + check artefaktów. Zwraca wynik w formacie checka."""
    os.makedirs(SCRATCH, exist_ok=True)
    root = tempfile.mkdtemp(prefix=f"{task['id']}-{label}-", dir=SCRATCH)
    fakehome = os.path.join(root, "home")
    tmpdir = os.path.join(root, "tmp")
    os.makedirs(fakehome, exist_ok=True)
    os.makedirs(tmpdir, exist_ok=True)
    # Pliki startowe zadania (`seed` z panel.json) — bez nich nie da się sprawdzić dyscypliny
    # zakresu (B-007): check musi znać stan PRZED rollouem. Ścieżki względne, bez wyjścia z roota.
    for rel, content in (task.get("seed") or {}).items():
        if os.path.isabs(rel) or ".." in rel.split("/"):
            raise ValueError(f"seed poza katalogiem roboczym: {rel!r}")
        sp = os.path.join(root, rel)
        os.makedirs(os.path.dirname(sp) or root, exist_ok=True)
        with open(sp, "w", encoding="utf-8") as f:
            f.write(content)
    # Baseline stays in parent memory and outside every --rw grant.
    # `probe` obejmuje oba tryby: snapshot katalogu dowodów (evidence_probe) i sam ślad (trace_probe).
    # Punkt odcięcia MUSI być policzony w obu — inaczej `trace_since(db, 0)` zwracał CAŁĄ historię
    # wariantu i stare wywołanie z poprzedniego rolloutu zaliczałoby kryterium (klasa: „stary ślad
    # wystarcza zamiast nowego").
    baseline = snapshot(os.path.join(root, 'evidence')) if task.get('evidence_probe') else None
    db = os.path.join(variant_root, 'state.db')
    first_id = probe_first_id(task, db)
    # Odcięcie dla klasyfikacji „czy rollout wystartował" — dla WSZYSTKICH trybów (patrz rollout_started).
    try:
        rows_before = last_message(db)
    except Exception:
        rows_before = first_id
    # `state_probe` (B-004): stan mierzonego drzewa zamrażamy TERAZ — przed rollem i w rodzicu,
    # czyli poza każdym `--rw` piaskownicy. Prawda liczona po rolloucie pozwalała agentowi
    # dorzucić plik i „potwierdzić" nową liczbę; brak drzewa dawał zaś fałszywe „0, 0" i pass.
    state_path = None
    if state_probe_enabled(task):
        snap_state, snap_error = state_snapshot(task, variant_root)
        if snap_error:
            # Stanu sprzed rolloutu nie ma, więc zadanie stanowe NIE MA czego porównać — a jego
            # pomiar kosztuje tokeny. Przerywamy jako awarię infrastruktury, zamiast płacić za
            # pomiar, który i tak musi fail-closed (recenzja 2026-11-09, §5.6).
            return {"status": "infra_error", "task": task["id"], "variant": label, "rc": -1, "wall_s": 0.0,
                    "scratch": root, "details": [f"snapshot błędu: {snap_error}"],
                    "summary": (f"INFRA: nie zamroziłem stanu sprzed rolloutu ({snap_error}) — "
                                f"zadanie stanowe nie ma punktu odniesienia, nie wydaję tokenów"),
                    "stdout_tail": "", "stderr_tail": ""}
        evidence_dir = os.path.join(EVAL, 'evidence')
        os.makedirs(evidence_dir, exist_ok=True)
        fd, state_path = tempfile.mkstemp(suffix='.state.json', dir=evidence_dir)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'state': snap_state, 'error': snap_error, 'variant': variant_root,
                       'taken_at': time.strftime('%Y-%m-%dT%H:%M:%S')}, f, ensure_ascii=False)
    # Bramka wariantu MUSI wstać na porcie, który piaskownica wpuści (F6): port losowy = bind
    # odrzucony = `Gateway draining: not_ready`. Port dobieramy per noga, żeby równoległe
    # rolloute się nie biły o ten sam numer.
    gw_port = ""
    if GATEWAY_PORTS:
        # Najpierw sprzątamy po poprzednim rolloucie (bramka `--idle-exit` trzyma przypięty port),
        # potem bierzemy pierwszy WOLNY z listy. Bez tego druga noga dostaje bind odmówiony
        # i pada w kilka sekund komunikatem o `not_ready` — czyli znów atrapa awarii kandydata.
        stop_variant_gateway(variant_root)
        gw_port = free_gateway_port(label)
        if not gw_port:
            return {"status": "infra_error", "task": task["id"], "variant": label, "rc": -1, "wall_s": 0.0,
                    "scratch": root, "details": [],
                    "summary": (f"INFRA: żaden port z `tier_b.gateway_ports` ({GATEWAY_PORTS}) nie jest "
                                f"wolny — bramka rolloute nie ma się gdzie zbindować"),
                    "stdout_tail": "", "stderr_tail": ""}
    if not prepare_variant_config(variant_root, gw_port):
        return {"status": "infra_error", "task": task["id"], "variant": label, "rc": -1, "wall_s": 0.0,
                "scratch": root, "details": [],
                "summary": ("INFRA: nie przygotowałem `config.yaml` wariantu (własny blok `providers:` "
                            "albo brak zapisu) — patrz tier_b.prepare_variant_config"),
                "stdout_tail": "", "stderr_tail": ""}
    env = dict(os.environ)
    env["HERMES_HOME"] = variant_root
    env["HOME"] = fakehome
    # TMPDIR MUSI wskazywać w piaskownicę: systemowy TMPDIR (~/.hermes/cache/scratch)
    # jest poza --rw, a zablokowany zapis tymczasowy przewraca rollout w środku pracy.
    env["TMPDIR"] = tmpdir
    keys = model_env()
    env.update(keys)
    shared = os.path.join(EVAL, "variants", ".shared")
    rw_extra = ["--rw", shared] if os.path.isdir(shared) else []
    # Sondy fail-closed: JAWNE ścieżki z konfiguracji bramki — szczegóły w `probes_forbidden()`.
    probes = probes_forbidden()
    # Dostawca rolloutu: przy pośredniku idziemy przez POLE (`tier_b.proxy`), nie przez dostawcę
    # użytkownika — poświadczenie zostaje po stronie proxy.
    prow = PROXY_PROVIDER if PROXY_URL else EVAL_PROVIDER
    cmd = [sys.executable, SANDBOX, "--rw", variant_root, "--rw", root] + rw_extra + [
           *[x for p in ["443"] + EXTRA_NET_PORTS + GATEWAY_PORTS + PROXY_PORTS for x in ("--allow-net-port", p)],
           "--keep-env", "HERMES_HOME",
           *[x for k in keys for x in ("--keep-env", k)],
           *[x for p in probes for x in ("--forbidden-probe", p)],
           "--quiet", "--", HERMES_BIN, "--in", root,
           "-m", EVAL_MODEL, "--provider", prow,
           "-z", task["prompt"], "--yolo"]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        rc, out, err = 124, (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), "timeout"
    wall = round(time.time() - t0, 1)

    # Sprzątanie PRZED checkiem, nie po: bramka wariantu (dźwigana przez sam rollout, `--idle-exit`)
    # żyje jeszcze kilka sekund i zadanie czytające /proc (B-002) widziało ją jako „prawdę"
    # — czyli mierzyło NASZĄ infrastrukturę, a nie stan hosta. To m.in. stąd brała się chwiejność
    # B-002 (recenzja 2026-11-09, §2.4 i §4.1).
    stop_variant_gateway(variant_root)
    check = os.path.join(BASE, task["check"])
    evidence_path = None
    if probe_enabled(task):
        # `evidence_probe` = snapshot katalogu dowodów + ślad (B-006). `trace_probe` = sam ślad,
        # bez snapshotu: B-001 nie ma katalogu `evidence/`, a `snapshot()` słusznie odmawia
        # pracy bez niepustego punktu odniesienia — więc dla zadań mierzących WYKONANIE
        # (czy skrypt naprawdę uruchomiono) wystarczy ślad, bez wymuszania struktury katalogów.
        ev = {'baseline': baseline, 'calls': [], 'trace_error': None}
        try:
            # `with_results=True` od panelu 1.4.13: dowód nieleksykalny (wynik wywołania), nie tylko
            # napisana komenda. Bez tego check widzi „model wspomniał nazwę skryptu", a nie
            # „skrypt coś wypisał" — a właśnie tę lukę wykorzystywał B-001 (`cat` = „uruchomienie").
            ev['calls'] = trace_since(db, first_id, with_results=True)
        except Exception as e:
            # Wyniki to osobny kanał dowodu: gdy baza ich nie dostarczy, NIE gubimy całego śladu
            # (B-006 mierzy zachowanie na argumentach), ale fakt zapisujemy jawnie — zadanie,
            # dla którego wynik jest kryterium (B-001), ma wtedy paść fail-closed, a nie po cichu
            # zmierzyć mniej, niż deklaruje.
            ev['results_error'] = type(e).__name__
            try:
                ev['calls'] = trace_since(db, first_id)
            except Exception as e2:
                ev['trace_error'] = type(e2).__name__
        evidence_dir = os.path.join(EVAL, 'evidence')
        os.makedirs(evidence_dir, exist_ok=True)
        fd, evidence_path = tempfile.mkstemp(suffix='.json', dir=evidence_dir)
        with os.fdopen(fd, 'w') as f:
            json.dump(ev, f, ensure_ascii=False)
    try:
        c = subprocess.run(check_argv(task, root, variant_root,
                                      evidence_path=evidence_path or "",
                                      state_path=state_path or ""),
                           capture_output=True, text=True, timeout=120)
        res = json.loads((c.stdout or "{}").strip().splitlines()[-1] or "{}")
        if not isinstance(res, dict) or res.get('status') not in ('pass', 'fail'):
            raise ValueError('invalid checker contract')
        if c.returncode != 0 or rc != 0:
            # Kontrakt checka (status↔rc) wymaga faila, ale NIE gubimy przyczyny:
            # nadpisanie summary komunikatem kontraktu ukrywało, CO się stało
            # (kosztowało ręczne odtwarzanie werdyktu przy diagnozie).
            res['summary'] = (res.get('summary') or '') + \
                f" [kontrakt: rollout/check nonzero: {rc}/{c.returncode}]"
            res['status'] = 'fail'
    except Exception as e:
        res = {"status": "fail", "summary": f"check artefaktów nie zadziałał: {type(e).__name__}",
               "details": []}
    # Awaria infrastruktury ≠ werdykt o kandydacie: rozdzielamy je PRZED parowaniem.
    # Trzy rozłączne przypadki (recenzja 2026-11-09, §1.1–1.3), w tej kolejności:
    #   (a) w wyjściu jest marker awarii DROGI DO MODELU (także przy `rc == 0`!) — infra,
    #   (b) rc≠0 i nic nie powstało (brak śladu i artefaktów) — infra,
    #   (c) reszta — werdykt checka (kandydat realnie coś zrobił albo realnie zawiódł).
    transport = transport_error(out, err)
    if transport:
        res["status"] = "infra_error"
        res["details"] = ([f"rc={rc}", f"wall_s={wall}", f"marker transportu: {transport!r}"]
                          + list(res.get("details") or [])[:4])
        res["summary"] = (f"INFRA: awaria drogi do modelu ({transport!r}) — nie jest to wynik kandydata; "
                          f"pierwotny powód: {(res.get('summary') or '')[:120]}")
    elif rc != 0:
        started, trace_rows, produced = rollout_started(root, variant_root, rows_before, task)
        if not started:
            res["status"] = "infra_error"
            res["details"] = ([f"rc={rc}", f"wall_s={wall}", "ślad w state.db: 0 wierszy",
                               "artefakty poza seedem: 0"]
                              + list(res.get("details") or [])[:4])
            res["summary"] = (f"INFRA: rollout nie wystartował (rc={rc}, {wall}s, brak śladu i artefaktów) — "
                              f"to awaria drogi do modelu, nie wynik kandydata; pierwotny powód: "
                              f"{(res.get('summary') or '')[:120]}")
    # Sprzątanie po rolloucie: bramka wariantu ma `--idle-exit`, ale gaśnie dopiero po okresie
    # bezczynności — przez ten czas trzyma przypięty port, który jest potrzebny następnej nodze.
    stop_variant_gateway(variant_root)
    res.update({"task": task["id"], "variant": label, "rc": rc, "wall_s": wall,
                "scratch": root, "stdout_tail": (out or "")[-400:], "stderr_tail": (err or "")[-300:]})
    return res


def proxy_health(url=None, timeout=5.0):
    """Czy pośrednik (`hermes proxy`) NAPRAWDĘ odpowiada? Zwraca `(ok, powód)`.

    Po co: 2026-11-09 spaliłem ~25 minut rolloutów, bo `hermes proxy` (startowany z `timeout`)
    wygasł, a rollout dostawał od modelu „Your request was not processed" i ponawiał próby przez
    ~5 minut na nogę. Objaw wygląda jak awaria dostawcy, a jest brakiem jednego procesu.
    Sprawdzenie kosztuje sekundę i oszczędza cały budżet.
    """
    url = (url if url is not None else PROXY_URL).strip()
    if not url:
        return True, ""                      # brak pośrednika = nie ma czego pilnować
    import urllib.request
    baza = url.rstrip("/")
    ostatni = "brak odpowiedzi"
    for sciezka in dict.fromkeys([baza + "/models", baza.rstrip("/v1") + "/v1/models"]):
        try:
            req = urllib.request.Request(sciezka, headers={"Authorization": "Bearer gate-proxy-local"})
            with urllib.request.urlopen(req, timeout=timeout) as odp:
                if 200 <= getattr(odp, "status", 0) < 300:
                    return True, ""
        except Exception as e:               # noqa: BLE001 — każdy błąd oznacza „pośrednika nie ma"
            ostatni = f"{type(e).__name__}: {e}"
    return False, f"{url} nie odpowiada ({ostatni})"


def scenario_outcomes(pairs):
    """Zwijają pary do wyniku NA ZADANIU: `(rozstrzygnięte, chwiejne, remisy)`.

    Jednostką dowodu jest ZADANIE, nie para (2026-11-09). Powtórzenia tego samego zadania dzielą
    stan wariantu, więc NIE są niezależnymi obserwacjami — własny artefakt bramki mówił to wprost
    (`independence is not established`), a stara reguła liczyła je jako `resolved` dowody
    (3 zadania × 4 powtórzenia dawały „12 rozstrzygnięć" przy 3 niezależnych obserwacjach).
    Zadanie jest rozstrzygnięte, gdy WSZYSTKIE jego powtórzenia mówią to samo; różne wyniki znaczą
    zadanie CHWIEJNE, czyli defekt EGZAMINU — przy regule `losses == 0` chwiejność zamienia się
    w fałszywe odrzucenie kandydata (zmierzone na B-002 2026-11-09). Noga z `infra_error` wyrzuca
    zadanie z dowodu w całości: awaria kabli nie jest dowodem w żadną stronę.
    """
    z = {}
    for p in pairs:
        if p.get("early_stop") or "kandydat" not in p:
            continue                                   # wiersz przerwania, nie para
        z.setdefault(p["task"], []).append((str(p.get("incumbent")), str(p.get("kandydat"))))
    rozstrzygnięte, chwiejne, remisy = [], [], []
    for zadanie, wyniki in z.items():
        głosy = set()
        for inc, cand in wyniki:
            if "infra_error" in (inc, cand):
                głosy.add("infra")
            elif inc == "fail" and cand == "pass":
                głosy.add("wygrana")
            elif inc == "pass" and cand == "fail":
                głosy.add("przegrana")
            else:
                głosy.add("remis")
        if "infra" in głosy:
            continue
        if len(głosy) > 1:
            chwiejne.append(zadanie)
        elif głosy == {"wygrana"}:
            rozstrzygnięte.append(("wygrana", zadanie))
        elif głosy == {"przegrana"}:
            rozstrzygnięte.append(("przegrana", zadanie))
        else:
            remisy.append(zadanie)
    return rozstrzygnięte, chwiejne, remisy


def cp_lower(w, n, conf=0.95):
    """Dolna granica Cloppera–Pearsona dla odsetka wygranych (bez scipy).

    Ogon `P(X >= w | n, p)` ROŚNIE z p, więc szukamy punktu, w którym spada do `1 - conf` —
    bisekcja od góry. Pierwsza wersja tej funkcji miała odwrócony warunek i zwracała bezsens
    (`cp_lower(5,5) = 0.0`, `cp_lower(4,4) = 1.0`), co wyglądało jak liczby; dlatego test
    w `tests/test_tierb_decision.py` porównuje wynik z NIEZALEŻNĄ postacią zamkniętą `α^(1/n)`,
    a nie z zapisaną stałą.
    """
    if n <= 0:
        return 0.0
    alpha = 1.0 - conf

    def ogon(p):                                       # P(X >= w | n, p), dokładnie
        return sum(math.comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(w, n + 1))

    lo, hi = 0.0, 1.0
    for _ in range(200):
        środek = (lo + hi) / 2
        if ogon(środek) > alpha:
            hi = środek                                # ogon za duży → p za wysokie
        else:
            lo = środek
    return round((lo + hi) / 2, 6)


def check_argv(task, scratch, variant_root, evidence_path="", state_path=""):
    """Pełne argv checka zadania — JEDNO źródło dla rolloutu, panelu i baterii falsyfikacyjnej.

    Po co osobna funkcja (2026-11-09): gałąź Tier B w `harness_eval.run_check` budowała argv
    `--variant`/`--home`, a KAŻDY check Tier B ma `--scratch` jako `required=True` (B-006 dodatkowo
    `--evidence`) — czyli polecenie nie do uruchomienia. Nie biegło w produkcji, więc nikt tego nie
    widział: klasa „parametr wymagany przez check, niepodawany przez wołającego".
    Kontrakt jest dwustronny i pilnuje go `tests/test_tierB_falsification.py`: brak wymaganego
    argumentu i argument NIEDECLAROWANY jednakowo wywalają argparse.
    """
    check = task["check"]
    if not os.path.isabs(check):
        check = os.path.join(BASE, check)
    argv = [sys.executable, check, "--scratch", scratch, "--variant", variant_root]
    if evidence_path:
        argv += ["--evidence", evidence_path]
    if state_path:
        argv += ["--snapshot", state_path]
    return argv


def pair_plan(tasks, repeats):
    """Plan par: `(powtórzenie, indeks_zadania, zadanie, czy_incumbent_pierwszy)`.

    Kolejność liczona z `(r + k) % 2`, NIE z indeksu planu. Przy parzystej liczbie zadań
    `i % 2` (gdzie `i = r*K + k`) daje temu samemu zadaniu zawsze tę samą kolejność, więc
    „naprzemienność" — jedna z niewielu kontrol nad zmiennością rolloutu — znikała dokładnie
    tam, gdzie miała działać (recenzja zewnętrzna 2026-11-09, §4.2). Wydzielone, bo plan da się
    przetestować bez wydawania ani jednego tokena.
    """
    return [(r, k, t, ((r + k) % 2 == 0))
            for r in range(max(1, int(repeats))) for k, t in enumerate(tasks)]


def paired(incumbent_root, candidate_root, tasks, max_losses=2, budget_s=900, repeats=1):
    """Sekwencyjnie, para po parze, z early stoppingiem (oszczędność tokenów).

    `repeats` = ile RAZY każdy task jest powtarzany; moc statystyczna kupujemy liczbą par
    (m ≥ 12 dla klas 1C/2), a nie luzowaniem progu. Kolejność nóg zmienia się co powtórzenie:
    bez tego „pierwsza noga rozgrzewa cache/środowisko" udawałoby różnicę harnessu.
    """
    from harness_eval import sign_test_p
    started = time.time()
    plan = pair_plan(tasks, repeats)
    pairs, wins, losses, ties, infra_errors = [], [], [], [], []
    for i, (r, k, task, first_inc) in enumerate(plan):
        if first_inc:
            inc = run_task(task, incumbent_root, "incumbent")
            cand = run_task(task, candidate_root, "kandydat")
        else:
            cand = run_task(task, candidate_root, "kandydat")
            inc = run_task(task, incumbent_root, "incumbent")
        broken = [f"{task['id']}:{lbl}" for lbl, r in (("incumbent", inc), ("kandydat", cand))
                  if r.get("status") == "infra_error"]
        pairs.append({"task": task["id"], "kolejnosc": ("incumbent,pierwsza" if first_inc
                                                        else "kandydat,pierwsza"),
                      "incumbent": inc["status"], "kandydat": cand["status"],
                      "inc_summary": inc.get("summary"), "cand_summary": cand.get("summary"),
                      "inc_wall_s": inc["wall_s"], "cand_wall_s": cand["wall_s"],
                      **({"infra_error": broken} if broken else {})})
        if broken:
            # Noga, która nie wystartowała, nie jest ani wygraną, ani przegraną — inaczej zmiana
            # dostawcy dostawcy fałszuje arytmetykę par (i werdykt o kandydacie).
            infra_errors.extend(broken)
            continue
        if inc["status"] == "pass" and cand["status"] == "fail":
            losses.append(task["id"])
        elif inc["status"] == "fail" and cand["status"] == "pass":
            wins.append(task["id"])
        else:
            ties.append(task["id"])
        if len(losses) >= max_losses:
            pairs.append({"early_stop": f"przerwano po {len(losses)} przegranych"})
            break
        if time.time() - started > budget_s:
            pairs.append({"early_stop": f"budżet czasu {budget_s}s wyczerpany"})
            break
    # Agregacja na SCENARIUSZACH — to ona jest dowodem; pary zostają dla śladu.
    _roz, _chw, _rem = scenario_outcomes(pairs)
    _wygr = [t for w, t in _roz if w == "wygrana"]
    _przegr = [t for w, t in _roz if w == "przegrana"]
    return {"pairs": pairs, "wins": wins, "losses": losses, "ties": ties,
            "infra_errors": infra_errors,
            "p_value": sign_test_p(len(_wygr), len(_przegr)),
            "repeats": max(1, repeats), "planned_pairs": len(plan),
            "resolved": len(wins) + len(losses),          # pary: ślad historyczny
            "resolved_scenarios": len(_roz), "decisive_scenarios": len(_roz),
            "chwiejne_zadania": _chw, "remisy_scenariuszy": _rem,
            "unique_scenarios": len({t['id'] for t in tasks}),
            "statistical_claim": "sign_test_over_decisive_scenarios",
            "p_value_note": ("powtórzenia służą STABILNOŚCI (wykrywaniu chwiejnego zadania), "
                             "nie mocy; dowodem są wyłącznie rozstrzygnięte scenariusze")}
