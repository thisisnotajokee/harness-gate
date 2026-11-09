# Instalacja bramki (jedna komenda, bez sudo, bez pytań)

## Wymagania

- `python3` (3.8+) — cała bramka to biblioteka standardowa, **zero zależności pip**.
- `git` — bramka porównuje drzewo z zapisaną wersją (kotwica). Bez `git` instalator odmawia.

## Instalacja

```sh
sh install.sh
```

To wszystko. Instalator:

1. kopiuje drzewo bramki do `$HERMES_HOME/scripts/harness_eval` (domyślnie `~/.hermes/scripts/harness_eval`),
2. zakłada **kotwicę Git** (`git init` + commit) — bez niej nie ma z czym porównywać integralności,
3. **przezbraja manifest** `panel.lock` na skopiowane drzewo,
4. tworzy `eval/variants/` (katalog egzaminu — brak katalogu nie jest stanem czystym),
5. dowiązuje `~/.local/bin/harness-gate` → launcher.

Na końcu wypisuje „co dalej" i „co pominięto". Nic nie pyta i **nie używa sudo**.

### Opcje

| opcja | znaczenie |
|---|---|
| `--home DIR` | inny dom Hermesa (domyślnie `$HERMES_HOME` albo `~/.hermes`) |
| `--dest DIR` | inny katalog bramki (domyślnie `<home>/scripts/harness_eval`) |
| `--force` | nadpisz istniejącą instalację (domyślnie instalator **odmawia**, gdy katalog nie jest pusty) |
| `--no-link` | nie twórz dowiązania w `~/.local/bin` |

## Po instalacji

```sh
harness-gate              # panel po ludzku (5–6 linii, 0 tokenów, ~1 s)
harness-gate --verbose    # pełna tabela kontroli (status, powód, czas)
harness-gate --fix        # naprawa: przezbrój manifest i kotwicę
harness-gate --help
```

Jeśli `harness-gate` nie jest znajdowane, dodaj `~/.local/bin` do `PATH` albo wołaj pełną
ścieżką: `<home>/scripts/harness_eval/bin/harness-gate.sh`.

## Konfiguracja (opcjonalna)

Świeża instalacja działa **bez pliku konfiguracji** (wartości domyślne: język `en`, paczki
wyłączone, Tier B wyłączony, parytet wyłączony). Chcesz coś zmienić:

```sh
cp <home>/scripts/harness_eval/gate.config.json.example \
   <home>/scripts/harness_eval/gate.config.json
$EDITOR <home>/scripts/harness_eval/gate.config.json
harness-gate --fix        # konfiguracja jest w manifeście — po zmianie przezbrój
```

Pełny opis kluczy: `docs/CONFIG.md`. Zepsuty plik konfiguracji to `REJECT` z powodem, nigdy
ciche wartości domyślne.

## Nietypowy dom (`HERMES_HOME` inny niż `~/.hermes`)

Bramkę można trzymać w dowolnym domu — w kontenerze, na próbę w `/tmp`, w `/opt/hermes`.
Obowiązuje jedna zasada:

> **`HERMES_HOME` wygrywa. `$HOME/.hermes` to wyłącznie wartość domyślna.**

Co z tego wynika w praktyce:

- **Instalacja:** `HERMES_HOME=/opt/hermes sh install.sh` — drzewo ląduje w `/opt/hermes/scripts/harness_eval`,
  a dowiązanie w `/opt/.local/bin` (przy domu bramki, nie w Twoim `~/.local/bin`). Cudze,
  działające dowiązanie zostaje nietknięte, chyba że podasz `--force`.
- **Uruchamianie:** `HERMES_HOME=/opt/hermes harness-gate`. Bramka mierzy **ten** dom i napisze to
  wprost na `stderr` (`i: mierzę drzewo … (z HERMES_HOME), a nie …`), żeby werdykt był jednoznaczny.
- **Sondy piaskownicy** (`probes_forbidden`) chronią **dom mierzony** (placeholder `<hermes_home>`),
  a nie zaszyte `~/.hermes` — inaczej chroniłyby cudzy katalog.
- **Snapshot i inne skrypty** czytają dom z `HERMES_HOME`, więc nie trzeba ich przepisywać.
- **`$HOME` bez `HERMES_HOME`** działa normalnie: bramka mierzy `$HOME/.hermes`. Dwa różne domy
  naraz (np. `HOME=/home/x`, `HERMES_HOME=/opt/hermes`) są **poprawne** — domem jest `/opt/hermes`,
  a `~` w poświadczeniach i sondach nadal znaczy `/home/x`.

Czego nie mieszaj: jeśli w `gate.config.json` wpiszesz `hermes_home` na sztywno, **konfiguracja
wygrywa nad zmienną** — bramka odmówi pracy (`FOREIGN_BASE`), gdy jej kod nie leży w tym drzewie.
To zamierzone: lepiej odmówić niż mierzyć jedno, a wykonywać drugie.

## `--fix` — kiedy go uruchomić

Uruchom `harness-gate --fix`, gdy:

- edytowałeś pliki bramki i dostajesz komunikat „pliki bramki zmieniły się od ostatniego uzgodnienia",
- zmieniłeś/usunąłeś `gate.config.json` (komunikat o zmianie konfiguracji),
- brakuje manifestu albo kotwicy.

`--fix` przezbraja `panel.lock`, zakłada/uzupełnia kotwicę Git i tworzy katalog egzaminu.
**Nie zmienia treści plików** i mówi wprost, co zrobił. Nic nie usuwa.

## Deinstalacja

```sh
rm -rf <home>/scripts/harness_eval
rm -f  ~/.local/bin/harness-gate
```

`memories/`, `skills/`, `kb/`, `config.yaml` nie są dotykane ani przy instalacji, ani przy
deinstalacji — bramka je tylko czyta.

## Rozwiązywanie problemów

| komunikat | co znaczy | co zrobić |
|---|---|---|
| `FOREIGN_BASE` / „runner uruchomiony z …, a drzewem bramki jest …" | uruchamiasz bramkę nie z tego drzewa, które mierzy | uruchom ją z zainstalowanej kopii (`<home>/scripts/harness_eval`) |
| „nie mam zapisanej wersji drzewa…" | brak kotwicy Git | `harness-gate --fix` |
| „pliki bramki zmieniły się od ostatniego uzgodnienia" | zmiana plików bez przezbrojenia manifestu | jeśli to Twoja zmiana: `harness-gate --fix` |
| „…nie wygląda na instalację Hermesa: brak katalogu skills/, plik state.db" (A-009) | wskazany dom nie jest domem Hermesa | sprawdź `--home`/`HERMES_HOME` |
| „brak katalogu …/eval/variants" (A-006) | brak katalogu egzaminu | `harness-gate --fix` |
| „The config file is invalid: …" | zepsuty `gate.config.json` | popraw albo usuń plik (wtedy działają domyślne) |

## Co instalator świadomie pomija

- **`gate.config.json`** — profil z tego drzewa (język, paczka, Tier B, parytet) **nie jest
  kopiowany**. U kogoś innego dałby czerwone kontrole domenowe, których nie ma jak spełnić.
  Dostajesz wartości domyślne + `gate.config.json.example` do skopiowania.
- **`tests/`, `tools/`, `docs/`** — nie są potrzebne do działania.
- **Tier B (płatne rolloute)** — domyślnie wyłączony; włączasz jawnie w konfiguracji.
