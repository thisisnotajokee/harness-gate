"""Werdykt nie może KŁAMAĆ o Tier B (2026-11-09).

Zmierzone na kontroli pozytywnej: artefakt `kontrola-pozytywna-B003` niósł w bloku `tier_b`
**dwie wygrane pary** (`incumbent=fail`, `kandydat=pass`), a nagłówek mówił „brak wygranych —
brak dowodu poprawy (remis/identyczny wynik)". Powód: klasa 1B nie wlicza Tier B do werdyktu
i fallback opisywał wyłącznie Tier A, ale jego treść czyta się jak wyrok o CAŁYM przebiegu.
Artefakt, który każe czytać „brak wygranych" przy istniejących wygranych, jest defektem raportu —
dokładnie ta klasa, którą bramka ma wykrywać u innych.
"""

import os
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import harness_eval


def para(task, inc, cand):
    return {"task": task, "incumbent": inc, "kandydat": cand,
            "inc_summary": "", "cand_summary": ""}


class WerdyktNieKlamieOTierBTest(unittest.TestCase):

    def _verdict(self, comp, tier_b=None, klasa="1B"):
        return harness_eval.verdict(comp, klasa, tier_b)

    def test_naglowek_nie_mowi_brak_wygranych_gdy_tier_b_ma_wygrane(self):
        tier_b = {"pairs": [para("B-003", "fail", "pass")], "infra_errors": []}
        v, r = self._verdict({"wins": [], "losses": [], "blockers": []}, tier_b)
        self.assertEqual(v, "REJECT")
        self.assertIn("Tier A bez wygranych", r, r)
        self.assertIn("Tier B ZMIERZONY osobno", r, r)
        self.assertIn("wygrane=1", r, r)

    def test_bez_tier_b_naglowek_zostaje_krotki(self):
        v, r = self._verdict({"wins": [], "losses": [], "blockers": []}, None)
        self.assertEqual(v, "REJECT")
        self.assertNotIn("Tier B ZMIERZONY", r, r)

    def test_chwiejne_zadania_sa_wymienione(self):
        tier_b = {"pairs": [para("B-006", "fail", "pass"), para("B-006", "pass", "pass")],
                  "infra_errors": []}
        v, r = self._verdict({"wins": [], "losses": [], "blockers": []}, tier_b)
        self.assertIn("chwiejne", r, r)
        self.assertIn("B-006", r, r)


if __name__ == "__main__":
    unittest.main()
