"""A-009 — kontrakt CLI mierzonego drzewa (check `checks/core/check_cli_contract.py`).

Test NIEZALEŻNY od implementacji checka: oparty wyłącznie na KONTRAKCIE podanym w zamówieniu,
nie na kodzie. Ma pinować cztery rzeczy, każdą z innego powodu:

* **zdrowe drzewo przechodzi** — fałszywa binarka z kompletem wymaganych flag + `skills/` +
  `state.db` to „dom zgodny z kontraktem", więc wynik musi być `pass`, a nie przegląd okoliczności;
* **brak wymaganej flagi JEST nazwany** — `summary` musi zawierać nazwę brakującej flagi,
  bo ogólne „help niekompletny" nie mówi człowiekowi, co dopisać do binarki;
* **fail-closed** — nieistniejąca binarka i zepsuty JSON bramki to `fail`, nigdy `skip`/`na`:
  „nie wiem, bo nie da się zmierzyć" nie może udawać „czysto";
* **kontrakt wyjścia** — ostatnia niepusta linia stdout to poprawny JSON z kluczami
  `check`/`status`/`summary`, a kod wyjścia to 0 dla `pass` i 1 dla `fail`.

Wszystkie zależności są lokalne: binarki tworzy test w katalogu tymczasowym (`chmod +x`),
konfiguracja bramki idzie przez `HARNESS_GATE_CONFIG`, drzewo mierzone jest w tmp.
Żadnej prawdziwej binarki `hermes` i żadnej sieci.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
CHECK = BASE / "checks" / "core" / "check_cli_contract.py"

# Zestaw flag z domyślnego profilu bramki (`gate_config.DEFAULTS["cli"]`). Bierzemy DOKŁADNIE ten
# zestaw, żeby test działał także wtedy, gdy check czyta wymagania z konfiguracji, jak i wtedy,
# gdy ma je wpisane jako domyślne — nie zakładamy po cichu, którego źródła użyje.
FLAGI = ["--in", "-z", "--yolo", "-m", "--provider"]


def write(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")


def run_check(script, *args, env_extra=None):
    """Uruchom check; `HARNESS_GATE_CONFIG` w env_extra wskazuje konfigurację bramki dla testu.

    Zwraca `(rep, returncode)`. Gdy stdout nie kończy się JSON-em, zwracamy syntetyczny raport
    z `status="NO-JSON(...)"`, żeby asercje na `status` dały czytelny powód, a nie wyjątek.
    """
    env = dict(os.environ)
    env.pop("HARNESS_GATE_CONFIG", None)
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, str(script), *map(str, args)],
                       capture_output=True, text=True, env=env, timeout=180)
    try:
        rep = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        rep = {"status": f"NO-JSON({p.returncode})", "summary": (p.stdout or p.stderr)[:200]}
    return rep, p.returncode


def run_raw(script, *args, env_extra=None):
    """Surowe uruchomienie — potrzebne tam, gdzie test sam parsuje ostatnią linię stdout (kontrakt wyjścia)."""
    env = dict(os.environ)
    env.pop("HARNESS_GATE_CONFIG", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(script), *map(str, args)],
                          capture_output=True, text=True, env=env, timeout=180)


def fake_cli(path, flags, version="1.0.0"):
    """Fałszywa binarka: skrypt POSIX odpowiadający `--version` i `--help` (z podanym zestawem flag).

    `--help` wypisuje każdą flagę z osobnej linii, więc check znajdzie je niezależnie od tego,
    czy szuka dokładnego tokenu, czy podciągu. Test nie dotyka prawdziwego `hermes`.
    """
    help_lines = "".join(f"  {f}  <wartość>\n" for f in flags)
    text = (
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  --version) echo "{version}"; exit 0;;\n'
        "  --help|-h)\n"
        "    cat <<'EOF'\n"
        "użycie: hermes-fake [opcje]\n"
        + help_lines +
        "EOF\n"
        "    exit 0;;\n"
        "esac\n"
        "exit 0\n"
    )
    p = Path(path)
    write(p, text)
    os.chmod(p, 0o755)
    return str(p)


def make_tree(root, skills=True, state=True):
    """Mierzone drzewo: katalog `skills/` i plik `state.db` (prawdziwy, pusty sqlite)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if skills:
        (root / "skills").mkdir(exist_ok=True)
    if state:
        sqlite3.connect(str(root / "state.db")).close()
    return str(root)


