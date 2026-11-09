# Harness Gate

**Bramka audytu harnessu Hermesa** — deterministyczne kontrole (0 tokenów) drzewa `~/.hermes`:
promptów, skilli, pamięci i konfiguracji. Odpowiada na jedno pytanie:

> Czy zmiana, którą właśnie zrobiłem w swoim agencie, faktycznie coś poprawiła — czy tylko
> wygląda lepiej?

Werdykt zamiast wrażenia: **„nic nie wymaga uwagi"** albo lista pozycji do poprawy, każda
z powodem i kodem wyjścia.

## Szybki start

```sh
sh install.sh          # kopiuje bramkę, zakłada kotwicę, tworzy katalog egzaminu
harness-gate           # panel po ludzku (0 tokenów, ~1 s)
harness-gate --verbose # pełna tabela kontroli + czasy
harness-gate --fix     # przezbrój manifest/kotwicę po zmianie plików bramki
```

Instalator **nie używa sudo**, nie zadaje pytań i **nie kopiuje** niczyjej konfiguracji:
dostajesz `gate.config.json.example` do świadomego skopiowania.

**Warunek:** bramka mierzy wyłącznie drzewo, z którego sama pochodzi. Uruchamiaj ją
z zainstalowanej kopii (`~/.hermes/scripts/harness_eval`) albo z dowiązania `harness-gate`.
Uruchomienie prosto z repo źródłowego kończy się `REJECT` / `FOREIGN_BASE` — to celowe
(nie chcesz, by cudzy kod oceniał twoje drzewo). Szczegóły: `docs/INSTALL.md`.

## Jak czytać wynik

Kod wyjścia: `0`, gdy każdy wiersz to `pass`/`na`; `1`, gdy jest `fail` lub `skip`.

```
Gate: 6/8 OK, 1 to fix
  • fix this: A-006-variant-cache-shared — brak katalogu …/eval/variants — brak egzaminu nie jest stanem czystym
Full report: …/eval/runs/20261109-011459-panel.json
More detail: run again with --verbose.
```

Wiersze nie wymagają żargonu: brak kotwicy to **instrukcja naprawy**, nigdy oskarżenie
o manipulację. `--verbose` dokłada po wierszu na kontrolę (`PASS`/`FAIL`/`SKIP`/`N/A`) i czasy.

## Co jest w środku (rdzeń, Tier A)

| kontrola | co mierzy |
|---|---|
| A-001 | każdy skrypt w skillach kompiluje się |
| A-002 | limity znaków pamięci (`MEMORY.md`, `USER.md`) |
| A-004 | `SKILL.md` ma poprawny frontmatter (m.in. `description`) |
| A-005 | **integralność bramki**: panel, runner, checki i `panel.lock` zgodne z kotwicą Git |
| A-006 | warianty testowe nie duplikują cache (inaczej każdy waży ~1 GB) |
| A-007 | spójność wyboru modelu/dostawcy (jedno źródło prawdy) |
| A-009 | kontrakt z binarką `hermes` (flagi, których używa bramka) |

Czego bramka **nie** robi: nie modyfikuje mierzonego drzewa (Tier A tylko czyta; jedyny zapis to
raport JSON), `--fix` naprawia wyłącznie spójność manifestu i kotwicy, i to mówi wprost.
To kontrola higieny, **nie audyt bezpieczeństwa**.

## Konfiguracja: jedno źródło prawdy

`gate.config.json` opisuje, co u Ciebie znaczy „poprawnie": limity pamięci, nazwę katalogu
konwencji, włączone paczki, kotwicę. Trzy stany pliku:

- **brak** → działają wartości domyślne (te same progi i ta sama surowość),
- **poprawny** → wartości są scalane z domyślnymi,
- **zepsuty/nieczytelny** → `fail` (fail-closed: literówka nie może cicho zmienić reguł).

