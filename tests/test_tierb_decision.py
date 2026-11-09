"""Reguła decyzyjna Tier B na SCENARIUSZACH (O2) — bez rolloutów, bez tokenów.

Kontekst (2026-11-09): `resolved` liczył PARY, a powtórzenia tego samego zadania są skorelowane —
własny artefakt bramki mówił `independence is not established`, więc „12 rozstrzygnięć" znaczyło
mniej, niż brzmiało (3 zadania × 4 powtórzenia = 12 par przy 3 niezależnych obserwacjach).
Ten plik pinuje: jednostkę dowodu = ZADANIE, blokadę chwiejności i kolejność klas werdyktu
(przegrana przed awarią infrastruktury).
"""

import json
import os
import unittest

import harness_eval
import tier_b


def para(task, inc, cand, **extra):
    """Para w kształcie, jaki produkuje `tier_b.paired()`."""
    p = {"task": task, "incumbent": inc, "kandydat": cand}
    p.update(extra)
    return p


class ScenarioOutcomesTest(unittest.TestCase):
    """Dowód liczy się na ZADANIU, nie na powtórzeniu."""

    def test_skorelowane_powtorzenia_to_jeden_dowod(self):
        pary = [para("B-002", "pass", "pass") for _ in range(3)]
        roz, chw, rem = tier_b.scenario_outcomes(pary)
        self.assertEqual(roz, [])
        self.assertEqual(chw, [])
        self.assertEqual(rem, ["B-002"], "3 remisy z jednego zadania = 1 scenariusz")

    def test_chwiejne_zadanie_jest_raportowane(self):
        pary = [para("X", "pass", "fail"), para("X", "pass", "pass"), para("X", "fail", "pass")]
        roz, chw, rem = tier_b.scenario_outcomes(pary)
        self.assertEqual(chw, ["X"], "różne wyniki powtórzeń = defekt EGZAMINU")
        self.assertEqual(roz, [])

    def test_zgodne_powtorzenia_daja_jeden_wynik(self):
        pary = [para("Y", "fail", "pass") for _ in range(3)]
        roz, chw, rem = tier_b.scenario_outcomes(pary)
        self.assertEqual(roz, [("wygrana", "Y")])

    def test_noga_z_infra_error_wypada_z_dowodu(self):
        pary = [para("Z", "pass", "infra_error"), para("Z", "fail", "pass")]
        roz, chw, rem = tier_b.scenario_outcomes(pary)
        self.assertEqual((roz, chw, rem), ([], [], []),
                         "awaria kabli nie jest dowodem w żadną stronę")

    def test_wiersz_przerwania_nie_jest_para(self):
        roz, chw, rem = tier_b.scenario_outcomes([{"early_stop": "przerwano po 2 przegranych"}])
        self.assertEqual((roz, chw, rem), ([], [], []))


class CPLowerTest(unittest.TestCase):
    """Dwie niezależne drogi: bisekcja po ogonie dwumianu vs postać zamknięta α^(1/n).

    Pierwsza wersja tej funkcji (w planie) miała ODWRÓCONĄ bisekcję i zwracała bezsens
    (`cp_lower(5,5)=0.0`, `cp_lower(4,4)=1.0`) — wyglądało jak liczby. Dlatego test porównuje
    wynik z niezależnym wzorem, a nie z zapisaną stałą.
    """

    def test_zero_porazek_zgadza_sie_z_postacia_zamknieta(self):
        for n in range(1, 13):
            with self.subTest(n=n):
                self.assertAlmostEqual(tier_b.cp_lower(n, n, 0.95), 0.05 ** (1 / n), places=5)

    def test_wartosci_kontrolne(self):
        self.assertAlmostEqual(tier_b.cp_lower(5, 5, 0.95), 0.549280, places=5)
        self.assertAlmostEqual(tier_b.cp_lower(6, 6, 0.95), 0.606962, places=5)
        self.assertAlmostEqual(tier_b.cp_lower(4, 4, 0.95), 0.472871, places=5)

    def test_porazki_zbijaja_granice_ponizej_polowy(self):
        self.assertLess(tier_b.cp_lower(6, 7, 0.95), 0.5, "6/7 nie jest dowodem przewagi")
        self.assertLess(tier_b.cp_lower(5, 6, 0.95), 0.5)

    def test_brzegi(self):
        self.assertEqual(tier_b.cp_lower(0, 5, 0.95), 0.0)
        self.assertEqual(tier_b.cp_lower(0, 0, 0.95), 0.0)
        self.assertAlmostEqual(tier_b.cp_lower(1, 1, 0.95), 0.05, places=5)


