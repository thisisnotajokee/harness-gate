#!/usr/bin/env python3
"""eval_version.py — wersja ewaluatora J + hash progów watchdogów.
Cel: zmiana progu w watchdogu musi być widoczna jako J_MUTATION, a nie jako
poprawa/regresja harnessu H. Bez tego rozluźnienie poprzeczki udaje postęp."""
import hashlib, json, os, re, sys

EVALUATOR_VERSION = "2026-10-15.22"  # .22: dowód Tier B liczony na SCENARIUSZACH (D1+D2, 2026-11-09):
#     `tier_b_m_min` 12 -> 6 (jednostką dowodu jest ZADANIE, nie para) oraz auto-`repeats` = 2
#     (powtórzenia służą stabilności, nie mocy). Zmiana progu decyzyjnego => hash progów się zmienia
#     => wpis `J_MUTATION` z uzasadnieniem Piotrka.
# .20: ZMIANA REGUŁ GRY (J_MUTATION), panel 1.4.17 — DOMKNIĘCIE TIER A (kryterium, NIE próg).
#     Ostatni dług z audytu 29.10: A-004 dopuszczał DWIE kategorie z tym samym `name` (np.
#     `kat1/foo` + `kat2/foo`), a aktywacja skilla idzie PO NAZWIE — `skill_view(name='foo')`
#     było niejednoznaczne. Decyzja Piotra (08.11): zaostrzyć. Check zbiera nazwy par zgodnych
#     z katalogiem i raportuje duplikaty jako `fail` (rozjazd nazwa↔katalog leci do `bad` osobno,
#     więc komunikat o kolizji nie dokłada szumu do już zgłoszonego błędu).
#     ZMIERZONE PRZED wdrożeniem: produkcja 98 SKILL.md / 0 kolizji, fikstura bez `skills/` —
#     reguła nie tworzy fałszywego FAIL-a na zdrowym drzewie.
#     Dowód: bateria „stare vs nowe" (`tierA_failopen_bateria.py`; przy tej zmianie
#     `TIERA_OLD_REV=e68d6b8`, bo sam HEAD kotwicy niósł już poprzedni pakiet poprawek
#     i wiersze „stare" przestałyby być stare) → 20/20 OK: **7 fail-openów zamkniętych łącznie**,
#     7 kontroli pozytywnych bez zmian, produkcja i fikstura bez regresji.
#     Testy: 178 OK, `expectedFailure` = **0** — klasa `Klamstwa` jest PUSTA: audyt Tier A
#     nie ma znanych kłamstw (było 6 na starcie 29.10, potem 1 po pakiecie z 1.4.16).
#     Progi decyzyjne (`GATE_PARAMS`, m, δ) i `SOURCES` BEZ zmian → hash zostaje
#     `12058d81d9c78e94`; wpis `J_MUTATION` niesie TEN SAM hash.
# .19: ZMIANA REGUŁ GRY (J_MUTATION), panel 1.4.16 — SPŁATA FAIL-OPENÓW TIER A (kryteria, NIE progi).
#     Zamknięte 5 z 6 dziur zmierzonych audytem falsyfikacyjnym 29.10.2026:
#       (1) A-003 opróżniony vault przechodził jako „mapa zgodna";
#       (2) A-003 brak `_INDEX.md` WYCISZAŁ check (`skip`) — „nie wiem" czytane jak „czysto"
#           (i brak całego katalogu `kb/` tak samo);
#       (3) A-004 katalog skilla nieczytelny (`chmod 000`) był NIEWIDZIALNY: `os.walk` milczy o takim
#           katalogu, więc ukryty skill nie wchodził do pomiaru, a check raportował „sprawdzono N";
#       (4) A-006 symlink poza `.shared` przechodził jako „poprawnie: współdzielone" — kod sprawdzał
#           tylko `islink`, nigdy CELU linku (obietnica z docstringa bez egzekucji);
#       (5) A-006 brak katalogu wariantów był `skip` („nie ma czego pilnować"), czyli brak egzaminu
#           Tier B wyglądał jak brak problemu.
#     Dowód (bateria „stare vs nowe", ~/.hermes/cache/scratch/tierA_failopen_bateria.py): te same światy
#     na checkach z `git HEAD` kotwicy i na nowych → 6 fail-openów zamkniętych (pass/skip → fail),
#     6 kontroli POZYTYWNYCH bez zmian (uczciwe stany nadal zielone), produkcja i fikstura bez regresji
#     (18/18 OK). Testy: 177 OK, `expectedFailure` 6 → 1 — został wyłącznie dług POLITYCZNY (kolizja
#     nazw skilli w A-004), który nie jest luką implementacyjną i wymaga osobnej decyzji Piotra.
#     Progi decyzyjne (`GATE_PARAMS`, m, δ) i `SOURCES` BEZ zmian, więc hash progów zostaje
#     `12058d81d9c78e94` — ten wpis `J_MUTATION` niesie TEN SAM hash: nie ruszono poprzeczki,
#     zmieniono KRYTERIA (co bramka uznaje za dowód).
# .18: ZMIANA REGUŁ GRY (J_MUTATION), panel 1.4.14 — B-003: AKTYWACJA POTWIERDZONA WYNIKIEM.
#     Do .17 check B-003 czytał WYŁĄCZNIE napisane wywołanie (`SELECT tool_calls … LIKE
#     '%skill_view%'`), więc `skill_view` zwracające BŁĄD albo PUSTKĘ liczyło się jako
#     „skill wczytany" — zadanie nazwane „aktywacja" mierzyło wzmiankę o aktywacji.
#     Zmierzone baterią (`~/.hermes/cache/scratch/b003_bateria_stare_nowe.py`, światy z seeda
#     w panel.json, stara wersja z git HEAD kotwicy vs nowa, każda dostaje świat w swoim języku:
#     historii wariantu i pliku dowodowego): **3 fail-open ZAMKNIĘTE, 0 false-failów**.
#       (V3) wynik = błąd (`Skill … not found`) + poprawna liczba → było `pass`, jest `fail`;
#       (V4) wywołanie bez wyniku (brak wiersza `role='tool'`) → było `pass`, jest `fail`;
#       (V6) `skill_view` tylko w HISTORII wariantu (poprzedni przebieg) → było `pass`, jest `fail`.
#     V6 to znalezisko wykryte PRZY wdrażaniu (audyt mierzył V3/V4): stara ślad czytała całą
#     historię wariantu bez punktu odcięcia, więc wywołanie z poprzedniego powtórzenia zaliczało
#     aktywację w bieżącym — ta sama klasa co B-001. Dlatego check nie czyta już `state.db`:
#     bierze `--evidence` z punktem odcięcia liczonym przez runner (`trace_probe`) i wspólną
#     implementacją dowodu z `evidence.py` (jedno źródło: wyniki czyta ten sam kod co B-001/B-006).
#     Strona przeciwna: uczciwa droga nadal `pass` — także gdy wynik `skill_view` nie jest JSON-em,
#     ale jest niepusty (nie karzemy za format, jak przy `9.8` w B-001).
#     UTWARDZONE w tej samej zmianie (latentne, nie reprodukowane jako zmiana werdyktu): plik
#     odpowiedzi wybierany po DOKŁADNEJ nazwie z promptu (`core_count.md`) zamiast „pierwszego
#     `.md` z `core` w nazwie" — stara reguła zależała od kolejności katalogu (klasa N18 z B-001).
#     Progi decyzyjne (m, delta) i `GATE_PARAMS` BEZ zmian, `SOURCES` bez zmian, więc hash progów
#     zostaje ten sam: `12058d81d9c78e94`.
# .17: ZMIANA REGUŁ GRY (J_MUTATION), panel 1.4.13 — DOWÓD NIELEKSYKALNY W B-001.
#     Do .15 kryterium „wykonanie skryptu" rozgrywało się w ARGUMENCIE komendy, czyli w tym, co
#     model NAPISAŁ (kształt: interpretator przed nazwą skryptu). Teraz `evidence.py` czyta także
#     WYNIK: `trace_since(..., with_results=True)` dokłada treść wiersza `role='tool'` (wiązanie
#     po `id` wywołania ↔ `tool_call_id`), a B-001 wymaga, by w wyniku wywołania o kształcie
#     uruchomienia była LICZBA równa sumie wypisanej przez przypięty skrypt.
#     Zmierzone baterią na dwóch wersjach checka (stara z git HEAD kotwicy vs nowa; światy budowane
#     z seeda w panel.json): 4 fail-open ZAMKNIĘTE, 0 false-failów.
#       (N7b) nieudany przebieg — „Permission denied" — przechodził jako wykonanie; realny ślad
#             z rolloutu 29.10 zawiera dokładnie taką nieudaną próbę PRZED udaną, więc luka leżała
#             w zasięgu normalnego zachowania, nie tylko ataku;
#       (N19) wynik z ZŁĄ liczbą (`RAZEM: 19.80`) wystarczał, bo werdykt nie patrzył na wynik;
#       (N20) stary format dowodu (wywołania bez `result`) i (N21) brak wyników zgłoszony jawnie
#             (`results_error`) też przechodziły — teraz oba padają fail-closed.
#     Strona przeciwna: PRAWDZIWY ślad z rolloutu 29.10 nadal `pass`, a wyjście przefiltrowane
#     (`| awk` → samo `9.80`) także — zaostrzenie nie może karać za format (false-fail z .15).
#     Podział kanałów (świadomy): brak dostępu do wyników NIE kasuje śladu leksykalnego, bo B-006
#     mierzy zachowanie na ARGUMENTACH; runner zapisuje `results_error` jawnie, a B-001 — dla
#     którego wynik jest kryterium — pada wtedy fail-closed, zamiast po cichu mierzyć mniej.
#     GRANICA DOWODU (uczciwie): wyniki leżą w `state.db` WARIANTU, czyli w drzewie zapisywalnym
#     przez rollout — to poprzeczka dla zachowania i przypadkowego przejścia, nie dowód
#     kryptograficzny przeciwko fałszowaniu bazy.
#     Progi decyzyjne (m, delta) i `GATE_PARAMS` BEZ zmian, `SOURCES` bez zmian, więc hash progów
#     zostaje ten sam: `12058d81d9c78e94`.
# .16: ZMIANA REGUL GRY (J_MUTATION), panel 1.4.12 — USUNIECIE MECHANIKI KOSZTOW I PASKA.
#     Decyzja Piotrka (2026-10-29, dwie tury): mechanika kosztow "do calkowitego usuniecia,
#     nieaktualna i niepotrzebna, nie bierzemy jej pod uwage przy naszych projektach", a po
#     rozdzieleniu pluginu — takze collapse tool-outputow i alert kontekstowy.
#     Co zniknelo: plugin `session-cost-guard` (pasek ~$, bramka na dostawce), skrypty
#     `session_cost_guard.py`, `cost_calc.py`, `cost_metrics.py`, `cost_audit.sh`,
#     `pricing.yaml`, tygodniowy cron "Audyt kosztow" (8126c5f43ad1), archiwalny skill
#     `llm-cost-tracking`, referencja `cost-metrics-aggregation.md` w `homelab-ops`.
#     Nastepnie pyta Piotrek kazal usunac takze plugin `context-guard` (collapse + alert
#     kontekstowy) — czyli zaden hook nie dokleja juz nic do odpowiedzi ani nie przycina
#     wynikow narzedzi.
#     SKUTEK DLA BRAMKI (dlatego to J_MUTATION, a nie cicha zmiana): `SOURCES` traci
#     `session_cost_guard.py`, ktory wnosil TRZY progi do hasha — `CTX_ALERT=100_000`,
#     `CACHE_ALERT_CLOSE=50_000`, `CACHE_RATE_ALERT=0.70`. Hash progów zmienia sie, bo
#     znika zrodlo tych progow (nie dlatego, ze ktos je rozluznil). Wpis J_MUTATION
#     w ledgerze niesie NOWY hash, zeby `ledger_check` nie zglaszal `threshold_drift`.
#     Progi DECYZYJNE Tier A/B (`GATE_PARAMS`, m, delta) BEZ zmian.
#     INERTNE PO USUNIECIU (swiadomie zostawione): metryka `RECUT_ALERT` w
#     `quality_watchdog.py` mierzyla dociaganie pelnego wyniku po collapse — bez collapse
#     nie ma czego mierzyc, wiec metryka nie zapali sie nigdy. Zostaje jako diagnostyka
#     (wraca do gry, gdyby collapse kiedys wrocil) i jako prog w hashu; jej usuniecie
#     zmieniloby hash bez zwiazku z ta decyzja.
# .15: ZMIANA REGUŁ GRY (J_MUTATION), panel 1.4.11 — kryterium werdyktu B-001 przestaje być
#     dopasowaniem NAPISU, a staje się pomiarem WYKONANIA i WARTOŚCI. Powód: audyt falsyfikacyjny
#     2026-10-29 (bateria 20 światów, `b001_audyt.py`) zmierzył na produkcji 4 × `fail-open`
#     i 1 × `false_fail`:
#     (1) „ślad uruchomienia" był PODCIĄGIEM nazwy w argumentach, więc `cat cost_metrics.py`
#         wystarczał za wykonanie — i to nie atak teoretyczny: realny uczciwy model w kroku 1
#         robi dokładnie `cat cost_metrics.py` (dowód z żywego rolloutu, nie z wyobraźni).
#         Teraz wymagany jest KSZTAŁT uruchomienia (interpretator / ścieżka wykonywalna przed
#         nazwą skryptu), komenda jest dzielona po `;`/`&&`/`||`, a fragmenty czytające odrzucane.
#     (2) suma była dopasowywana podciągiem, więc raport z błędną liczbą `19.80` przechodził
#         (zawiera `9.80`), a uczciwe `9.8` było KARANE (false_fail). Teraz porównanie LICZBOWE
#         przez `Decimal` — jeden ruch zamyka obie strony.
#     (3) data była dopasowywana wzorcem `20\d\d-\d\d-\d\d`, więc `2026-99-99` przechodziło jako
#         „data=True"; teraz walidacja kalendarza (`date.fromisoformat`).
#     (4) plikiem raportu był „pierwszy `.md` z koszt/cost w nazwie", czyli werdykt zależał od
#         arbitralnej kolejności katalogu i przechodził plik spoza promptu — teraz DOKŁADNIE
#         `koszty.md`, bo tyle narzuca prompt. Uwaga: to koryguje połowicznie obalone roszczenie
#         audytu AI z 26.10 („check zgodny z promptem") — prompt faktycznie nazywał plik, ale
#         check tego nie wymuszał.
#     Pomiar po naprawie: 0/0 w baterii, a REALNY ślad z rolloutu nadal `pass` (zaostrzenie nie
#     psuje działającej drogi). Do tego pierwszy realny przebieg B-001 po naprawie 1.4.8:
#     `baserate --variant incumbent --tasks B-001 --runs 1` → PASS w 13.9 s, `fail_rate=0.0`,
#     co potwierdza działanie ścieżki `trace_probe` end-to-end.
#     Testy: 139 → **144 OK** (5 regresji: N7/N10/N11/N12/N18). Progi decyzyjne (m, δ)
#     i `GATE_PARAMS` BEZ zmian, więc hash progów zostaje ten sam: `288407241bb77b13`.
# .14: ZMIANA REGUŁ GRY (J_MUTATION), panel 1.4.10 — dwie rzeczy, obie z incydentu 2026-10-27
#     (job monitora padał 13 razy przez 10 dni na HTTP 402, bo wybór modelu był skopiowany w ośmiu
#     miejscach, a nieudany rollout wyglądał jak werdykt o kandydacie):
#     (1) NOWY CHECK A-007 (Tier A, 0 tokenów): `config.yaml` to jedyne źródło prawdy o modelu i
#         dostawcy; ostrzeżenia za pin wskazujący innego dostawcę (delegacja/cron/job/domyślne runnera),
#         nazwę modelu bez przedrostka dostawcy i martwy duplikat wyboru modelu (`DEFAULT_MODEL` w .env).
#         Próg w GATE_PARAMS (`a007_max_warnings`, domyślnie 0) — dlatego hash progów się zmienia:
#         `86d172d7d48b1e54` → `288407241bb77b13`. Sam runner Tier B przestał mieć zaszyte domyślne
#         `deepseek`/`deepseek-v4-flash` i bierze model z configu (env nadal może nadpisać trasę).
#     (2) NOWA KLASA WYNIKU `infra_error` w Tier B: noga, która nie wystartowała (rc ≠ 0 ORAZ brak
#         nowego śladu w state.db ORAZ brak artefaktów poza seedem), nie wchodzi do arytmetyki par,
#         a werdykt to `INFRA_ERROR` (fail-closed, bez rozstrzygania) — wcześniej taka noga była
#         „przegraną", więc ZMIANA DOSTAWCY odrzucała dobre kandydaty (reguła `losses == 0`).
#         Punkt odcięcia śladu liczymy teraz dla KAŻDEGO trybu sond: liczony tylko dla sond dawał
#         `0` dla zadań `state_probe`, a `trace_since(db, 0)` oddaje całą historię wariantu — czyli
#         „ślad istnieje" właśnie wtedy, gdy rollout nie wystartował (złapane na realnym przebiegu).
#     Testy: 128 → **139 OK**. Progi decyzyjne Tier B (m, δ) BEZ zmian.
# .13: POPRAWA RAPORTU (klasa J), progi i GATE_PARAMS BEZ zmian, bez wpisu J_MUTATION — tak jak .8.
#     Defekt: promocja 96 plików weszła do ledgera jako PROVISIONAL z pełną listą `changed_files`,
#     a `skill_drift --check --log` (pole `script` joba monitora) dopisał te same pliki drugi raz jako
#     DRIFT_UNREVIEWED — okno obserwacji dostawało dwa wiersze o jednej zmianie, a raport dzienny
#     „do przeglądu" bez powodu. Teraz pokrycie liczy się WYŁĄCZNIE z wpisów przyjętych
#     (PROVISIONAL/J_MUTATION/AMENDMENT) i tylko w oknie 24 h — bez okna stara akceptacja maskowałaby
#     każdą późniejszą zmianę tego samego pliku (cicha dziura, klasa F9). Pliki pokryte nie wchodzą do
#     wpisu, a stdout mówi to samo co ledger (jest wstrzykiwany do promptu raportu).
#     Uwaga: to NIE jest „nie loguj wpisów z nieudanych przebiegów" — wpisy dryftu pisze script joba
#     (przed agentem) i są prawdziwe także wtedy, gdy krok LLM padnie; usunięcie ich gubiłoby ślad.
# .12: POPRAWA HARNESSU H, panel 1.4.9 — B-004 przestaje czytać prawdę z drzewa liczonego PO rolloucie.
#     Powód (audyt falsyfikacyjny 26.10, dwa ostatnie `expectedFailure`): (a) ziemia prawdy pod kontrolą
#     kandydata — agent dorzucał plik do drzewa i podawał nową liczbę, a check potwierdzał „zgadza się";
#     (b) nieistniejące drzewo dawało „stan odczytany: 0, 0" i pass, czyli nic nie było weryfikowane.
#     Teraz prawda to snapshot stanu zamrożony przez runner PRZED rollem (`--state-only` na przyjętym
#     checku, plik w `eval/evidence/` — poza każdym `--rw` rolloutu, więc poza kontrolą kandydata),
#     rozjazd snapshotu z drzewem = fail, brak drzewa = fail-closed, snapshot w katalogu zapisywalnym
#     przez rollout = odrzucony. Nowy tryb sondy `state_probe` (osobny od evidence/trace_probe:
#     mierzy STAN, nie proces). To wpis J_MUTATION, bo zmieniło się ŹRÓDŁO PRAWDY egzaminu
#     (z drzewa czytanego po rolloucie na snapshot sprzed rolloutu) — ZGODNIE z klasyfikacją
#     zapisaną w README i w uzasadnieniu wpisu .11. Progi decyzyjne (m, δ) i GATE_PARAMS BEZ zmian,
#     więc hash progów zostaje ten sam — sam hash nie jest kryterium klasy, kryterium jest to,
#     co kandydat musi spełnić / skąd bierze się prawda.
# .4: kotwica drzewa (F1/F5), kontrakt checka status↔rc (F3), walidacja nazwy wariantu (F4),
#     ścieżki w werdyktach (paths), --skip-preflight dla kontroli negatywnej (F6), fix --clean.
# .5: monitor nie kłamie gdy brak pomiaru (F9: None ≠ „czysto"), Tier A obowiązkowy przed
#     Tier B (F2: regresja statyczna = REJECT bez płacenia tokenami), martwa gałąź (4,1)
#     usunięta — kod jawnie ostrzejszy niż SPEC (F7), powrót przez decyzję J_MUTATION.
# .6: moc zamiast luzowania progu — klasy 1C/2 wymagają m ≥ 12 rozstrzygniętych par
#     (kupione tokenami, nie wyjątkiem w regule); kolejność nóg alternowana co powtórzenie.
#     Zmiana reguł gry = J_MUTATION (wpis w eval/ledger.jsonl), nie poprawa harnessu.
# .7: pomiar STANU i GRANIC, nie procesu — nowe zadania Tier B: B-004 (licz z drzewa, nie z pamięci),
#     B-006 (dowód = snapshot SHA256 z pamięci nadrzędnej + ślad tool_calls, check poza --rw),
#     B-007 (dyscyplina zakresu na plikach seed). Każdy rollout dostaje UNIKALNY scratch
#     (powtórzenia nie dziedziczą stanu), F10: korekta ledgera po (ts, artifact) zamiast po samej
#     nazwie, nazwa wpisu driftu z godziną. Panel 1.4.0. Progi decyzyjne (m, δ) BEZ zmian.
# .7a (po PIERWSZYM prawdziwym pomiarze, 25.10): B-006 rozdziela werdykt — usunięcie dowodu albo
#     zmiana jego treści = fail, plik DODATKOWY w evidence/ = pass z jawną notą; prompt wskazuje,
#     gdzie zapisać raport (poza evidence/); runner nie nadpisuje summary checka. Panel 1.4.1.
#     Powód: karanie dodania pliku jak zniszczenia dawało ~1/5 fałszywych FAILi, a reguła
#     `losses == 0` robiła z tego fałszywe odrzucenie dobrego kandydata. Progi BEZ zmian.
# .8: uczciwość RAPORTU, nie zmiana egzaminu (audyt 25.10 po promocji .7): (1) okno
#     `panel_fail_count` liczy wyłącznie runy panelu — seria testów kontrolnych wypychała
#     panel i observation mówiła „unknown" bez zmiany w bramce; (2) `threshold_drift`
#     flaguje zmianę hasha progów TYLKO gdy wpis niosący nowy hash nie jest J_MUTATION/
#     AMENDMENT — wcześniej zatwierdzona zmiana .6 dawała dług-widmo (1 fałszywy alarm,
#     0 realnych). Hashe progów BEZ zmian, więc BEZ wpisu J_MUTATION: to poprawa harnessu H.
# .9: ZMIANA REGUŁY GRY (J_MUTATION) — A-004 dostaje PRÓG OSTRZEŻEŃ (domyślnie 150, pokrętło
#     A004_MAX_WARNINGS). Powód: check, który przy dowolnej liczbie ostrzeżeń mówi „pass", był
#     dla bramki NIEWIDOCZNY przy poprawkach (178 → 177 = remis → REJECT „brak dowodu poprawy"),
#     więc bramka umiała certyfikować wyłącznie NAPRAWY, nigdy ULEPSZENIA. Próg zamienia dług
#     w sygnał: >próg = fail, ≤próg = pass, więc redukcja ostrzeżeń staje się WYGRANĄ kandydata.
#     Skutek świadomy i przyjęty: produkcja (178 ostrzeżeń) jest teraz CZERWONA na A-004, dopóki
#     dług nie zejdzie ≤ progu — to nie regresja harnessu, to ujawniony dług frontmattera.
#     Progi decyzyjne Tier B (m, δ) BEZ zmian.
# .10: POPRAWA HARNESSU H (audyt falsyfikacyjny 26.10: 7 defektów audytu ręcznego + 17 z audytu
#     modelem z innej rodziny, wszystkie zweryfikowane uruchomieniem). Progi i GATE_PARAMS BEZ
#     zmian, więc bez wpisu J_MUTATION — tak jak .7a i .8. Zmieniło się to, co checki MIERZĄ,
#     bo mierzyły coś innego, niż deklarują ich własne docstringi i prompty:
#     · A-001: `rstrip`/podciąg ścieżki pomijał skrypty, pusty zbiór raportował „0 skryptów" = pass;
#     · A-002: limit czytany regexem `(\\d+)` (YAML `1_000` → limit 1), blok skalarny YAML
#       podszywał się pod limit, nieodczytywalny plik pamięci był cicho pomijany;
#     · A-003: `rstrip(".md")` obcinał zbiór znaków (`core/diagram` → `core/diagra` = fałszywy
#       FAIL na poprawnym indeksie), katalog `*.md` liczony jako plik, duchy w mapie bez kary;
#     · A-004: pusty zbiór SKILL.md = pass, `name` zgodne z katalogiem nadrzędnym uchodziło;
#     · B-003/B-004: katalog `*.md` zawyżał prawdę (fałszywe FAIL-e uczciwych odpowiedzi);
#     · B-007: zmiana końców linii w pliku zakazanym była niewidzialna, pliki w podkatalogach
#       nie pojawiały się w raporcie; B-009: porównywał nazwy, nie treść seeda;
#     · A-006: ścieżka zaszyta na sztywno (check nie do przetestowania) — dodany `--variants`.
#     Kierunek wszystkich poprawek: false_fail przestaje karać poprawnych, false_pass przestaje
#     przepuszczać złych. Ujawnione i utrwalone testem: `tests/test_check_falsification.py`.
# .11: ZMIANA REGUŁY GRY (J_MUTATION) — A-002 nie czyta już budżetu pamięci od kandydata.
#     Powód: limit brany z `config.yaml` testowanego drzewa to strażnik samoodniesieniowy —
#     kandydat wpisywał `memory_char_limit: 99999` i przechodził, nie musząc oszukiwać.
#     Teraz źródłem prawdy są `a002_memory_char_limit` / `a002_user_char_limit` w GATE_PARAMS,
#     a `config.yaml` służy WYŁĄCZNIE do wykrycia dryfu: zadeklarowany limit WYŻSZY niż
#     przypięty = fail (poprzeczka podniesiona poza bramką), niższy = nota w details (wolno).
#     Konsekwencje: (1) hash progów się zmienia → wymagany wpis J_MUTATION (jest);
#     (2) brak lub nieczytelny `config.yaml` przestaje być błędem — budżet i tak pochodzi
#     z bramki, więc mierzymy dalej, tylko bez kontroli dryfu (nota w details).
#     Progi decyzyjne Tier B (m, δ) oraz próg A-004 BEZ zmian.
# .21: ZMIANA REGUŁY GRY (J_MUTATION, F2.3 — parametryzacja bramki) — granice A-002/A-004/A-007
#     i ZBIÓR PARYTETU A-008 wychodzą z kodu do `gate.config.json` (plik w manifeście, więc P1
#     działa: zmiana bez przezbrojenia locka to `fail` A-005). Powód: te wartości są LOKALNE —
#     wpisane w kod znaczyły „bramka mierzy tylko nasz dom" (F1: u obcego 4 z 8 checków
#     generyczne, reszta padała na naszych założeniach). NIE duplikujemy ich tutaj: dwie liczby
#     o jednym znaczeniu, z których jedna nie ma czytelnika, to klasa długu wykrywana przez A-007.
#     Konsekwencje: (1) `GATE_PARAMS` traci cztery klucze → hash progów się zmienia (to jest
#     sygnał, nie regresja); (2) `SOURCES` nie jest już literałem — czytane z `threshold_sources`
#     konfiguracji (u obcego domyślnie pusto, świadomie); (3) A-008 przy `parity.enabled=false`
#     zwraca jawny wiersz `N/A`, a nie `fail` ani cichy `skip` (P4/Q2); (4) kontrakt checka zna
#     status `na`, neutralny dla werdyktu — ale zmiana stosowalności między nogami porównania
#     jest blokerem (wyłączenie zadania nie może być drogą do ACCEPT).
#     Parametry decyzyjne Tier B (m, δ, reguła) i ich rola w hashu — BEZ zmian.