def gate_config(path, cli):
    """Minimalna konfiguracja bramki z sekcją `cli` (reszta pól bramka bierze z DEFAULTS)."""
    write(path, json.dumps({"schema": 1, "lang": "pl", "cli": cli}))
    return str(path)


class _CliContractCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="a009-"))
        # Cleanup rejestrujemy PRZED ewentualnym fail-em, żeby katalog nie wyciekał.
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        if not CHECK.exists():
            # Oczekiwany stan przed integracją: check ma jeszcze nie istnieć. Czytelny FAIL
            # (nie błąd importu ani wyjątek) z jasnym powodem.
            self.fail(f"check A-009 jeszcze nie istnieje: {CHECK} — "
                      "oczekiwany stan przed integracją, testy wskazują brak implementacji")

    def config(self, cli):
        return gate_config(self.tmp / "gate.config.json", cli)

    def cli(self, flags, version="1.0.0"):
        return fake_cli(self.tmp / "bin" / "hermes-fake", flags, version=version)

    def tree(self, skills=True, state=True):
        return make_tree(self.tmp / "tree", skills=skills, state=state)


class SciezkaSzczesliwa(_CliContractCase):
    def test_komplet_flag_i_zdrowe_drzewo_to_pass(self):
        """Fałszywa binarka z wszystkimi wymaganymi flagami + `skills/` + `state.db` → `pass`."""
        binp = self.cli(FLAGI)
        cfg = self.config({"bin": binp, "required_flags": FLAGI, "version_max": ""})
        rep, rc = run_check(CHECK, "--home", self.tree(),
                            env_extra={"HARNESS_GATE_CONFIG": cfg})
        self.assertEqual(rep["status"], "pass",
                         f"zdrowe drzewo i komplet flag musi dać pass, a nie: {rep}")
        self.assertEqual(rc, 0, f"pass nie może dawać kodu wyjścia != 0: {rep}")


class BrakujacaFlaga(_CliContractCase):
    def test_brak_jednej_flagi_to_fail_z_jej_nazwa(self):
        """`--help` bez jednej wymaganej flagi → `fail`, a summary NAZYWA brakującą flagę."""
        brak = "--yolo"
        binp = self.cli([f for f in FLAGI if f != brak])
        cfg = self.config({"bin": binp, "required_flags": FLAGI, "version_max": ""})
        rep, rc = run_check(CHECK, "--home", self.tree(),
                            env_extra={"HARNESS_GATE_CONFIG": cfg})
        self.assertEqual(rep["status"], "fail", f"brak flagi musi dać fail: {rep}")
        self.assertEqual(rc, 1, f"fail musi dawać kod wyjścia 1: {rep}")
        self.assertIn(brak, rep["summary"],
                      "summary musi NAZWAĆ brakującą flagę — inaczej człowiek nie wie, co dopisać")


class FailClosed(_CliContractCase):
    def test_nieistniejaca_binarka_to_fail_nie_skip(self):
        """`cli.bin` wskazuje nieistniejącą ścieżkę → `fail` (fail-closed), nie `skip`."""
        cfg = self.config({"bin": str(self.tmp / "nie-ma" / "hermes"),
                           "required_flags": FLAGI, "version_max": ""})
        rep, rc = run_check(CHECK, "--home", self.tree(),
                            env_extra={"HARNESS_GATE_CONFIG": cfg})
        self.assertEqual(rep["status"], "fail", f"brak binarki musi być failem: {rep}")
        self.assertNotEqual(rep["status"], "skip",
                            "nieobecność binarki to nie brak dowodu do zmierzenia, lecz twardy fail")
        self.assertEqual(rc, 1, f"fail musi dawać kod wyjścia 1: {rep}")

    def test_brak_state_db_to_fail(self):
        """Brak `state.db` w mierzonym drzewie → `fail`."""
        binp = self.cli(FLAGI)
        cfg = self.config({"bin": binp, "required_flags": FLAGI, "version_max": ""})
        rep, rc = run_check(CHECK, "--home", self.tree(state=False),
                            env_extra={"HARNESS_GATE_CONFIG": cfg})
        self.assertEqual(rep["status"], "fail", f"brak state.db musi dać fail: {rep}")
        self.assertEqual(rc, 1, rep["summary"])

    def test_brak_katalogu_skills_to_fail(self):
        """Brak katalogu `skills/` w mierzonym drzewie → `fail`."""
        binp = self.cli(FLAGI)
        cfg = self.config({"bin": binp, "required_flags": FLAGI, "version_max": ""})
        rep, rc = run_check(CHECK, "--home", self.tree(skills=False),
                            env_extra={"HARNESS_GATE_CONFIG": cfg})
        self.assertEqual(rep["status"], "fail", f"brak katalogu skills/ musi dać fail: {rep}")
        self.assertEqual(rc, 1, rep["summary"])

    def test_zepsuta_konfiguracja_bramki_to_fail(self):
        """Niepoprawny JSON przez `HARNESS_GATE_CONFIG` → `fail` (fail-closed, nie ciche pominięcie)."""
        zepsuty = self.tmp / "zepsuty.json"
        write(zepsuty, '{"schema": 1, "cli": {"bin": "x",}}')   # przecinek na końcu = niepoprawny JSON
        rep, rc = run_check(CHECK, "--home", self.tree(),
                            env_extra={"HARNESS_GATE_CONFIG": str(zepsuty)})
        self.assertEqual(rep["status"], "fail", f"zepsuta konfiguracja musi być failem: {rep}")
        self.assertEqual(rc, 1, f"fail musi dawać kod wyjścia 1: {rep}")


