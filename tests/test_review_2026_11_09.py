#!/usr/bin/env python3
"""test_review_2026_11_09.py — naprawy po recenzji zewnętrznej (18 zarzutów, 2026-11-09).

Każdy test pinuje JEDEN konkretny zarzut, który został potwierdzony wobec naszego kodu —
i tylko te, które da się sprawdzić bez wydawania tokenów. Zarzuty odrzucone albo uznane za
nieistotne NIE mają tu testów (nie pinujemy czegoś, czego nie naprawiliśmy).

Numeracja zarzutów jak w recenzji: §1.1–1.4 (separacja infra/fail), §2.1 (A-005),
§2.4/§4.1 (B-002), §3.1 (TOCTOU), §4.2 (parowanie), §5.2 (duplikaty), §6 (przenośność).
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import harness_eval  # noqa: E402
import tier_b  # noqa: E402

CHECKS = BASE / "checks" / "core"


class PairPlanTest(unittest.TestCase):
    """§4.2: kolejność nóg musi być naprzemienna PER ZADANIE, nie per indeks planu."""

    def test_parzysta_liczba_zadan_odwraca_kolejnosc(self):
        tasks = [{"id": f"T{i}"} for i in range(6)]          # 6 = parzysta (nasz realny przypadek)
        plan = tier_b.pair_plan(tasks, 2)
        for k, t in enumerate(tasks):
            kolejnosc = [inc for r, kk, tt, inc in plan if kk == k]
            # Wymagamy ODWRÓCENIA, nie konkretnego porządku: pierwsze powtórzenie może zacząć
            # dowolna noga (parzystość indeksu zadania), byle drugie miało przeciwnie.
            self.assertNotEqual(kolejnosc[0], kolejnosc[1],
                                f"{t['id']}: kolejność nie odwróciła się między powtórzeniami "
                                f"(dokładnie ten defekt: i%2 przy parzystym K)")

    def test_nieparzysta_tez_naprzemienna(self):
        plan = tier_b.pair_plan([{"id": f"T{i}"} for i in range(5)], 2)
        for k in range(5):
            self.assertEqual([inc for r, kk, tt, inc in plan if kk == k], [k % 2 == 0, k % 2 != 0])

    def test_liczba_par_jak_zaplanowana(self):
        plan = tier_b.pair_plan([{"id": "A"}, {"id": "B"}], 3)
        self.assertEqual(len(plan), 6)


class CompareTest(unittest.TestCase):
    """§3.1: wykryte oszustwo nie może lądować w remisach."""

    def _rows(self, *pary):
        return [{"id": i, "status": s} for i, s in pary]

    def test_wiersz_fail_tylko_u_kandydata_to_przegrana(self):
        inc = self._rows(("A-001", "pass"))
        cand = self._rows(("A-001", "pass"), ("A-005-integrity-post", "fail"))
        c = harness_eval.compare(inc, cand)
        self.assertIn("A-005-integrity-post", c["losses"])
        self.assertNotIn("A-005-integrity-post", c["ties"])

    def test_integrity_post_blokuje(self):
        inc = self._rows(("A-001", "pass"))
        cand = self._rows(("A-001", "pass"), ("A-005-integrity-post", "fail"))
        c = harness_eval.compare(inc, cand)
        self.assertTrue([b for b in c["blockers"] if "A-005-integrity-post" in b], c["blockers"])

    def test_wiersz_pass_tylko_u_kandydata_zostaje_remisem(self):
        """Nie każdy wiersz jednostronny to przegrana — `pass` bez odpowiednika to brak porównania."""
        c = harness_eval.compare(self._rows(("A-001", "pass")),
                                 self._rows(("A-001", "pass"), ("A-00X", "pass")))
        self.assertIn("A-00X", c["ties"])
        self.assertEqual(c["losses"], [])

    def test_remis_pass_pass_i_na_na_bez_zmian(self):
        c = harness_eval.compare(self._rows(("A-001", "pass"), ("A-002", "na")),
                                 self._rows(("A-001", "pass"), ("A-002", "na")))
        self.assertEqual(c["losses"], [])
        self.assertEqual(sorted(c["ties"]), ["A-001", "A-002"])


class VerdictPriorityTest(unittest.TestCase):
    """§1.4: udowodniona regresja nie może zniknąć pod awarią infrastruktury."""

    def _comp(self):
        return {"wins": [], "losses": [], "ties": [], "blockers": [], "p_value": 1.0}

    def test_przegrana_wyzej_niz_awaria(self):
        # D1 (2026-11-09): dowód czytamy z PAR, nie z liczników — `wins`/`losses` bez `pairs` to
        # wejście niespójne i nowa reguła odmawia głośno. Tu świadomie podajemy pary.
        tb = {"pairs": [{"task": "B-001", "incumbent": "pass", "kandydat": "fail"},
                        {"task": "B-002", "incumbent": "pass", "kandydat": "infra_error"}],
              "losses": ["B-001"], "wins": [], "ties": [], "infra_errors": ["B-002:incumbent"],
              "resolved": 1}
        v, reason = harness_eval.verdict(self._comp(), "1C", tier_b=tb)
        self.assertEqual(v, "REJECT")
        self.assertIn("uwaga".lower(), reason.lower(), "brak ostrzeżenia o niepełnym przebiegu")

    def test_sama_awaria_to_infra_error(self):
        tb = {"losses": [], "wins": [], "ties": [], "infra_errors": ["B-002:incumbent"], "resolved": 0}
        v, _ = harness_eval.verdict(self._comp(), "1C", tier_b=tb)
        self.assertEqual(v, "INFRA_ERROR")


class TransportErrorTest(unittest.TestCase):
    """§1.1–1.3: czkawka dostawcy nie jest werdyktem o kandydacie (a pętla kandydata nie jest awarią)."""

    def test_markery_rozpoznane(self):
        for tekst in ("Error: HTTP 402: Insufficient Balance",
                      "⚠️ No usable credentials found for provider 'deepseek'",
                      "Error: Gateway draining: not_ready",
                      "requests.exceptions.ConnectionError: Connection reset by peer"):
            self.assertTrue(tier_b.transport_error(tekst, ""), tekst)

    def test_zdrowe_wyjscie_bez_markera(self):
        self.assertEqual(tier_b.transport_error("", "zapisano plik ok.md"), "")
        # Słowo z zadania nie może być markerem (stąd brak gołego „timeout" na liście).
        self.assertEqual(tier_b.transport_error("", "RAZEM: 0.00 USD, timeout ustawiono na 30"), "")


class A005SelectionTest(unittest.TestCase):
    """§2.1: własne drzewo bramki bez `panel.lock` to JEGO problem, nie powód, by mierzyć nasz lock."""

    def test_katalog_bramki_bez_locka_nie_daje_pass(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "scripts" / "harness_eval").mkdir(parents=True)   # własna bramka bez manifestu
            r = harness_eval.run_check(
                {"id": "A-005-panel-integrity", "check": "checks/core/check_panel_integrity.py"}, d)
        self.assertNotEqual(r["status"], "pass",
                            "kandydat z własnym katalogiem bramki i bez panel.lock dostał pass")


class MemoryDuplicatesTest(unittest.TestCase):
    """§5.2: duplikat klucza limitu to konfiguracja niejednoznaczna, nie „ostatni wygrywa"."""

    def _run(self, config_body):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / ".hermes"
            (home / "memories").mkdir(parents=True)
            (home / "config.yaml").write_text(config_body, encoding="utf-8")
            (home / "memories" / "MEMORY.md").write_text("x", encoding="utf-8")
            env = dict(os.environ)
            env["HARNESS_GATE_CONFIG"] = str(Path(d) / "brak.json")
            env["HERMES_HOME"] = str(home)
            p = subprocess.run([sys.executable, str(CHECKS / "check_memory_limits.py"),
                                "--home", str(home)], capture_output=True, text=True,
                               env=env, timeout=60)
        return p.returncode, json.loads((p.stdout or "{}").strip().splitlines()[-1])

    def test_duplikat_klucza_to_fail(self):
        rc, w = self._run("memory_char_limit: 2500\nmemory_char_limit: 999999\n")
        self.assertEqual(w["status"], "fail", w)
        self.assertEqual(rc, 1)
        self.assertIn("zduplikowany", json.dumps(w, ensure_ascii=False))

    def test_pojedynczy_klucz_nie_jest_duplikatem(self):
        rc, w = self._run("memory_char_limit: 2500\n")
        self.assertEqual(w["status"], "pass", w)


