"""Kontrola pozytywna B-003 — czy check ROZRÓŻNIA brak skilla od jego obecności (0 tokenów).

Tło (2026-11-09): w 13 przebiegach Tier B parami nie pojawiła się ANI JEDNA wygrana, bo
kandydatem było zawsze to samo drzewo. Reguła werdyktu umie ACCEPT, ale ścieżka
„kandydat lepszy → wygrana" nie została wykonana. Ta kontrola ma ZNANĄ prawdę z konstrukcji:
incumbent bez skilla, kandydat z skillem.

Ten test pinuje ogniwo, które da się zmierzyć bez modelu: **kontrakt wyniku `skill_view`**.
Gdy skilla nie ma, narzędzie zwraca błąd (`success: false` / `error`) — i check MUSI to uznać
za brak aktywacji. Gdy skill jest, wynik niepusty i bez błędu — aktywacja zaliczona.
Bez tego pinowania „wygrana" w kontroli mogłaby się wziąć z czegoś innego niż brak skilla.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import evidence          # noqa: E402
import tier_b            # noqa: E402

TID = "B-003-skill-activation"


def zadanie():
    for t in tier_b.load_tier_b_tasks():
        if t["id"] == TID:
            return t
    return None


class KontrolaPozytywnaB003Test(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.task = zadanie()
        if cls.task is None:
            raise unittest.SkipTest(f"{TID}: zadanie niedostępne w tym drzewie (paczka domenowa)")
        with open(os.path.join(BASE, cls.task["check"]), encoding="utf-8") as fh:
            m = re.search(r'^SKILL\s*=\s*["\']([^"\']+)["\']', fh.read(), re.M)
        if not m:
            raise unittest.SkipTest(f"{TID}: check nie deklaruje stałej SKILL")
        cls.skill = m.group(1)

    def _swiat(self, wynik_skill_view, liczba_w_pliku=None):
        """Świat: seed zadania + odpowiedź `core_count.md` + ślad z podanym wynikiem `skill_view`."""
        root = tempfile.mkdtemp(prefix="posctl-b003-")
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        for rel, tresc in (self.task.get("seed") or {}).items():
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p) or root, exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(tresc)

        prawda = len([f for f in os.listdir(os.path.join(root, "kb", "core")) if f.endswith(".md")])
        liczba = prawda if liczba_w_pliku is None else liczba_w_pliku
        with open(os.path.join(root, "core_count.md"), "w", encoding="utf-8") as fh:
            fh.write(f"{liczba}\n")

        slad = [{"message_id": 1, "name": "skill_view",
                 "arguments": {"name": self.skill},
                 "result": json.dumps(wynik_skill_view, ensure_ascii=False)}]
        ev = os.path.join(root, "ev.json")
        with open(ev, "w", encoding="utf-8") as fh:
            json.dump({"baseline": None, "calls": slad, "trace_error": None}, fh)
        return root, ev

    def _check(self, root, ev):
        argv = tier_b.check_argv(self.task, root, root, evidence_path=ev)
        import subprocess
        p = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        return p.returncode, json.loads((p.stdout or "{}").strip().splitlines()[-1])

    def test_brak_skilla_wynik_bledu_jest_faila(self):
        """Skill nieobecny → `skill_view` zwraca błąd → check MUSI dać fail (to jest strona incumbenta)."""
        root, ev = self._swiat({"success": False,
                                "error": f"Skill '{self.skill}' not found"})
        rc, rep = self._check(root, ev)
        self.assertNotEqual(rc, 0, rep)
        self.assertEqual(rep["status"], "fail", rep)
        self.assertIn("skill_załadowany=False", rep["summary"])

    def test_obecny_skill_zaliczony(self):
        """Skill obecny + poprawna liczba → pass (strona kandydata)."""
        root, ev = self._swiat({"success": True, "name": self.skill,
                                "content": "# skill\n"})
        rc, rep = self._check(root, ev)
        self.assertEqual(rc, 0, rep)
        self.assertEqual(rep["status"], "pass", rep)

    def test_liczba_nie_ratuje_braku_aktywacji(self):
        """Poprawna liczba BEZ aktywacji to nadal fail — inaczej kontrola mierzyłaby arytmetykę."""
        root, ev = self._swiat({"success": False, "error": "not found"})
        rc, rep = self._check(root, ev)
        self.assertNotEqual(rc, 0, rep)


if __name__ == "__main__":
    unittest.main()
