#!/usr/bin/env python3
"""test_example_pack.py — przykładowa paczka na PRAWDZIWYM kontrakcie (trzy statusy).

Po co: `packs/example/` jest wzorcem do skopiowania, więc musi być sprawdzony tak samo
twardo jak rdzeń. Ten plik uruchamia jego kontrolę przez CLI (nie przez import funkcji —
kontrakt to linia JSON na stdout i kod wyjścia) i pinuje wszystkie trzy statusy:

* katalogu konwencji NIE MA → `na` (rc 0): konwencja nieużywana to jawne „nie dotyczy",
* katalog JEST, ale pusty → `fail` (rc 1): brak materiału to nie czystość,
* katalog z plikami → `pass` (rc 0).

Konfigurację podajemy przez `HARNESS_GATE_CONFIG`, a drzewo przez `--home`, więc test nie
zależy od żadnego pliku w drzewie — tak samo musi działać u odbiorcy.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CHECK = BASE / "packs" / "example" / "checks" / "check_example_conventions.py"


def _run(home, config_path):
    env = dict(os.environ)
    env["HARNESS_GATE_CONFIG"] = str(config_path)
    p = subprocess.run([sys.executable, str(CHECK), "--home", str(home)],
                       capture_output=True, text=True, timeout=60, env=env)
    linia = (p.stdout or "").strip().splitlines()[-1] if (p.stdout or "").strip() else "{}"
    try:
        wynik = json.loads(linia)
    except ValueError:
        wynik = {"status": "nieparsowalny", "raw": linia[:200]}
    return p.returncode, wynik


class ExamplePackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.cfg = self.root / "gate.config.json"
        self.cfg.write_text(json.dumps({
            "schema": 1, "lang": "en",
            # Konwencja o INNEJ nazwie niż nasza — dokładnie po to jest w konfiguracji.
            "paths": {"kb": "notes"},
        }), encoding="utf-8")

    def test_brak_katalogu_konwencji_to_na(self):
        rc, w = _run(self.home, self.cfg)
        self.assertEqual(rc, 0, w)
        self.assertEqual(w.get("status"), "na", w)

    def test_pusty_katalog_to_fail(self):
        (self.home / "notes").mkdir()
        rc, w = _run(self.home, self.cfg)
        self.assertEqual(rc, 1, f"pusty katalog musi dać fail (kontrakt: rc=1): {w}")
        self.assertEqual(w.get("status"), "fail", w)

    def test_katalog_z_plikami_to_pass(self):
        d = self.home / "notes"
        d.mkdir()
        (d / "pierwsza.md").write_text("# notatka\n", encoding="utf-8")
        rc, w = _run(self.home, self.cfg)
        self.assertEqual(rc, 0, w)
        self.assertEqual(w.get("status"), "pass", w)
        self.assertIn("1", str(w.get("summary")))

    def test_literowka_w_konfiguracji_to_fail_closed(self):
        self.cfg.write_text('{"paths": {"kb": "notes",}}', encoding="utf-8")   # zły JSON
        rc, w = _run(self.home, self.cfg)
        self.assertEqual(rc, 1, f"nieczytelna konfiguracja nie może wyglądać jak czysto: {w}")
        self.assertEqual(w.get("status"), "fail", w)


if __name__ == "__main__":
    unittest.main()
