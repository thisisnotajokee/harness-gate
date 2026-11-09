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