class WersjaPowyzejMax(_CliContractCase):
    def test_wersja_powyzej_max_nie_jest_failem_ale_ostrzega(self):
        """Wersja > `version_max` NIE jest failem — to ostrzeżenie widoczne w `details`/`summary`."""
        binp = self.cli(FLAGI, version="9.9.9")
        cfg = self.config({"bin": binp, "required_flags": FLAGI, "version_max": "1.2.3"})
        rep, rc = run_check(CHECK, "--home", self.tree(),
                            env_extra={"HARNESS_GATE_CONFIG": cfg})
        self.assertEqual(rep["status"], "pass",
                         f"wersja powyżej maksimum to ostrzeżenie, nie fail: {rep}")
        blob = (rep.get("summary", "") + " " + " ".join(rep.get("details") or [])).lower()
        self.assertTrue("9.9.9" in blob or "version_max" in blob or "wersj" in blob,
                        f"ostrzeżenie o wersji musi być widoczne w summary/details: {rep}")


class KontraktWyjscia(_CliContractCase):
    """Ostatnia niepusta linia stdout = JSON z `check`/`status`/`summary`; kod wyjścia 0/1."""

    def _ostatnia_linia_json(self, script, *args, env_extra=None):
        p = run_raw(script, *args, env_extra=env_extra)
        linie = [l for l in p.stdout.splitlines() if l.strip()]
        self.assertTrue(linie, f"check nic nie wypisał na stdout (kod {p.returncode})")
        return json.loads(linie[-1]), p.returncode      # json.JSONDecodeError = czytelny fail

    def test_pass_daje_json_z_kodem_zero(self):
        binp = self.cli(FLAGI)
        cfg = self.config({"bin": binp, "required_flags": FLAGI, "version_max": ""})
        rep, rc = self._ostatnia_linia_json(CHECK, "--home", self.tree(),
                                            env_extra={"HARNESS_GATE_CONFIG": cfg})
        self.assertEqual(rep["check"], "A-009-cli-contract", rep)
        self.assertEqual(rep["status"], "pass", rep)
        self.assertTrue(rep["summary"], "summary musi być niepuste")
        self.assertEqual(rc, 0, "pass musi dawać kod wyjścia 0")

    def test_fail_daje_json_z_kodem_jeden(self):
        brak = "--yolo"
        binp = self.cli([f for f in FLAGI if f != brak])
        cfg = self.config({"bin": binp, "required_flags": FLAGI, "version_max": ""})
        rep, rc = self._ostatnia_linia_json(CHECK, "--home", self.tree(),
                                            env_extra={"HARNESS_GATE_CONFIG": cfg})
        self.assertEqual(rep["check"], "A-009-cli-contract", rep)
        self.assertEqual(rep["status"], "fail", rep)
        self.assertTrue(rep["summary"], "summary musi być niepuste")
        self.assertEqual(rc, 1, "fail musi dawać kod wyjścia 1")

    def test_zepsuta_konfiguracja_daje_json_z_kodem_jeden(self):
        zepsuty = self.tmp / "zepsuty.json"
        write(zepsuty, '{"schema": 1, "cli": {"bin": "x",}}')
        rep, rc = self._ostatnia_linia_json(CHECK, "--home", self.tree(),
                                            env_extra={"HARNESS_GATE_CONFIG": str(zepsuty)})
        self.assertEqual(rep["check"], "A-009-cli-contract", rep)
        self.assertEqual(rep["status"], "fail", rep)
        self.assertTrue(rep["summary"], "summary musi być niepuste")
        self.assertEqual(rc, 1, "fail musi dawać kod wyjścia 1")


