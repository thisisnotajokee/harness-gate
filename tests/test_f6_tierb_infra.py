#!/usr/bin/env python3
"""test_f6_tierb_infra.py — infrastruktura Tier B: sandbox (bind), poświadczenia, porty.

Powstało z F6 (2026-11-09), gdy smoke pokazał, że Tier B nie jest podłączony, a jego awarie
MASKOWAŁY SIĘ jako wynik kandydata. Każdy test pinuje jeden konkretny, cichy defekt:

* `test_bind_na_dozwolonym_porcie_dziala` — sandbox ograniczał BIND (był w `handled`), ale
  przyznawał tylko CONNECT, więc **bind był zabroniony na każdym porcie**. Niewidoczne dopóki
  `hermes -z` nie dźwigał bramki; potem każdy rollout padał na `Gateway draining: not_ready`.
* `test_zgadywanie_gdy_brak_tabeli` / `test_tabela_hermesa_*` — kształt poświadczenia pochodzi
  z tabeli Hermesa, nie z naszego domysłu (`huggingface` -> `HF_TOKEN`, nie `*_API_KEY`).
* `prepare_variant_config` — bramka musi wstać na PRZYPIĘTYM porcie, a pośrednik musi być
  wpisany w konfigurację wariantu; własny blok `providers:` = odmowa, nie ciche doklejenie.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import tier_b  # noqa: E402

SANDBOX = BASE / "sandbox_landlock.py"


def _run_sandbox(allowed_ports, code):
    cmd = [sys.executable, str(SANDBOX)]
    rw = tempfile.mkdtemp(prefix="gate-sandbox-test-")
    cmd += ["--rw", rw]
    for p in allowed_ports:
        cmd += ["--allow-net-port", str(p)]
    cmd += ["--quiet", "--", sys.executable, "-c", code]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


class SandboxBindTest(unittest.TestCase):
    """Landlock: co jest `handled`, a nie ma reguły — jest ZABRONIONE. Bind też."""

    def test_bind_na_dozwolonym_porcie_dziala(self):
        port = 8765
        code = (f"import socket;s=socket.socket();s.bind(('127.0.0.1',{port}));s.close();"
                f"print('bind ok')")
        p = _run_sandbox([port], code)
        self.assertEqual(p.returncode, 0, f"bind na dozwolonym porcie odrzucony: {p.stderr[-300:]}")
        self.assertIn("bind ok", p.stdout)

    def test_bind_na_niedozwolonym_porcie_odrzucony(self):
        code = ("import socket;s=socket.socket();s.bind(('127.0.0.1',8766));s.close();"
                "print('bind ok')")
        p = _run_sandbox([8765], code)
        self.assertNotEqual(p.returncode, 0, "bind na porcie poza listą MUSI być odrzucony (fail-closed)")


class CredentialDiscoveryTest(unittest.TestCase):
    """Kształt poświadczenia pytamy HERMESA — inaczej każdy dostawca OAuth psuje się jak nasz nous."""

    def test_zgadywanie_gdy_brak_tabeli(self):
        with mock.patch.object(tier_b, "_host_checkout", return_value=""):
            self.assertEqual(tier_b.provider_credentials("deepseek"), (("DEEPSEEK_API_KEY",), "", False))
            # Znaczenie testu: to jest ZGADYWANIE i tak jest oznaczone (`znany=False`),
            # bo dla `huggingface` byłoby FAŁSZYWE (prawdziwa zmienna to `HF_TOKEN`).
            self.assertEqual(tier_b.provider_credentials("huggingface"), (("HUGGINGFACE_API_KEY",), "", False))

    def test_tabela_hermesa_kluczowe_i_oauth(self):
        if not tier_b._host_checkout():
            self.skipTest("brak checkoutu Hermesa — tabeli dostawców nie ma skąd wziąć")
        zmienne, base, znany = tier_b.provider_credentials("deepseek")
        self.assertTrue(znany)
        self.assertIn("DEEPSEEK_API_KEY", zmienne)
        self.assertEqual(base, "DEEPSEEK_BASE_URL")
        # Dostawca OAuth: znany, ale BEZ zmiennej z kluczem — jedyną drogą jest `hermes proxy`.
        zmienne_o, _b, znany_o = tier_b.provider_credentials("nous")
        self.assertTrue(znany_o)
        self.assertEqual(zmienne_o, ())

    def test_port_posrednika_z_adresu(self):
        self.assertEqual(tier_b._port_from_url("http://127.0.0.1:8645/v1"), "8645")
        self.assertEqual(tier_b._port_from_url("https://proxy.example/v1"), "443")
        self.assertEqual(tier_b._port_from_url(""), "")


class DiagnostykaAwariiTest(unittest.TestCase):
    """Robi 1 (2026-11-09): noga `infra_error` musi nieść PRZYCZYNĘ, nie tylko `rc`.

    Stan wyjściowy: `run_task` zbierał `stderr_tail`/`stdout_tail`, ale `paired()` je odrzucało,
    więc w artefakcie zostawało `rc=1` po 60,7 s i nic więcej — a awaria jest przejściowa, więc
    „uruchom jeszcze raz i patrz" jej nie odtworzy. Redakcja jest warunkiem ZAPISU: piaskownica
    dostaje klucz modelu, a komunikat błędu potrafi go powtórzyć.
    """

    @staticmethod
    def _atrapy_sekretow():
        """Atrapy budowane W LOCIE — w źródle nie może leżeć literał w kształcie klucza.

        Dwa niezależne skanery słusznie blokują taki literał: `secretscan.sh` w kotwicy (blokuje
        push) i skan wycieków wydania (blokuje build). One nie odróżnią atrapy od prawdziwego klucza
        — i nie powinny. Sklejanie w runtime zachowuje test (kształt powstaje dopiero w pamięci),
        a ze źródła znika materiał, który wygląda jak poświadczenie.
        """
        return ["sk-" + "a" * 20, "eyJ" + "b" * 30 + "." + "c" * 20, "y" * 44]

    def test_redakcja_usuwa_wzorce_i_zachowuje_tresc(self):
        import evidence
        tajne = self._atrapy_sekretow()
        tekst = ("Blad 401: klucz " + tajne[0] + "\nAuthorization: Bearer " + tajne[1] +
                 "\napi_key: " + tajne[0] + "\ndlugi=" + tajne[2] + "\nkoniec bez zmian")
        wynik = evidence.redact(tekst)
        for t in tajne:
            self.assertNotIn(t, wynik, "wzorzec nie został zredagowany")
        self.assertIn("Blad 401", wynik)
        self.assertIn("koniec bez zmian", wynik)

    def test_redakcja_dokladnych_wartosci(self):
        import evidence
        wartosc = "tajne" + "-" + "haslo" + "-" + "1" * 6          # sklejane: patrz _atrapy_sekretow
        wynik = evidence.redact(f"k: {wartosc}, dalej tekst", extra_values=(wartosc,))
        self.assertNotIn(wartosc, wynik)
        self.assertIn("dalej tekst", wynik)

    def _leg_awarii(self):
        return {"status": "infra_error", "rc": 1, "wall_s": 60.7,
                "stderr_tail": f"Blad providera 401: klucz {self._atrapy_sekretow()[0]} odrzucony",
                "stdout_tail": ""}

    def test_ogon_awarii_zapisuje_zredagowany_slad(self):
        with mock.patch.object(tier_b, "EVAL", tempfile.mkdtemp(prefix="eval-test-")):
            wpis = tier_b.ogon_awarii(self._leg_awarii(), "B-900", "kandydat")
            self.assertTrue(os.path.isfile(wpis["plik"]), wpis)
            tresc = open(wpis["plik"], encoding="utf-8").read()
        self.assertIn("rc=1", tresc, "ślad musi nieść kod wyjścia")
        self.assertIn("401", tresc, "ślad musi nieść treść błędu (po to istnieje)")
        atrapa = self._atrapy_sekretow()[0]
        self.assertNotIn(atrapa, tresc, "klucz NIE MOŻE trafić na dysk")
        self.assertNotIn(atrapa, wpis["ogon"], "…ani do artefaktu")

    def test_rodzaje_awarii_z_sygnalow_zaobserwowanych(self):
        """`infra_error` mówi „to nie kandydat", ale nie mówi GDZIE IŚĆ — to robi `rodzaj`."""
        self.assertEqual(tier_b.rodzaj_awarii(124), "timeout")
        self.assertEqual(tier_b.rodzaj_awarii(1, "", "Gateway draining: not_ready"), "sandbox")
        self.assertEqual(tier_b.rodzaj_awarii(1, "", "sandbox_landlock: Permission denied (port)"),
                         "sandbox")
        self.assertEqual(tier_b.rodzaj_awarii(0, "", "", transport="billing exhausted"), "provider")
        self.assertEqual(tier_b.rodzaj_awarii(1, "", "cos dziwnego"), "brak_startu",
                         "niepewność nazywamy wprost, nie udajemy wiedzy")

    def test_marker_dostawcy_wyprzedza_limit_czasu(self):
        """Gdy w wyjściu JEST przyczyna (marker), jest cenniejsza niż nasz limit czasu."""
        self.assertEqual(tier_b.rodzaj_awarii(124, "", "502", transport="502 bad gateway"), "provider")

    def test_paired_niesie_rodzaj_w_diagnostyce(self):
        task = {"id": "B-901", "check": "checks/core/tierb_B-002-service-check.py", "prompt": "x"}
        leg = {"status": "infra_error", "rc": 124, "wall_s": 420.0,
               "stdout_tail": "", "stderr_tail": "przekroczono limit czasu", "infra_kind": "timeout"}

        def fake_run_task(t, root, label, timeout=420):
            return leg if label == "kandydat" else {"status": "pass", "summary": "ok", "wall_s": 1.0,
                                                   "rc": 0, "stdout_tail": "", "stderr_tail": ""}

        with mock.patch.object(tier_b, "EVAL", tempfile.mkdtemp(prefix="eval-test-")), \
             mock.patch.object(tier_b, "run_task", side_effect=fake_run_task):
            wynik = tier_b.paired("/inc", "/cand", [task], repeats=1)
        diag = wynik["pairs"][0]["infra_diag"]["kandydat"]
        self.assertEqual(diag["rodzaj"], "timeout")
        self.assertTrue(os.path.isfile(diag["plik"]))

    def test_uzasadnienie_werdyktu_niesie_rodzaje(self):
        """Operator ma dostać „timeout: 1", a nie tylko „powtórz serię"."""
        import harness_eval
        tb = {"pairs": [{"task": "B-900", "incumbent": "pass", "kandydat": "infra_error",
                         "infra_error": ["B-900:kandydat"],
                         "infra_diag": {"kandydat": {"rodzaj": "timeout", "rc": 124}}}],
              "infra_errors": ["B-900:kandydat"]}
        werdykt, powod = harness_eval.tier_b_verdict(tb, {"tier_b_m_min": 6, "tier_b_delta": 0.2})
        self.assertEqual(werdykt, "INFRA_ERROR")
        self.assertIn("timeout: 1", powod, powod)

    def test_paired_zanotowuje_diagnostyke_tylko_dla_nogi_awarii(self):
        task = {"id": "B-900", "check": "checks/core/tierb_B-002-service-check.py", "prompt": "x"}

        def fake_run_task(t, root, label, timeout=420):
            if label == "kandydat":
                return self._leg_awarii()
            return {"status": "pass", "summary": "ok", "wall_s": 1.0, "rc": 0,
                    "stdout_tail": "", "stderr_tail": ""}

        with mock.patch.object(tier_b, "EVAL", tempfile.mkdtemp(prefix="eval-test-")), \
             mock.patch.object(tier_b, "run_task", side_effect=fake_run_task):
            wynik = tier_b.paired("/inc", "/cand", [task], repeats=1)
        para = wynik["pairs"][0]
        self.assertEqual(para["infra_error"], ["B-900:kandydat"])
        self.assertIn("infra_diag", para, "artefakt musi nieść przyczynę, nie tylko fakt")
        self.assertIn("kandydat", para["infra_diag"])
        self.assertNotIn("incumbent", para["infra_diag"], "diagnostyka tylko dla nogi, która padła")
        self.assertNotIn(self._atrapy_sekretow()[0], json.dumps(para, ensure_ascii=False),
                         "klucz nie może wyciec do artefaktu")
        self.assertEqual(wynik["infra_errors"], ["B-900:kandydat"])
        self.assertEqual(wynik["decisive_scenarios"], 0, "noga infra nie jest dowodem w żadną stronę")