class PortabilityTest(unittest.TestCase):
    """§6: `HERMES_HOME` wygrywa także w modułach pomocniczych (nie tylko w runnerze)."""

    def _stala(self, modul, atrybut):
        kod = f"import {modul} as m; print(getattr(m, '{atrybut}'))"
        env = dict(os.environ)
        env["HERMES_HOME"] = "/tmp/dom-recenzja"
        p = subprocess.run([sys.executable, "-c", kod], capture_output=True, text=True,
                           cwd=str(BASE), env=env, timeout=60)
        return (p.stdout or "").strip()

    def test_moduly_idą_za_hermes_home(self):
        self.assertIn("/tmp/dom-recenzja", self._stala("ledger_check", "LEDGER"))
        self.assertIn("/tmp/dom-recenzja", self._stala("skill_drift", "SKILLS"))
        self.assertIn("/tmp/dom-recenzja", self._stala("skill_telemetry", "DB"))


class ProxyHealthTest(unittest.TestCase):
    """Pre-flight pośrednika: martwy `hermes proxy` kosztował cały budżet rolloutów (2026-11-09)."""

    def test_brak_posrednika_nie_jest_bledem(self):
        ok, why = tier_b.proxy_health("")
        self.assertTrue(ok, why)

    def test_martwy_adres_to_odmowa(self):
        ok, why = tier_b.proxy_health("http://127.0.0.1:8699/v1", timeout=1)
        self.assertFalse(ok, "martwy pośrednik musi dać odmowę, nie ciszę")
        self.assertIn("nie odpowiada", why)

    def test_zywy_serwer_przechodzi(self):
        import http.server, socketserver, threading
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":[]}')
            def log_message(self, *a):
                pass
        with socketserver.TCPServer(("127.0.0.1", 0), H) as srv:
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            ok, why = tier_b.proxy_health(f"http://127.0.0.1:{port}/v1", timeout=2)
            srv.shutdown()
        self.assertTrue(ok, why)


