"""Strażnik manifestu niezależny od A-005 (F3).

A-005 jest checkiem w tym samym drzewie, więc podmiana jego pliku na stub wyłączała
strażnika i panel ogłaszał sukces na zepsutym systemie. `anchor_integrity_inline()`
żyje w runnerze i nie ufa wynikowi A-005: porównuje manifest z kotwicą Git i hash
samego checkera z manifestem. Te testy pinują to zachowanie.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import harness_eval  # noqa: E402

CHECKER = "checks/core/check_panel_integrity.py"


class AnchorInlineGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        self.base = self.home / "scripts" / "harness_eval"
        (self.base / "checks" / "core").mkdir(parents=True)
        self.checker = self.base / CHECKER
        self.checker.write_text("# oryginalny checker\n")
        self.lock = self.base / "panel.lock"
        files = {CHECKER: hashlib.sha256(self.checker.read_bytes()).hexdigest()}
        self.lock.write_text(json.dumps({"schema_version": 2, "files": files},
                                        sort_keys=True, indent=2) + "\n")
        subprocess.run(["git", "-C", str(self.base), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.base), "config", "user.email",
                        "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.base), "config", "user.name", "Test"],
                       check=True)
        subprocess.run(["git", "-C", str(self.base), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.base), "commit", "-qm", "baseline"],
                       check=True)
        self.paths = {"anchor_mode": "in-place", "anchor_repo": str(self.base),
                      "anchor_repo_path": "panel.lock"}

    def tearDown(self):
        self.temp.cleanup()

    def _run(self):
        with patch.object(harness_eval, "HERMES_HOME", str(self.home)), \
             patch.object(harness_eval, "PATHS", self.paths):
            return harness_eval.anchor_integrity_inline()

    def test_czysty_manifest_i_checker_przechodza(self):
        r = self._run()
        self.assertEqual(r["status"], "pass", r)

    def test_podmiana_a005_na_stub_jest_wykryta(self):
        """Podmiana A-005 bez przezbrojenia manifestu = TAMPERING, nie cicha zgoda."""
        self.checker.write_text('print("pass")\n')
        r = self._run()
        self.assertEqual(r["status"], "fail", r)
        self.assertEqual(r["code"], "TAMPERING_DETECTED", r)

    def test_manifest_poza_kotwica_jest_wykryty(self):
        """Manifest przeliczony po zmianie, ale niezacommitowany = LOCK_DRIFT."""
        self.checker.write_text('print("pass")\n')
        files = {CHECKER: hashlib.sha256(self.checker.read_bytes()).hexdigest()}
        self.lock.write_text(json.dumps({"schema_version": 2, "files": files},
                                        sort_keys=True, indent=2) + "\n")
        r = self._run()
        self.assertEqual(r["status"], "fail", r)
        self.assertEqual(r["code"], "LOCK_DRIFT", r)

    def test_brak_manifestu_jest_wykryty(self):
        os.unlink(self.lock)
        r = self._run()
        self.assertEqual(r["status"], "fail", r)
        self.assertEqual(r["code"], "LOCK_MISSING", r)

    def test_brak_kotwicy_nie_udaje_weryfikacji(self):
        """`anchor.mode=none` nie ma z czym porównać — jawne ANCHOR_NONE, nie ciche pass."""
        self.paths["anchor_mode"] = "none"
        r = self._run()
        self.assertEqual(r["status"], "pass", r)
        self.assertEqual(r["code"], "ANCHOR_NONE", r)

    def test_kotwica_external_z_gleboka_sciezka_w_repo(self):
        """Kotwica `external`: manifest na dysku jest w katalogu bramki, a w repo kotwicy leży
        pod `scripts/harness_eval/panel.lock`. Strażnik musi rozdzielić te dwie ścieżki — gdy
        składał je razem, szukał manifestu w `<bramka>/scripts/harness_eval/panel.lock`."""
        anchor_repo = Path(self.temp.name) / "brain"
        (anchor_repo / "scripts" / "harness_eval").mkdir(parents=True)
        shutil.copy2(self.lock, anchor_repo / "scripts" / "harness_eval" / "panel.lock")
        subprocess.run(["git", "-C", str(anchor_repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(anchor_repo), "config", "user.email",
                        "a@b.invalid"], check=True)
        subprocess.run(["git", "-C", str(anchor_repo), "config", "user.name", "T"], check=True)
        subprocess.run(["git", "-C", str(anchor_repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(anchor_repo), "commit", "-qm", "anchor"], check=True)
        self.paths.update({"anchor_mode": "external", "anchor_repo": str(anchor_repo),
                           "anchor_repo_path": "scripts/harness_eval/panel.lock"})
        r = self._run()
        self.assertEqual(r["status"], "pass", r)


class KontraktICzasTestów(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.base = self.home / "scripts" / "harness_eval"
        (self.base / "checks" / "core").mkdir(parents=True)
        self.paths = {"anchor_mode": "none", "anchor_repo": "", "anchor_repo_path": "panel.lock"}

    def tearDown(self):
        self.temp.cleanup()

    def _check(self, body):
        p = self.base / "checks" / "core" / "fake_check.py"
        p.write_text("import json, sys\n" + body)
        return p

    def _run(self, p):
        with patch.object(harness_eval, "HERMES_HOME", str(self.home)), \
             patch.object(harness_eval, "BASE", str(self.base)), \
             patch.object(harness_eval, "PATHS", self.paths):
            return harness_eval.run_check({"id": "X", "check": "checks/core/fake_check.py"},
                                          str(self.home))

    def test_skip_z_rc_1_to_niespelniony_kontrakt(self):
        """Kontrakt: rc=1 tylko dla `fail`. `skip` z rc=1 to kłamstwo checka, nie skip."""
        self._check('print(json.dumps({"status": "skip", "summary": "brak"}))\n'
                    'sys.exit(1)\n')
        r = self._run(None)
        self.assertEqual(r["status"], "fail", r)
        self.assertIn("kontrakt", r["summary"])

    def test_skip_z_rc_0_przechodzi(self):
        self._check('print(json.dumps({"status": "skip", "summary": "brak"}))\n')
        r = self._run(None)
        self.assertEqual(r["status"], "skip", r)


class PostCheckTests(unittest.TestCase):
    """TOCTOU: zmiana pliku w trakcie panelu musi zostać wykryta po ostatnim checku."""

    PANEL = {"panel_version": "t", "tasks": [
        {"id": "A-001-x", "tier": "A", "check": "checks/core/a.py"},
        {"id": "A-005-panel-integrity", "tier": "A",
         "check": "checks/core/check_panel_integrity.py"},
    ]}

    def _run(self, post_status, calls):
        def fake_run_check(task, root, timeout=180):
            calls.append(task["id"])
            if task["id"] == "A-005-panel-integrity" and calls.count("A-005-panel-integrity") == 2:
                return {"id": task["id"], "status": post_status, "summary": "post"}
            return {"id": task["id"], "status": "pass", "summary": "ok"}

        with patch.object(harness_eval, "merged_panel", lambda: (self.PANEL, [])), \
             patch.object(harness_eval, "run_check", fake_run_check), \
             patch.object(harness_eval, "pack_rows", lambda: []):
            return harness_eval.run_panel("/tmp", tier="A")

    def test_zdrowy_przebieg_nie_dokłada_wiersza(self):
        calls = []
        ids = [r["id"] for r in self._run("pass", calls)]
        self.assertEqual(ids, ["A-001-x", "A-005-panel-integrity"], ids)
        self.assertEqual(calls.count("A-005-panel-integrity"), 2, calls)

    def test_dryf_w_trakcie_panelu_dokłada_wiersz_fail(self):
        calls = []
        results = self._run("fail", calls)
        self.assertEqual(results[-1]["id"], "A-005-integrity-post", results)
        self.assertEqual(results[-1]["status"], "fail")


if __name__ == "__main__":
    unittest.main()
