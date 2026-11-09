# Zestaw testów — co jest w wydaniu, a co nie

Ten katalog zawiera **testy mechanizmu**: rzeczy, które muszą być prawdziwe u każdego, kto
uruchomi bramkę na swoim drzewie.

| plik | co mierzy |
|---|---|
| `test_cli_contract.py` | kontrakt CLI runnera (flagi, kody wyjścia, werdykty) |
| `test_gate_config.py` | trzy stany `gate.config.json` (brak / poprawny / zepsuty) i fail-closed |
| `test_install.py` | instalator i dowiązanie `harness-gate` kończą się działającą bramką |
| `test_integrity_guard.py` | strażnik manifestu A-005 i kotwica Git |
| `test_evidence_pipeline.py` | dowód nieleksykalny: ślad wykonania i zamrożony stan |
| `test_consumer_paths.py` | nikt nie woła pojedynczego checka po ścieżce — tylko entrypoint |
| `test_f6_tierb_infra.py` | infrastruktura Tier B: bind w sandboxie, wykrywanie poświadczeń, porty |
| `test_example_pack.py` | przykładowa paczka: trzy statusy (`na` / `pass` / `fail`) na prawdziwym kontrakcie |

## Czego tu NIE ma i dlaczego

Zestaw **autorski** (nie wchodzi do wydania) zawiera dodatkowo testy, które mierzą profil autora
i jego **paczki referencyjne**: `test_admission_regressions.py`, `test_anchor_freshness.py`,
`test_check_falsification.py`, `test_tierA_falsification.py`, `test_tierb_truth.py`. W świeżym
drzewie nie mają czego mierzyć — odwołują się do kontroli spoza `checks/core/` i do profilu
w `gate.config.json`, którego wydanie nie zawiera. Trzymanie ich tutaj dawałoby **czerwony
zestaw w wydaniu „na zielono"**, czyli kłamałoby o stanie projektu.

W kilku miejscach testy mechanizmu mają pojedyncze przypadki zależne od profilu/paczki — są
oznaczone `@unittest.skipUnless(...)` z powodem, więc świeże drzewo je pomija, a drzewo
z paczką/proflem wykonuje.

Uruchomienie zestawu:

```sh
cd tests && python3 -m unittest discover -p 'test_*.py'
```
