# Paczki i kontrakt kontroli

Panel to dwa poziomy: **rdzeń** (zadania generyczne, działają u każdego Hermesa) i **paczki**
(zadania zależne od konwencji konkretnego domu). Scalenie jest deterministyczne: najpierw zadania
rdzenia w kolejności autora, potem paczki alfabetycznie (wewnątrz — kolejność autora paczki).

## Jak wygląda paczka (drzewo katalogów)

```
v2/
├── panel.json                     # zadania RDZENIA (generyczne)
├── checks/
│   └── core/                      # checki rdzenia (A-001, A-002, A-004, A-005, A-006, A-007, A-009, tierb B-001/002/006/007/009)
│                                  # A-009 = check_cli_contract.py, w panel.json i panel.lock jak reszta rdzenia
└── packs/
    └── example/                     # NAZWA paczki
        ├── panel.d.json           # zadania paczki (schemat jak panel.json)
        └── checks/                # checki paczki
            ├── check_kb_index.py                    # A-003
            ├── check_anchor_freshness.py            # A-008
            ├── tierb_B-003-skill-activation.py
            └── tierb_B-004-live-state.py
```

Dziś istnieje jedna paczka: `packs/example` (nasza domena — flat-KB z mapą, kotwica-mirror, zadania
czytające `kb/`).

## Jak się ją włącza

Listą `packs` w `gate.config.json`:

```json
{ "packs": ["example"] }
```

- paczka **obecna na dysku, lecz nieaktywna** → jawny wiersz `N/A` z powodem i instrukcją
  („dopisz `example` do `packs` i przezbrój manifest"). Jeden wiersz na paczkę, nie na każde zadanie —
  bez tej konwencji cztery wiersze `N/A` byłyby szumem (`pack_rows()`).
- paczka **zadeklarowana w konfiguracji, a brak jej na dysku** → `fail` („konfiguracja deklaruje coś,
  czego nie ma"), nie cisza.
- **duplikat `id`** zadania rdzeń/paczka → problem widoczny w panelu (dwa zadania o jednej tożsamości
  psują parowanie w `--compare`).

Uwaga: dziś świeża instalacja v2 (bez `gate.config.json`, czyli `packs = []`) sprawia, że A-003 i A-008
**nie są uruchamiane** — nie ma ich w wierszach; zamiast tego pojawia się jeden wiersz `N/A` o paczce.
To oczekiwane i zgodne z kontraktem paczek.

## Schemat `panel.d.json`

```json
{
  "paczka": "example",
  "pack_version": "1.0.0",
  "opis": "...",
  "tasks": [ { ...zadanie... } ]
}
```

Zadanie (pola wspólne z `panel.json`):

| pole | wymagane | znaczenie |
|---|---|---|
| `id` | tak | Identyfikator zadania (np. `A-003-kb-index-consistency`); unikalny w całym scalonym panelu |
| `tier` | tak | `A` (statyczny, 0 tokenów) albo `B` (rollout — płatny) |
| `kategoria` | tak | Grupa tematyczna (raportowa) |
| `opis` | tak | Opis po ludzku |
| `check` | tak | Ścieżka checka **względem katalogu bramki** (np. `packs/example/checks/check_kb_index.py`) |
| `zrodlo` | tak | Pochodzenie reguły (papier/incydent/praktyka) |
| `epizod` | tak | Wskaźnik incydentu albo `null` |
| `czas_s` | tak | Orientacyjny budżet czasu |
| `prompt` | tylko Tier B | Treść zadania dla agenta w rolloucie |
| `seed` / `expect` | opcjonalne (Tier B) | Pliki startowe w katalogu roboczym / oczekiwany stan |
| `trace_probe` / `evidence_probe` / `state_probe` | opcjonalne (Tier B) | Rodzaj sondy dowodowej użytej przez werdykt |

Runner sonduje, czy `check` leży w katalogu bramki: ścieżka poza katalogiem = `fail`, brak pliku
checka = `skip` (`run_check`).

## Kontrakt checka

Check to samodzielny plik uruchamiany przez runner jako `python3 <check> <args>`.

- **Wyjście**: dokładnie **jedna linia JSON na stdout**:
  `{"check": <nazwa>, "status": <pass|fail|skip|na>, "summary": <tekst>, "details": [<tekst>, ...]}`.
  Runner czyta **ostatnią** linię stdout jako JSON.
- **Kod wyjścia**: `1` dla `fail`; `0` dla `pass`, `na` i `skip`. Kontrakt jest dwustronny:
  runner odrzuca `status=pass|na|skip` przy `rc != 0`, `status=na` bez powodu oraz nieznany status —
  check, który zwraca `rc=1` i `status=skip`, jest traktowany jako `fail` (nie „miękki pominięty").
- **`na` wymaga powodu** („nie dotyczy ≠ milczenie"); pusty `summary` przy `na` = `fail`.
- **Argumenty**: Tier A — `--home <drzewo>` (runner dokłada też `--variant <drzewo>`); A-005 dodatkowo
  `--repo` i `--repo-path`. Tier B — `--scratch` (wymagane), `--variant`, `--evidence`.
- **Tylko stdlib** — żadnych zależności zewnętrznych.
- **Reguły przez konfigurację**: check czyta progi/ścieżki wyłącznie przez `gate_config.for_checks()`
  (z drzewa **bramki**, nigdy z drzewa mierzonego — granica pilnowana przez A-002; strażnik nie może
  czytać własnej poprzeczki od kandydata). `for_checks()` z `ok: False` (zepsuta konfiguracja) →
  check **musi** zwrócić `fail`, nie wartości domyślne.

### Zasada: nieaktywna paczka → JAWNY wiersz `N/A`; zadeklarowana, a brakująca → `fail`

Zadania paczki, która **jest na dysku, ale nieaktywna** (nie ma jej w `packs` w konfiguracji),
**nie znikają cicho**. Zamiast milczenia pojawia się jawny wiersz `N/A` z powodem i instrukcją
włączenia. Paczka **zadeklarowana w konfiguracji, a nieobecna na dysku** to `fail` (konfiguracja
deklaruje coś, czego nie ma). Paczka nieobecna i niezadeklarowana nie dostaje wiersza — nie ma
czego wyjaśniać. Uzasadnienie: użytkownik nie może odróżnić „nie dotyczy" od „nie ma", jeśli obu
brakuje na ekranie. `N/A` (`na`) nie jest niezaliczeniem (nie czerwieni panelu), ale zmiana
stosowalności między nogami porównania jest blokerem — „wyłączenie zadania" nie może być
najtańszą drogą do `ACCEPT`.

### Zasada jednego pisarza przy manifeście

`panel.lock` (manifest integralności, pilnowany przez A-005) obejmuje **wszystkie** pliki `.py`,
`.sh`, `.json` w katalogu bramki — poza samym `panel.lock`, `.git` i `__pycache__`. Konsekwencje:

- **nowy plik** (nowy check paczki, nowy `panel.d.json`, nowa konfiguracja) = nowy wpis w manifeście;
  bez przezbrojenia `python3 panel_lock.py --write` kontrola A-005 zgłosi `fail` („brak w locku").
- **zmiana istniejącego pliku** bez przezbrojenia = `fail` („hash != lock").
- manifest pilnuje też zgodności `panel_version` z `panel.json`.

Manifest jest **jednoosobowy**: przezbrojenie locka zmienia reguły J, więc wymaga przeglądu i commita
człowieka (stąd „jeden pisarz"). Manifest złapał już własny dryf autorów w tym drzewie — dowód, że
działa, nie tylko teoretyzuje.
