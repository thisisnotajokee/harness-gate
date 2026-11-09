"""Retencja środowisk zależności we `.shared` (wyciek zmierzony 2026-11-09: 84 envy / 15 GB).

Każde wywołanie Tier B materializuje nowy hash środowiska (~175 MB) i nic go nie prune'owało:
`prune_variants` patrzy tylko na katalogi wariantów, a `--clean` też ich nie ruszał. Test pilnuje
dwóch rzeczy naraz: (a) stare środowiska znikają, (b) NIE znikają rzeczy, które nie są envem
(plik, symlink) ani drzewo, którego nie ma — inaczej „sprzątanie" kasowałoby cudze dane.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))          # bramka może być uruchamiana także z katalogu `tests/`

import harness_eval


class PruneSharedEnvsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shared-envs-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        saved = harness_eval.VARIANTS
        harness_eval.VARIANTS = self.tmp
        self.addCleanup(lambda: setattr(harness_eval, "VARIANTS", saved))
        self.envs = os.path.join(self.tmp, ".shared", "installs", "abc", "environments")
        os.makedirs(self.envs)

    def _env(self, name, mtime, size=1024):
        p = os.path.join(self.envs, name)
        os.makedirs(p)
        with open(os.path.join(p, "f"), "wb") as fh:
            fh.write(b"x" * size)
        os.utime(p, (mtime, mtime))
        return p

    def test_keep_newest_and_remove_rest(self):
        for i in range(5):
            self._env(f"env{i}", 1000 + i)
        removed, freed = harness_eval.prune_shared_envs(keep=2)
        self.assertEqual(sorted(os.listdir(self.envs)), ["env3", "env4"])
        self.assertEqual(sorted(removed), ["env0", "env1", "env2"])
        self.assertGreater(freed, 0)

    def test_default_keeps_three(self):
        for i in range(6):
            self._env(f"e{i}", 1000 + i)
        removed, _ = harness_eval.prune_shared_envs()
        self.assertEqual(len(removed), 3)
        self.assertEqual(sorted(os.listdir(self.envs)), ["e3", "e4", "e5"])

    def test_fewer_than_keep_is_noop(self):
        self._env("only", 1000)
        removed, freed = harness_eval.prune_shared_envs(keep=3)
        self.assertEqual(removed, [])
        self.assertEqual(freed, 0.0)
        self.assertEqual(os.listdir(self.envs), ["only"])

    def test_plik_i_symlink_nie_sa_smieciem(self):
        for i in range(4):
            self._env(f"e{i}", 1000 + i)
        with open(os.path.join(self.envs, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("not an env")
        os.symlink(os.path.join(self.envs, "e3"), os.path.join(self.envs, "link"))
        harness_eval.prune_shared_envs(keep=1)
        left = sorted(os.listdir(self.envs))
        self.assertIn("notes.txt", left)
        self.assertIn("link", left)
        self.assertEqual([n for n in left if n.startswith("e") and n != "notes.txt"], ["e3"])

    def test_brak_drzewa_to_noop(self):
        harness_eval.VARIANTS = os.path.join(self.tmp, "nope")
        self.assertEqual(harness_eval.prune_shared_envs(keep=1), ([], 0.0))

    def test_wiele_installi_sprzatane_osobno(self):
        second = os.path.join(self.tmp, ".shared", "installs", "def", "environments")
        os.makedirs(second)
        for i in range(3):
            self._env(f"a{i}", 1000 + i)
        for i in range(3):
            p = os.path.join(second, f"b{i}")
            os.makedirs(p)
            os.utime(p, (1000 + i, 1000 + i))
        removed, _ = harness_eval.prune_shared_envs(keep=1)
        self.assertEqual(len(removed), 4)
        self.assertEqual(sorted(os.listdir(second)), ["b2"])


if __name__ == "__main__":
    unittest.main()