Zmiana reguł bez przezbrojenia manifestu jest wykrywana — to cała pointa A-005.
Klucze i przykłady: `docs/CONFIG.md`.

## Tier B (opcjonalny, płatny)

Realne rolloute na kopii drzewa w piaskownicy (Landlock) — mierzy **zachowanie** agenta, nie
tylko pliki. Kosztuje tokeny, więc jest **wyłączony domyślnie** (`tier_b.enabled: false`)
i uruchamia się wyłącznie przez `--tier-b`.

**Co Tier B widzi, a czego nie — zmierzone, nie założone (2026-11-09).** Egzamin jest czuły na
**skutek**, nie na treść: skill z wyciętą całą treścią przechodzi (nic nie psuje wyniku), a jedna
pozornie sensowna, fałszywa instrukcja — która zmienia odpowiedź — jest wykrywana jako regresja.
Dla Ciebie znaczy to tyle: Tier B **wykryje poprawę wtedy, gdy poprawa zmienia obserwowalny wynik
zadania**. Nie oceni stylu dokumentacji ani tego, czy skill „ładniej tłumaczy".

Zasady, które warto znać przed włączeniem (wszystkie wymuszone kodem, nie dobrą wolą):

- **Poświadczenie.** Do piaskownicy wchodzą wyłącznie zmienne, których wskazany dostawca
  naprawdę wymaga — bramka czyta to z tabeli dostawców Hermesa (`hermes_cli.auth.PROVIDER_REGISTRY`).
  Gdy poświadczenia brak, bramka **odmawia** (`TIER_B_NO_CREDENTIALS`) **przed** wydaniem tokenów.
- **Dostawcy OAuth** (np. logowanie przez portal) nie mają klucza w zmiennej — dla nich ustaw
  `tier_b.proxy` na lokalny `hermes proxy`: poświadczenie zostaje po jego stronie, przez granicę
  piaskownicy przechodzi tylko port i token-atrapa.
- **Port bramki.** Współczesny Hermes dźwiga bramkę dla sesji i binduje port lokalny; piaskownica
  wpuszcza wyłącznie porty wskazane w `tier_b.gateway_ports`. Bez tego rollout pada przed
  wywołaniem modelu.
- **Awaria drogi do modelu nie jest werdyktem o kandydacie.** Rollout, który nie wystartował,
  jest raportowany jako `infra_error` i nie liczy się ani jako wygrana, ani jako przegrana.

## Rozszerzanie: paczki

Paczka = dodatkowe zadania dla kontroli zależnych od Twoich konwencji. Wzorzec do skopiowania
leży w `packs/example/` (jedna kontrola, trzy statusy, czytanie konfiguracji, komentarze
„dlaczego tak"). Kontrakt kontroli i sposób włączania: `docs/PACKS.md`.

## Granica zaufania

- Bramka **nie uruchamia** Tier B bez jawnego włączenia; nie modyfikuje mierzonego drzewa.
- Izolacja Tier B chroni **integralność** Twojego drzewa przed rollem, nie poufność. Piaskownica
  blokuje ZAPISY, nie odczyty: rollout czyta pliki i ma dostęp do sieci. Sekrety (`.env`, klucze)
  **nie** są kopiowane do piaskownicy, ale to **nie jest** izolacja poświadczeń — rollout może je
  odczytać z drzewa (do niego wchodzi tylko klucz modelu, potrzebny, by w ogóle zadziałał).
- `TAMPERING_DETECTED` / `LOCK_DRIFT` znaczą „rozjazd treści albo brak manifestu w HEAD" —
  nie samo słowo „atak". Bez kotwicy zobaczysz instrukcję naprawy, nie oskarżenie.

## Dokumentacja

- `docs/INSTALL.md` — instalacja, opcje, rozwiązywanie problemów
- `docs/CONFIG.md` — klucze konfiguracji i ich typy
- `docs/PACKS.md` — paczki i kontrakt kontroli

## Licencja

MIT — patrz `LICENSE`.