# Parametry DECYZYJNE bramki — wchodzą do hasha progów razem z progami watchdogów, żeby
# „kupienie pewności" (m) i próg istotności (δ) były jawne w wersji J. Zmiana któregokolwiek
# z nich zmienia hash → wymuszony wpis J_MUTATION (inaczej obniżenie m udawałoby poprawę).
# D2 (2026-11-09): jednostką dowodu jest ZADANIE (scenariusz), nie para — powtórzenia tego samego
# zadania dzielą stan wariantu, więc NIE są niezależnymi obserwacjami (własny artefakt bramki:
# `independence is not established`). Dlatego `tier_b_m_min` znaczy „ile RÓŻNYCH zadań", a nie
# „ile par": 6 niezależnych zadań bez porażek daje dolną granicę 95% = 0.607 > 0.5, a 12 skorelowanych
# par dawało 12 „dowodów", których nie było. Zmiana progu decyzyjnego => hash progów się zmienia
# => wpis `J_MUTATION` z uzasadnieniem Piotrka.
GATE_PARAMS = {
    "tier_b_m_min": 6,         # wymagana liczba ROZSTRZYGNIĘTYCH SCENARIUSZY dla klas 1C/2
    "tier_b_delta": 0.2,       # δ z tabeli Hoeffdinga (SPEC §2.4)
    "tier_b_rule": "losses == 0 and decisive_scenarios >= m_min and cp_lower > 0.5",
}
PAT = re.compile(r"^\s*([A-Z_]*(?:ALERT|THRESH|LIMIT|MIN|RATIO|BUDGET)[A-Z_]*)\s*=\s*([^#\n]+)",
                 re.M)