class TierBVerdictTest(unittest.TestCase):
    """Reguła werdyktu — czysta funkcja, mierzona bez wydania tokenu."""

    PARAMS = {"tier_b_m_min": 6, "tier_b_delta": 0.2}

    def werdykt(self, pary, **extra):
        wynik = {"pairs": pary, "infra_errors": []}
        wynik.update(extra)
        return harness_eval.tier_b_verdict(wynik, self.PARAMS)

    def test_szesc_wygranych_scenariuszy_to_accept(self):
        pary = [para(f"T-{i}", "fail", "pass") for i in range(6)]
        v, r = self.werdykt(pary)
        self.assertEqual(v, "ACCEPT", r)
        self.assertIn("6/6", r)

    def test_piec_scenariuszy_to_za_malo(self):
        v, r = self.werdykt([para(f"T-{i}", "fail", "pass") for i in range(5)])
        self.assertEqual(v, "TIER_B_REQUIRED", r)

    def test_powtorzenia_nie_dobijaja_dowodu(self):
        """Sedno defektu: 1 zadanie × 6 powtórzeń dawało `resolved=6` i przechodziło próg."""
        pary = [para("B-002", "fail", "pass") for _ in range(6)]
        v, r = self.werdykt(pary)
        self.assertEqual(v, "TIER_B_REQUIRED", r)
        self.assertIn("1/6", r)

    def test_przegrana_wyprzedza_awarie_infrastruktury(self):
        pary = [para("A", "pass", "fail"), para("B", "pass", "infra_error")]
        v, r = self.werdykt(pary, infra_errors=["B"])
        self.assertEqual(v, "REJECT",
                         f"przegrana to dowód regresji, awaria to brak pomiaru; powód: {r}")

    def test_awaria_bez_przegranej_to_infra_error(self):
        v, r = self.werdykt([para("B", "pass", "infra_error")], infra_errors=["B"])
        self.assertEqual(v, "INFRA_ERROR", r)

    def test_chwiejne_zadanie_blokuje_accept(self):
        pary = ([para(f"T-{i}", "fail", "pass") for i in range(6)]
                + [para("X", "pass", "fail"), para("X", "fail", "pass")])
        v, r = self.werdykt(pary)
        self.assertEqual(v, "REJECT", r)
        self.assertIn("chwiejne", r.lower())

    def test_remis_nie_jest_dowodem_ale_nie_szkodzi(self):
        pary = ([para(f"T-{i}", "fail", "pass") for i in range(6)]
                + [para("R", "pass", "pass")])
        v, r = self.werdykt(pary)
        self.assertEqual(v, "ACCEPT", r)
        self.assertIn("6/6", r)

    def test_bez_par_nie_ma_dowodu(self):
        v, r = self.werdykt([])
        self.assertEqual(v, "TIER_B_REQUIRED", r)


class ArtifactRegressionTest(unittest.TestCase):
    """Dowód na przebiegu z 2026-11-09: 3 pary z 1 zadania to JEDEN remis, nie trzy dowody."""

    SCIEZKA = os.path.expanduser(
        "~/.hermes/eval/runs/20261109-055252-weryfikacja-chwiejnosci-B002-v2.json")

    def test_dzisiejszy_przebieg_nie_jest_dowodem(self):
        if not os.path.exists(self.SCIEZKA):
            self.skipTest("artefakt niedostępny w tym drzewie")
        with open(self.SCIEZKA, encoding="utf-8") as fh:
            t = json.load(fh)["tier_b"]
        roz, chw, rem = tier_b.scenario_outcomes(t["pairs"])
        self.assertEqual(roz, [], "3 pary z jednego zadania NIE są rozstrzygnięciem")
        self.assertEqual(chw, [], "zadanie nie było chwiejne — obie nogi `pass` w każdej parze")
        self.assertEqual(rem, ["B-002-service-check"])
        self.assertEqual(len(t["pairs"]), 3, "a mimo to par było 3 (to jest cały defekt)")
        v, r = harness_eval.tier_b_verdict(t, {"tier_b_m_min": 6, "tier_b_delta": 0.2})
        self.assertNotEqual(v, "ACCEPT", r)


if __name__ == "__main__":
    unittest.main()
