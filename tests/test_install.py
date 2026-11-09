"""Instalator: świeża instalacja u „obcego" ma być ZIELONA jedną komendą (kryteria #11/#12).

Test uruchamia PRAWDZIWY `install.sh` w izolowanym domu (bez sudo, bez pytań), a potem
uruchamia zainstalowaną bramkę i sprawdza werdykt. Nie dotyka `~/.hermes`.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
REPO = BASE

CONFIG_YAML = ("model:\n  provider: nous\n  default: deepseek/deepseek-v4.1-flash\n"
               # `updates.check: false` NIE jest kosmetyką: `hermes --version` robi synchroniczny
               # passive update check (`_startup_fast.py` → `check_for_updates`), a cache świeżego
               # świata jest ZIMNY — więc każde sondowanie A-009 szło do GitHuba (zmierzone:
               # DNS + HTTPS do 140.82.121.5, 1,91 s; przy throttlingu >60 s → fałszywy FAIL A-009
               # w `test_install` i w budowie wydania). Świat testowy ma być lokalny i deterministyczny.
               "updates:\n  check: false\n")
SKILL = "---\nname: demo\ndescription: test\n---\n\n# Demo\n"


@unittest.skipUnless(shutil.which("git"), "git jest wymagany przez instalator")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / ".hermes"
        (self.home / "skills" / "demo").mkdir(parents=True)
        (self.home / "memories").mkdir(parents=True)
        (self.home / "skills" / "demo" / "SKILL.md").write_text(SKILL, encoding="utf-8")
        (self.home / "memories" / "MEMORY.md").write_text("# Pamięć\n", encoding="utf-8")
        (self.home / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
        import sqlite3
        sqlite3.connect(str(self.home / "state.db")).close()
        self.gate = self.home / "scripts" / "harness_eval"

    def tearDown(self):
        self.temp.cleanup()

    def _env(self):
        env = dict(os.environ)
        env.pop("HERMES_HOME", None)
        # Kontrakt CLI (A-009) mierzy BINARKĘ MASZYNY, nie treść świata: skoro podmieniamy `HOME`
        # dla instalacji, mówimy checkowi, gdzie naprawdę mieszka `hermes` (ta sama furtka co
        # w świecie pomiarowym; u zwykłego użytkownika niepotrzebna).
        env["HARNESS_GATE_MACHINE_HOME"] = os.path.expanduser("~")
        env["HOME"] = self.temp.name           # dowiązanie ląduje w izolowanym domu
        return env

    def _install(self, *extra):
        return subprocess.run(["sh", str(REPO / "install.sh"), *extra],
                              capture_output=True, text=True, env=self._env(), timeout=300)

    def _gate(self, *args):
        env = self._env()
        env["HERMES_HOME"] = str(self.home)
        return subprocess.run([sys.executable, str(self.gate / "harness_eval.py"), *args],
                              capture_output=True, text=True, env=env, timeout=300)

    def test_instalacja_bez_pytan_tworzy_zielona_bramke(self):
        r = self._install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.gate / "harness_eval.py").is_file(), "brak skopiowanej bramki")
        self.assertFalse((self.gate / "gate.config.json").exists(),
                         "instalator nie może kopiować NASZEJ konfiguracji")
        self.assertTrue((self.home / "eval" / "variants").is_dir(), "brak katalogu egzaminu")
        self.assertTrue((self.gate / ".git").is_dir(), "brak kotwicy Git")
        self.assertTrue((Path(self.temp.name) / ".local" / "bin" / "harness-gate").exists(),
                        "brak dowiązania w ~/.local/bin")
        g = self._gate("--panel", "--verbose")
        self.assertEqual(g.returncode, 0, g.stdout + g.stderr)
        self.assertIn('"fail": 0', g.stdout, g.stdout)
        self.assertIn('"skip": 0', g.stdout, g.stdout)

    def test_dowiazanie_uruchamia_bramke(self):
        """Dowiązanie w `~/.local/bin` ma DZIAŁAĆ, nie tylko istnieć.

        Regresja złapana 2026-11-09 przy pierwszym realnym użyciu: launcher liczył katalog
        z `$0`, a przez dowiązanie `$0` to ścieżka linku — szukał `~/.local/harness_eval.py`
        i kończył „nie znajduję bramki". Test sprawdzał tylko `exists()`, więc to przepuścił;
        u obcego użytkownika psuło to CAŁĄ instalację (`install.sh` zakłada właśnie dowiązanie).
        """
        self.assertEqual(self._install().returncode, 0)
        link = Path(self.temp.name) / ".local" / "bin" / "harness-gate"
        self.assertTrue(link.exists(), "brak dowiązania")
        env = self._env()
        env["HERMES_HOME"] = str(self.home)
        r = subprocess.run([str(link), "--panel"], capture_output=True, text=True,
                           env=env, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK", r.stdout, r.stdout)

    def test_niestandardowy_hermes_home_nie_dotyka_prawdziwego_local_bin(self):
        """Instalacja z `HERMES_HOME` poza `$HOME/.hermes` NIE MOŻE pisać do `$HOME/.local/bin`.

        Zmierzone 2026-11-09 na prawdziwym systemie: katalog dowiązania liczony z `$HOME`
        sprawił, że instalacja próbna w `/tmp` **przepięła produkcyjne** `~/.local/bin/harness-gate`
        na drzewo w `/tmp` — czyli jedna próba unieruchomiła działającą bramkę użytkownika.
        Reguła: dowiązanie ląduje przy DOMU BRAMKI (`dirname(HERMES_HOME)/.local/bin`).
        """
        obcy = Path(self.temp.name) / "deep" / "nested"        # ≠ `$HOME/.hermes`
        obcy.mkdir(parents=True)
        env = self._env()
        env["HERMES_HOME"] = str(obcy)
        r = subprocess.run(["sh", str(REPO / "install.sh")], capture_output=True, text=True,
                           env=env, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((Path(self.temp.name) / ".local" / "bin" / "harness-gate").exists(),
                         "instalator dotknął `$HOME/.local/bin` mimo niestandardowego HERMES_HOME")
        link = Path(self.temp.name) / "deep" / ".local" / "bin" / "harness-gate"
        self.assertTrue(link.is_symlink(), f"brak dowiązania przy domu bramki: {link}")
        self.assertIn(str(obcy / "scripts" / "harness_eval"), os.readlink(link))

    def test_nie_przepina_cudzego_dowiazania_bez_force(self):
        """Cudze, działające dowiązanie zostaje nietknięte — instalator mówi o tym wprost."""
        obcy = Path(self.temp.name) / "deep" / "nested"
        obcy.mkdir(parents=True)
        link_dir = Path(self.temp.name) / "deep" / ".local" / "bin"
        link_dir.mkdir(parents=True)
        cudzy = link_dir / "harness-gate"
        cudzy.symlink_to("/usr/bin/true")                      # „inna instalacja"
        env = self._env()
        env["HERMES_HOME"] = str(obcy)
        r = subprocess.run(["sh", str(REPO / "install.sh")], capture_output=True, text=True,
                           env=env, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(os.readlink(cudzy), "/usr/bin/true", "przepięto cudze dowiązanie")
        self.assertIn("inną instalację", r.stdout)

    def test_instalator_odmawia_nadpisania_bez_force(self):
        self.assertEqual(self._install().returncode, 0)
        r = self._install()
        self.assertNotEqual(r.returncode, 0, "drugie uruchomienie nadpisało bez --force")
        self.assertIn("--force", r.stdout + r.stderr)

    def test_zmiana_pliku_wymaga_fix_a_fix_naprawia(self):
        self.assertEqual(self._install().returncode, 0)
        target = self.gate / "checks" / "core" / "check_skill_frontmatter.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# zmiana\n", encoding="utf-8")
        g = self._gate("--panel")
        self.assertNotEqual(g.returncode, 0, "zmiana pliku bez przezbrojenia przeszła jako OK")
        f = self._gate("--fix")
        self.assertEqual(f.returncode, 0, f.stdout + f.stderr)
        g2 = self._gate("--panel")
        self.assertEqual(g2.returncode, 0, g2.stdout + g2.stderr)


if __name__ == "__main__":
    unittest.main()