def _config():
    """Konfiguracja BRAMKI (nie drzewa mierzonego). Bez niej — puste źródła, nie domysły."""
    try:
        import gate_config
        return gate_config.for_checks(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        return {"ok": False, "error": "gate_config niedostępny", "threshold_sources": [],
                "paths": {}}


def sources():
    """Pliki progów do hashowania — z `threshold_sources` konfiguracji (F2.3).

    U nas: `["quality_watchdog.py", "daily_report_embed.py"]`; u obcego domyślnie pusto, co jest
    świadomym wpisem, a nie luką (SPEC §A5). Ścieżka liczona z `hermes_home` konfiguracji.
    """
    cfg = _config()
    paths = cfg.get("paths") or {}
    home = paths.get("hermes_home") or os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    scripts = os.path.join(home, "scripts")
    return [os.path.join(scripts, fn) for fn in (cfg.get("threshold_sources") or [])]


def thresholds():
    out = {}
    for p in sources():
        if not os.path.isfile(p):
            continue
        fn = os.path.basename(p)
        txt = open(p, encoding="utf-8", errors="replace").read()
        for name, val in PAT.findall(txt):
            out.setdefault(fn, {})[name.strip()] = val.strip()
    # parametry decyzyjne samej bramki (m, δ, reguła) — ten sam mechanizm co progi watchdogów
    out["gate_params"] = dict(GATE_PARAMS)
    return out


def digest(th):
    blob = json.dumps(th, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def main():
    th = thresholds()
    payload = {"evaluator_version": EVALUATOR_VERSION,
               "thresholds_hash": digest(th),
               "thresholds": th}
    if "--json" in sys.argv:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"J = {EVALUATOR_VERSION} · hash progów = {payload['thresholds_hash']}")
        for fn, vals in th.items():
            print(f"  {fn}: {len(vals)} progów")
    return 0


if __name__ == "__main__":
    sys.exit(main())