class Diagnoza(_CliContractCase):
    """Poprawka po pomiarze w świecie (F3): „binarka nie odpowiedziała" to INNA przyczyna niż
    „zna `--help`, ale bez wymaganej flagi".

    Zmierzone w świecie testowym: przy podstawionym `HOME` binarka Hermesa kończy `--help` kodem 1
    i 88 znakami błędu. Check mówił wtedy „nie zna wymaganej flagi: --in, -z, …" — czyli wskazywał
    przyczynę, której nie było: użytkownik „naprawiałby" binarkę zamiast środowiska.
    """

    def _binarka(self, help_rc=1, help_text="błąd: nie znajduję domu"):
        p = self.tmp / "bin" / "hermes-x"
        write(p, "#!/bin/sh\n"
                 "case \"$1\" in\n"
                 "  --version) echo \"1.0.0\"; exit 0;;\n"
                 f"  --help|-h) echo \"{help_text}\"; exit {help_rc};;\n"
                 "esac\n"
                 "exit 0\n")
        os.chmod(p, 0o755)
        return str(p)

    def test_help_z_niezerowym_kodem_to_fail_z_powodem_braku_odpowiedzi(self):
        cfg = self.config({"bin": self._binarka(help_rc=1), "required_flags": FLAGI,
                           "version_max": ""})
        rep, rc = run_check(CHECK, "--home", self.tree(),
                            env_extra={"HARNESS_GATE_CONFIG": cfg})
        self.assertEqual(rep["status"], "fail", rep)
        self.assertEqual(rc, 1, rep)
        self.assertIn("nie odpowiedziała", rep["summary"], rep["summary"])
        self.assertNotIn("nie zna wymaganej flagi", rep["summary"],
                         "diagnoza nie może oskarżać o brak flag, gdy binarka w ogóle nie "
                         "odpowiedziała")

    def test_binarka_dostaje_prawdziwy_dom_przez_harness_gate_machine_home(self):
        """Kontrakt CLI dotyczy MASZYNY, a harness pomiarowy podstawia `HOME` na mierzone drzewo.

        Bez jawnego prawdziwego domu binarka nie startuje w obcym domu, więc check mierzyłby
        sztuczkę pomiarową, nie kontrakt. Test pinuje furtkę `HARNESS_GATE_MACHINE_HOME`.
        """
        dom = self.tmp / "prawdziwy-dom"
        dom.mkdir()
        p = self.tmp / "bin" / "hermes-home"
        write(p, "#!/bin/sh\n"
                 "case \"$1\" in\n"
                 "  --version) echo \"1.0.0\"; exit 0;;\n"
                 "  --help|-h)\n"
                 f"    if [ \"$HOME\" = \"{dom}\" ]; then\n"
                 "      echo '  --in DIR'; echo '  -z PROMPT'; echo '  --yolo'\n"
                 "      echo '  -m MODEL'; echo '  --provider P'; exit 0\n"
                 "    fi\n"
                 "    echo 'brak domu'; exit 1;;\n"
                 "esac\n"
                 "exit 0\n")
        os.chmod(p, 0o755)
        cfg = self.config({"bin": str(p), "required_flags": FLAGI, "version_max": ""})
        rep, _ = run_check(CHECK, "--home", self.tree(),
                           env_extra={"HARNESS_GATE_CONFIG": cfg,
                                      "HOME": str(self.tmp / "podstawiony-dom"),
                                      "HARNESS_GATE_MACHINE_HOME": str(dom)})
        self.assertEqual(rep["status"], "pass",
                         f"binarka ma dostać prawdziwy dom z HARNESS_GATE_MACHINE_HOME: {rep}")


if __name__ == "__main__":
    unittest.main()