class SwiezyWariantTest(unittest.TestCase):
    """Świeży wariant musi być CZYTELNY dla sond Tier B (znalezione płatnym przebiegiem 2026-11-09).

    `make_variant` zakłada SQLite z poprawnym nagłówkiem, ale BEZ schematu — Hermes tworzy tabele
    `messages` dopiero przy pierwszym zapisie rolloutu, a punkt odcięcia śladu liczymy PRZED rollem.
    Sondy czytały tabelę bez tolerancji, więc **każde** zadanie z `trace_probe`/`evidence_probe`
    wywracało cały przebieg (`no such table: messages`) — po decyzji o wariantach, zaraz przed
    wydaniem tokenów. Ten test pilnuje, że sonda nie wybucha i że uszkodzona baza nadal jest błędem.
    """

    def _swieza_baza(self, root):
        import sqlite3
        db = os.path.join(root, "state.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE IF NOT EXISTS _gate_init (x INTEGER)")
        con.execute("DROP TABLE _gate_init")
        con.commit()
        con.close()
        return db

    def test_sondy_nie_wybuchaja_bez_schematu(self):
        import evidence
        root = tempfile.mkdtemp(prefix="swiezy-wariant-")
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        db = self._swieza_baza(root)
        self.assertEqual(evidence.last_message(db), 0, "brak tabeli = brak historii, nie wyjątek")
        self.assertEqual(evidence.trace_since(db, 0), [], "brak tabeli = brak wierszy")
        task = {"id": "B-001-cost-report", "trace_probe": True}
        self.assertEqual(tier_b.probe_first_id(task, db), 0,
                         "punkt odcięcia na świeżym wariancie to 0, nie wyjątek")

    def test_uszkodzona_baza_nadal_jest_bledem(self):
        """Tolerancja dotyczy WYŁĄCZNIE `no such table` — inaczej cicha baza udawałaby pustą."""
        import evidence
        root = tempfile.mkdtemp(prefix="zepsuta-baza-")
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        db = os.path.join(root, "state.db")
        with open(db, "wb") as fh:
            fh.write(b"to nie jest baza sqlite" * 40)
        with self.assertRaises(Exception):
            evidence.last_message(db)