class LedgerStatusTest(unittest.TestCase):
    """§1.4: `INFRA_ERROR` to legalny wynik przebiegu — inaczej audyt zgłasza nieznany status."""

    def test_status_jest_dozwolony(self):
        import ledger_check
        self.assertIn("INFRA_ERROR", ledger_check.ALLOWED_STATUS)


def _load_check(nazwa):
    """Checki mają myślniki w nazwie (nie da się ich zaimportować normalnie)."""
    sciezka = CHECKS / nazwa
    spec = importlib.util.spec_from_file_location(nazwa.replace("-", "_").replace(".py", ""), sciezka)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class B002TruthOwnershipTest(unittest.TestCase):
    """§2.4: bramka NASZEGO wariantu nie może być „prawdą" o hoście."""

    def test_proces_wlasnego_wariantu_jest_wykryty(self):
        mod = _load_check("tierb_B-002-service-check.py")
        moj_dom = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        self.assertTrue(mod._wlasny_proces(str(os.getpid()), moj_dom),
                        "proces z HERMES_HOME wariantu nie został rozpoznany jako nasz")
        self.assertFalse(mod._wlasny_proces(str(os.getpid()), "/tmp/zupełnie-inny-dom"))

    def test_bez_wariantu_nic_nie_wykluczamy(self):
        mod = _load_check("tierb_B-002-service-check.py")
        self.assertFalse(mod._wlasny_proces(str(os.getpid()), ""))


if __name__ == "__main__":
    unittest.main()
