# Konfiguracja bramki — `gate.config.json`

Źródło prawdy o kodzie: `gate_config.py` (stała `DEFAULTS`, walidacja `_validate`, scalanie `_merge`,
rozwiązywanie `resolve`). Plik konfiguracji leży obok `harness_eval.py` (albo pod ścieżką ze zmiennej
`HARNESS_GATE_CONFIG`).

Wartości domyślne dają **te same progi i tę samą surowość** (fail-closed) co nasza instalacja, ale
nie są tożsame z naszym profilem: domyślnie `lang: "en"`, `packs: []`, `tier_b.enabled: false`,
`parity.enabled: false`. Przykład niżej włącza język, przykładową paczkę, Tier B i parytet
(patrz „Przykład 2" niżej).

## Trzy stany pliku konfiguracji

| stan | zachowanie |
|---|---|
| **brak pliku** | Wartości domyślne (`DEFAULTS`). Pierwszy przebieg jest zielony bez żadnej konfiguracji. `load()` zwraca `ok: True`, `source: "domyślne (brak pliku)"`. |
| **plik poprawny** | Wartości użytkownika wygrywają, brakujące klucze zagnieżdżane są uzupełniane domyślnymi (`_merge`). Zwraca `ok: True` + `hash` efektywnej konfiguracji. |
| **plik zepsuty** | `ok: False` + powód. **Nigdy ciche domyślne**: literówka nie może wyglądać jak brak konfiguracji. Runner odmawia pracy (`REJECT`); domyślnie pokazuje zdanie po ludzku, a kod `CONFIG_INVALID` dokłada `--verbose` (JSON). Checki (`gate_config.for_checks()` z `ok: False`) muszą zwrócić `fail`. |

Plik zepsuty to: niepoprawny JSON, korzeń nie-obiekt, `schema != 1`, `anchor.mode` spoza dozwolonych,
zły typ któregoś klucza (patrz kolumna „Walidacja" niżej). Nieznany klucz (najwyższego poziomu lub
w znanej sekcji) **nie** psuje pliku — trafia do `warnings` jako „zignorowany", ale zostaje w hashu.

## Klucze — pełna tabela

Kolumna „Reguła gry?" = czy zmiana wpływa na kryteria/werdykt. **Mechanicznie**: cały plik jest
objęty manifestem `panel.lock`, więc każda zmiana pliku (gdy istnieje) bez przezbrojenia manifestu
(`python3 panel_lock.py --write`) daje `fail` kontroli **A-005** („reguły się zmieniły").

### Klucze najwyższego poziomu

| klucz | typ | domyślna | co zmienia | walidacja `_validate` | Reguła gry? |
|---|---|---|---|---|---|
| `schema` | int | `1` | Wersja schematu konfiguracji | musi być `1` (inaczej `fail`) | tak (format) |
| `lang` | str | `"en"` | Kod języka warstwy komunikatów (`pl`/`en`) | musi być stringiem | nie (tylko komunikaty) |
| `hermes_home` | str | `"auto"` | Drzewo mierzone; `auto` → `$HERMES_HOME` albo `~/.hermes` | string albo `"auto"` | tak (zakres egzaminu) |
| `eval_dir` | str | `"auto"` | Katalog na `variants/`, `runs/`, `ledger.jsonl`; `auto` → `<home>/eval` | string albo `"auto"` | tak (zakres egzaminu) |
| `anchor` | obj | patrz niżej | Repozytorium kotwicy (integralność A-005 / parytet A-008) | obiekt; `anchor.mode` ∈ {`in-place`,`external`,`none`} | tak |
| `packs` | list | `[]` | Nazwy włączonych paczek (ich zadania są scalane do panelu) | lista | tak (zakres egzaminu) |
| `tier_b` | obj | patrz niżej | Płatne rolloute Tier B | obiekt; `tier_b.enabled` musi być bool | tak |
| `limits` | obj | patrz niżej | Progi kontroli A-002/A-004/A-007 | obiekt; każdy klucz to liczba całkowita | tak |
| `threshold_sources` | list | `[]` | Pliki progów wchodzące do hasha wersji J | lista | tak (wpływa na hash progów) |
| `paths` | obj | `{"kb":"kb"}` | Względne nazwy katalogów konwencji hosta (np. `kb`) | obiekt | tak (zakres egzaminu) |
| `cli` | obj | patrz niżej | Kontrakt z binarką Hermesa | obiekt | tak |
| `parity` | obj | patrz niżej | Parytet kotwicy (A-008) | obiekt; `parity.enabled` musi być bool | tak |
| `probes_forbidden` | list | `["<hermes_home>","~/.ssh"]` | Ścieżki, których piaskownica Tier B **nie** dostaje w `--rw` (sondy fail-closed). **`<hermes_home>` = dom MIERZONY** (rozwiązywany przy wczytaniu), więc przeniesienie domu nie zostawia osłony na starej ścieżce; `~` znaczy prawdziwy dom użytkownika | lista | tak |

### Sekcje zagnieżdżone

**`anchor`** — `{mode, repo, repo_path}`

| klucz | typ | domyślna | co zmienia (`resolve`) |
|---|---|---|---|
| `anchor.mode` | str | `"in-place"` | `in-place` → kotwica = katalog bramki, `repo_path="panel.lock"`; `external` → `repo=""` gdy `auto`, `repo_path="scripts/harness_eval/panel.lock"`; `none` → brak kotwicy (obie puste) |
| `anchor.repo` | str | `"auto"` | Ścieżka repo kotwicy; `auto` bierze się z trybu |
| `anchor.repo_path` | str | `"auto"` | Ścieżka pliku locka wewnątrz repo; `auto` bierze się z trybu |

**`tier_b`** — `{enabled, model, provider, budget_s, repeats, gateway_ports, proxy}`

| klucz | typ | domyślna | co zmienia |
|---|---|---|---|
| `tier_b.enabled` | bool | `false` | Czy `--tier-b` w ogóle wystartuje (świeża instalacja nie odpala płatnych rolloutów przez nieuwagę) |
| `tier_b.model` | str | `""` | Model rolloutu Tier B (`""` → z `config.yaml`) |
| `tier_b.provider` | str | `""` | Dostawca rolloutu Tier B (`""` → z `config.yaml`) |
| `tier_b.budget_s` | int | `900` | Budżet czasu rolloutu w sekundach |
| `tier_b.repeats` | int | `0` | Liczba powtórzeń; `0` → wyliczane z `GATE_PARAMS.tier_b_m_min` i liczby zadań |
| `tier_b.gateway_ports` | list | `[]` | Porty, na których wolno wstać bramce rolloute. **Wymagane**, gdy Hermes dźwiga bramkę w obcym `HERMES_HOME` (dziś: zawsze) — piaskownica wpuszcza tylko wymienione porty, a bramka na porcie losowym dostaje `Permission denied` i rollout pada na `not_ready`. Wariant dostaje `webapp: {enabled: true, port: …}` (pierwszy port = noga incumbent, drugi = kandydat) |
| `tier_b.proxy` | obj | `{"url":"","provider":"gate-proxy","key_env":"GATE_PROXY_KEY"}` | Lokalny pośrednik (`hermes proxy`) dla dostawców **OAuth** (nous, xai): poświadczenie zostaje po stronie proxy, a rollout dostaje adres i token-atrapę. Gdy `url` jest puste — rollout używa `tier_b.provider`. Port pośrednika jest wyliczany z `url` i automatycznie wpuszczany do piaskownicy |

**Skąd bierze się poświadczenie rolloutu.** Piaskownica dostaje **wyłącznie** zmienne, których wskazany dostawca naprawdę wymaga — bramka czyta to z tabeli dostawców Hermesa (`hermes_cli.auth.PROVIDER_REGISTRY`), a nie z własnej listy: `deepseek` → `DEEPSEEK_API_KEY`, `huggingface` → `HF_TOKEN`, `nous` → *(brak — OAuth, tylko przez pośrednika)*. Gdy brak poświadczenia, `--tier-b` **odmawia** kodem `TIER_B_NO_CREDENTIALS` **przed** wydaniem tokenów, zamiast pokazać cichy `infra_error` wyglądający jak wina kandydata.


**`limits`** — progi kontroli (wszystkie int)

| klucz | domyślna | co mierzy |
|---|---|---|
| `limits.memory_char` | `2500` | Limit znaków `memories/MEMORY.md` (A-002) |
| `limits.user_char` | `1500` | Limit znaków `memories/USER.md` (A-002) |
| `limits.a004_max_warnings` | `150` | Próg ostrzeżeń frontmattera (A-004) |
| `limits.a007_max_warnings` | `0` | Próg ostrzeżeń spójności modelu/dostawcy (A-007) |

**`cli`** — kontrakt z binarką

| klucz | typ | domyślna | co zmienia |
|---|---|---|---|
| `cli.bin` | str | `"hermes"` | Nazwa/ścieżka binarki Hermesa (`resolve_bin`: `PATH` → `~/.local/bin` → jak podano) |
| `cli.required_flags` | list | `["--in","-z","--yolo","-m","--provider"]` | Flagi, których obecności wymaga kontrola kontraktu CLI |
| `cli.version_max` | str | `""` | Górna granica wersji CLI (pusta = brak limitu) |

**`parity`** — zbiór parytetu A-008

| klucz | typ | domyślna | co zmienia |
|---|---|---|---|
| `parity.enabled` | bool | `false` | Wyłączne → jawny wiersz `N/A` z powodem; włączone bez kotwicy/plików → `fail` |
| `parity.repo` | str | `"auto"` | Repo parytetu; `auto` → rodzeństwo domu (`../anchor`) |
| `parity.files` | list | `["SOUL.md","memories/MEMORY.md","memories/USER.md","kb/_INDEX.md","kb/core/*.md","kb/incidents/*.md"]` | Pliki porównywane bez wyjątków |
| `parity.clean_prefixes` | list | `["skills/","kb/","scripts/","memories/","agent-hooks/","SOUL.md"]` | Prefiksy sprawdzane pod „czyste drzewo" (zatrzymany sync) |

**`paths`** — konwencja hosta

| klucz | typ | domyślna | co zmienia |
|---|---|---|---|
| `paths.kb` | str | `"kb"` | Nazwa katalogu bazy wiedzy, którą czytają kontrole paczek (A-003, B-004); u kogoś innego może być np. `docs` |

## Przykład 1 — minimalny (pusto = wszystko domyślne)

Brak pliku w ogóle (zalecane dla pierwszej instalacji) **albo** jawne puste nadpisania:

```json
{ "schema": 1 }
```

Efekt: identyczne wartości jak `DEFAULTS` (progi produkcyjne, kotwica `in-place`, paczki puste,
Tier B wyłączony, parytet wyłączony). To droga „świeżej instalacji" — u obcego drzewa daje werdykt
zielony bez konfiguracji, a pozycje niepasujące do jego konwencji pojawiają się jako jawne `N/A`.

## Przykład 2 — konfiguracja z włączoną paczką

```json
{
  "schema": 1,
  "lang": "en",
  "hermes_home": "auto",
  "eval_dir": "auto",
  "anchor": { "mode": "in-place" },
  "packs": ["example"],
  "paths": { "kb": "kb" },
  "tier_b": { "enabled": false, "gateway_ports": [8646, 8647],
              "proxy": { "url": "", "provider": "gate-proxy", "key_env": "GATE_PROXY_KEY" } },
  "cli": { "bin": "hermes" }
}
```

Włączenie paczki poszerza zakres egzaminu, więc po jej dopisaniu trzeba przezbroić manifest
(`panel_lock.py --write`, w instalacji robi to `harness-gate --fix`) — inaczej A-005 słusznie
zgłosi drift. Tier B zostaje wyłączony, dopóki nie wskażesz dostawcy i poświadczenia
(patrz wyżej: `tier_b.proxy` dla dostawców OAuth).


## Uwaga o drugiej prawdzie

Parametry **decyzyjne ewaluatora** (`tier_b_m_min`, `tier_b_delta`, reguła werdyktu) **nie** są tu
kluczami — mieszkają w `eval_version.GATE_PARAMS`, bo wchodzą do hasha progów i wymuszają wpis
`J_MUTATION`. Próba wpisania ich do pliku (np. `tier_b.m_min`) kończy się **ostrzeżeniem**
„nieznany klucz konfiguracji" i zignorowaniem — nie cichym przyjęciem.