class GatewayPortTest(unittest.TestCase):
    """Port bramki: przypięty (piaskownica wpuszcza tylko wymienione), ale musi być WOLNY."""

    def test_wybiera_pierwszy_wolny(self):
        import socket
        with mock.patch.object(tier_b, "GATEWAY_PORTS", ["8765", "8766"]):
            zajety = socket.socket()
            zajety.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            zajety.bind(("127.0.0.1", 8765))
            zajety.listen(1)   # bez listen() SO_REUSEADDR pozwala zbindować ten sam port
            try:
                self.assertEqual(tier_b.free_gateway_port("inc"), "8766",
                                 "zajęty port musi zostać pominięty — inaczej noga pada w 3 s na bindzie")
                self.assertEqual(tier_b.free_gateway_port("kandydat"), "8766")
            finally:
                zajety.close()

    def test_brak_wolnego_portu_zwraca_pusto(self):
        import socket
        with mock.patch.object(tier_b, "GATEWAY_PORTS", ["8767"]):
            zajety = socket.socket()
            zajety.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            zajety.bind(("127.0.0.1", 8767))
            zajety.listen(1)
            try:
                self.assertEqual(tier_b.free_gateway_port("inc"), "",
                                 "brak wolnego portu ma być JAWNY, nie cichy fallback na zajęty")
            finally:
                zajety.close()


class PrepareVariantConfigTest(unittest.TestCase):
    """Infrastruktura rolloutu nie może zmieniać tego, co porównujemy, ani po cichu psuć YAML-a."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.variant = Path(self.tmp.name) / "variant"
        self.variant.mkdir()
        (self.variant / "config.yaml").write_text("model:\n  default: deepseek/x\n", encoding="utf-8")

    def _body(self):
        return (self.variant / "config.yaml").read_text(encoding="utf-8")

    def test_wpina_webapp_na_przypietym_porcie(self):
        self.assertTrue(tier_b.prepare_variant_config(str(self.variant), "8646"))
        body = self._body()
        self.assertIn("webapp:", body)
        self.assertIn("port: 8646", body)
        self.assertIn("enabled: true", body)
        self.assertIn("model:\n  default: deepseek/x", body)   # nie ruszamy tego, co porównujemy

    def test_idempotentne(self):
        tier_b.prepare_variant_config(str(self.variant), "8646")
        raz = self._body()
        self.assertTrue(tier_b.prepare_variant_config(str(self.variant), "8646"))
        self.assertEqual(raz, self._body(), "drugie wywołanie dopisało blok po raz drugi")

    def test_wlasny_blok_providers_to_odmowa(self):
        (self.variant / "config.yaml").write_text("providers:\n  moj:\n    base_url: http://x\n",
                                                  encoding="utf-8")
        with mock.patch.object(tier_b, "PROXY_URL", "http://127.0.0.1:8645/v1"):
            self.assertFalse(tier_b.prepare_variant_config(str(self.variant), "8646"),
                             "własny blok `providers:` musi dać odmowę, nie ciche doklejenie")

    def test_wpina_posrednika(self):
        with mock.patch.object(tier_b, "PROXY_URL", "http://127.0.0.1:8645/v1"), \
             mock.patch.object(tier_b, "PROXY_PROVIDER", "gate-proxy"), \
             mock.patch.object(tier_b, "PROXY_KEY_ENV", "GATE_PROXY_KEY"):
            self.assertTrue(tier_b.prepare_variant_config(str(self.variant), "8646"))
        body = self._body()
        self.assertIn("providers:", body)
        self.assertIn("gate-proxy:", body)
        self.assertIn("base_url: http://127.0.0.1:8645/v1", body)
        self.assertIn("key_env: GATE_PROXY_KEY", body)


class ModelEnvProxyTest(unittest.TestCase):
    """Pośrednik: sekret zostaje po jego stronie, rollout dostaje token-atrapę."""

    def test_token_atrapa_dla_posrednika(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".env").write_text("DEEPSEEK_API_KEY=tajne\n", encoding="utf-8")
            with mock.patch.object(tier_b, "PROXY_URL", "http://127.0.0.1:8645/v1"), \
                 mock.patch.object(tier_b, "PROXY_KEY_ENV", "GATE_PROXY_KEY"), \
                 mock.patch.object(tier_b, "ENV_WHITELIST", ("GATE_PROXY_KEY",)):
                keys = tier_b.model_env(env_path=str(Path(d, ".env")))
        self.assertEqual(keys.get("GATE_PROXY_KEY"), "gate-proxy-local")
        self.assertNotIn("tajne", str(keys), "prawdziwy sekret nie może wejść do piaskownicy")


if __name__ == "__main__":
    unittest.main()
