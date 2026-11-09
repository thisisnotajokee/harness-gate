"""F2.3 — reguły bramki czytane z `gate.config.json` (nie z kodu, nie od kandydata).

Po co ten plik: F2.1/F2.2 dały bramkę, która *działa* u obcego, ale granice A-002/A-004/A-007,
zbiór parytetu A-008 i katalog wariantów A-006 siedziały jeszcze w kodzie — czyli bramka mierzyła
tylko taki dom, jaki mamy my. F2.3 przenosi je do konfiguracji BRAMKI (plik w manifeście, więc
`P1` działa bez zmiany mechanizmu: zmiana bez przezbrojenia locka = `fail` A-005).

Testy pinują trzy rzeczy, każda z innego powodu:

* **jedno źródło na wartość** — po przenosinach limit NIE MOŻE zostać drugi raz w `GATE_PARAMS`;
  dwie liczby o tym samym znaczeniu, z których jedna nie ma czytelnika, to klasa długu A-007;
* **fail-closed na zepsutej konfiguracji** — literówka w `gate.config.json` nie może wyglądać
  jak brak konfiguracji (wtedy „nie wiem, jakie są reguły" udawałoby „czysto");
* **`na` ≠ `skip`** — jawny wiersz `N/A` (parytet wyłączony, paczka nieobecna) nie jest
  niezaliczeniem, ale ZMIANA stosowalności między nogami porównania jest blokerem: wyłączanie
  zadania nie może być najtańszą drogą do ACCEPT.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Profil autora (gate.config.json) NIE jest częścią wydania: te przypadki mierzą JEGO,
# a nie mechanizm — w świeżym drzewie nie mają czego mierzyć, więc jawnie się pomijają.
_PROFIL = os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                                     "gate.config.json"))
_PROFIL_SKIP = "brak gate.config.json — ten przypadek mierzy profil, nie mechanizm"

from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
# F3: checki rdzenia mieszkają w `checks/core/`, zadania paczek w `packs/<n>/checks/`.
CHECK_DIRS = [BASE / "checks" / "core"] + sorted((BASE / "packs").glob("*/checks"))
sys.path.insert(0, str(BASE))


def check_path(script):
    """Rozwiąż nazwę checka do nowego układu rdzeń/paczki (F3)."""
    for d in CHECK_DIRS:
        if (d / script).is_file():
            return d / script
    raise FileNotFoundError(f"nie ma checka {script} w {[str(d) for d in CHECK_DIRS]}")


def write(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")


def run_check(script, *args, env_extra=None):
    """Uruchom check; `HARNESS_GATE_CONFIG` w env_extra wskazuje konfigurację bramki dla testu."""
    env = dict(os.environ)
    env.pop("HARNESS_GATE_CONFIG", None)
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, str(check_path(script)), *map(str, args)],
                       capture_output=True, text=True, env=env, timeout=180)
    try:
        rep = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        rep = {"status": f"NO-JSON({p.returncode})", "summary": (p.stdout or p.stderr)[:200]}
    return rep, p.returncode


class _GateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="f23-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, *parts):
        return str(self.tmp.joinpath(*parts))

    def config(self, cfg: dict, name="gate.config.json") -> str:
        path = self.p(name)
        write(path, json.dumps(cfg))
        return path

    def world(self, cfg: dict | None = None) -> str:
        """Świat: kopia bramki POD `$HERMES_HOME/scripts/harness_eval` + własna kotwica git.

        Potrzebny, gdy chcemy mierzyć RUNNER (preflight wymaga spójności drzewa i kotwicy),
        a nie pojedynczy check. Lock generujemy w świecie, żeby test nie zależał od stanu
        manifestu w drzewie cienia.
        """
        home = self.tmp / ".hermes"
        gate = home / "scripts" / "harness_eval"
        gate.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(BASE, gate, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        if cfg is not None:
            write(gate / "gate.config.json", json.dumps(cfg))
        subprocess.run([sys.executable, str(gate / "panel_lock.py"), "--write"],
                       capture_output=True, text=True, timeout=120)
        git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
        for cmd in ([["git", "init", "-q", "-b", "master"], ["git", "add", "-A"],
                     git + ["commit", "-qm", "swiat"]]):
            subprocess.run(cmd, cwd=gate, capture_output=True, text=True, timeout=60)
        return str(gate)

    def runner(self, gate: str, *args):
        env = dict(os.environ)
        env["HERMES_HOME"] = str(self.tmp / ".hermes")
        env.pop("HARNESS_GATE_CONFIG", None)
        p = subprocess.run([sys.executable, str(Path(gate) / "harness_eval.py"), *map(str, args)],
                           capture_output=True, text=True, env=env, timeout=300,
                           cwd=str(self.tmp))
        return p


class NietypowyDomTest(unittest.TestCase):
    """`HERMES_HOME` to dom MIERZONY; `$HOME` jest wyłącznie wartością domyślną.

    Regresja zmierzona 2026-11-09 na prawdziwym systemie: sondy piaskownicy miały zaszyte
    `~/.hermes`, więc użytkownik z niestandardowym `HERMES_HOME` chronił CUDZY dom, a własny
    zostawał bez osłony; selftest sandboxa badał ścieżkę, w której rollout nigdy nie pracuje.
    """

    def test_sondy_idą_za_domem_mierzonym(self):
        import gate_config
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"HERMES_HOME": d}):
                cfg = gate_config.for_checks(str(BASE))
        self.assertTrue(cfg["ok"], cfg.get("error"))
        self.assertEqual(cfg["paths"]["hermes_home"], os.path.realpath(d))
        self.assertEqual(cfg["probes_forbidden"][0], os.path.realpath(d),
                         "sonda ma chronić DOM MIERZONY, nie zaszyte ~/.hermes")
        self.assertIn(".ssh", cfg["probes_forbidden"][1])

    def test_placeholder_nie_wycieka_jako_sciezka(self):
        """`<hermes_home>` to placeholder KONFIGURACJI, nigdy ścieżka w werdykcie."""
        import gate_config
        cfg = gate_config.for_checks(str(BASE))
        for p in cfg["probes_forbidden"]:
            self.assertNotIn("<hermes_home>", p, f"placeholder nie został rozwiązany: {p}")

    def test_nota_tylko_gdy_dom_inny(self):
        """Nota „mierzę to drzewo" pojawia się wyłącznie, gdy dom != `$HOME/.hermes`.

        `base` musi być bramką TEGO domu (`<home>/scripts/harness_eval`) — inaczej `tree_guard`
        słusznie kończy `FOREIGN_BASE` i noty nie ma (ta kolejność guardów jest zamierzona).
        """
        import harness_eval
        with tempfile.TemporaryDirectory() as d:
            dom = os.path.join(d, "a")
            bramka = os.path.join(dom, "scripts", "harness_eval")
            os.makedirs(bramka)
            rozny = harness_eval.tree_guard(home=dom, env_home=os.path.join(d, "b"), base=bramka)
            taki_sam = harness_eval.tree_guard(home=dom, env_home=dom, base=bramka)
        self.assertEqual(rozny["status"], "pass", rozny)
        self.assertTrue(rozny.get("note"), "brak noty przy innym domu")
        self.assertFalse(taki_sam.get("note"), "nota przy tym samym domu to szum")


class JednoZrodloPrawdy(_GateCase):
    def test_limit_nie_ma_drugiej_prawdy_w_gate_params(self):
        """Po przenosinach limitów do konfiguracji `GATE_PARAMS` nie może ich trzymać drugi raz."""
        import eval_version
        zostale = [k for k in eval_version.GATE_PARAMS
                   if k.startswith(("a002_", "a004_", "a007_"))]
        self.assertEqual(zostale, [], f"druga prawda o limitach w GATE_PARAMS: {zostale}")

    def test_limity_sa_w_konfiguracji_bramki(self):
        import gate_config
        cfg = gate_config.for_checks(str(BASE))
        self.assertTrue(cfg["ok"], cfg.get("error"))
        self.assertEqual(cfg["limits"], {"memory_char": 2500, "user_char": 1500,
                                         "a004_max_warnings": 150, "a007_max_warnings": 0})

    @unittest.skipUnless(_PROFIL, _PROFIL_SKIP)
    def test_nasz_profil_nie_jest_domyslny_lecz_jawny(self):
        """Parytet u nas włączony, ale to WYNIK KONFIGURACJI, nie domyślnej wartości kodu."""
        import gate_config
        domyslne = gate_config.DEFAULTS
        self.assertFalse(domyslne["parity"]["enabled"],
                         "domyślny parytet ma być WYŁĄCZONY — brak mirrora to jawne N/A, nie fail")
        nasz = gate_config.load(str(BASE))["cfg"]
        self.assertTrue(nasz["parity"]["enabled"])
        self.assertEqual(nasz["threshold_sources"],
                         ["quality_watchdog.py", "daily_report_embed.py"])

    def test_nieznane_pokretlo_w_sekcji_jest_glosne(self):
        """`tier_b.m_min` wygląda jak pokrętło, a nim nie jest (mieszka w hashu progów) —
        cicha ignorancja uczy, że konfiguracja działa, gdy nie działa."""
        import gate_config
        cfg = self.config({"schema": 1, "tier_b": {"m_min": 6, "enabled": True}})
        os.environ["HARNESS_GATE_CONFIG"] = cfg
        try:
            res = gate_config.load()
        finally:
            os.environ.pop("HARNESS_GATE_CONFIG", None)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertTrue(any("tier_b.m_min" in w for w in res["warnings"]), res["warnings"])


class FailClosedNaKonfiguracji(_GateCase):
    CHECKI = ("check_memory_limits.py", "check_skill_frontmatter.py",
              "check_provider_coherence.py", "check_anchor_freshness.py")

    @unittest.skipUnless(_PROFIL, _PROFIL_SKIP)
    def test_zepsuta_konfiguracja_blokuje_wszystkie_checki(self):
        """Literówka w JSON-ie nie może wyglądać jak brak konfiguracji (fail-closed)."""
        zepsuty = self.p("zepsuty.json")
        write(zepsuty, '{"schema": 1, "limits": {"memory_char": 100,}}')   # przecinek na końcu
        for check in self.CHECKI:
            rep, rc = run_check(check, "--home", self.p(), env_extra={"HARNESS_GATE_CONFIG": zepsuty})
            self.assertEqual(rep["status"], "fail", f"{check} przepuścił zepsutą konfigurację")
            self.assertNotEqual(rc, 0, check)
            self.assertIn("nie da się odczytać", rep["summary"], check)

    def test_nieznany_schemat_konfiguracji_to_fail(self):
        cfg = self.config({"schema": 99})
        rep, _ = run_check("check_memory_limits.py", "--home", self.p(),
                           env_extra={"HARNESS_GATE_CONFIG": cfg})
        self.assertEqual(rep["status"], "fail", rep["summary"])

    def test_limit_idzie_z_konfiguracji_a_nie_z_drzewa(self):
        """Twardy dowód na źródło: konfiguracja bramki mówi 100, drzewo nie deklaruje nic."""
        write(self.p("memories", "MEMORY.md"), "x" * 200)
        cfg = self.config({"schema": 1, "limits": {"memory_char": 100}})
        rep, _ = run_check("check_memory_limits.py", "--home", self.p(),
                           env_extra={"HARNESS_GATE_CONFIG": cfg})
        self.assertEqual(rep["status"], "fail", rep["summary"])
        self.assertIn("200 > 100", rep["summary"])

    def test_podniesienie_wlasnego_budzetu_w_drzewie_nadal_jest_failem(self):
        """Dryf: kandydat deklaruje WIĘCEJ niż bramka (to nie jest zaostrzenie)."""
        write(self.p("memories", "MEMORY.md"), "x" * 10)
        write(self.p("config.yaml"), "memory_char_limit: 99999\n")
        cfg = self.config({"schema": 1, "limits": {"memory_char": 100}})
        rep, _ = run_check("check_memory_limits.py", "--home", self.p(),
                           env_extra={"HARNESS_GATE_CONFIG": cfg})
        self.assertEqual(rep["status"], "fail", rep["summary"])
        self.assertIn("podniósł własny budżet", rep["summary"])


class ParytetA008(_GateCase):
    @unittest.skipUnless(_PROFIL, _PROFIL_SKIP)
    def test_wylaczony_parytet_to_jawny_na(self):
        """P4/Q2: brak mirrora to jawny `N/A` z powodem — nie `fail`, nie cichy `skip`."""
        cfg = self.config({"schema": 1})      # parytet domyślnie wyłączony
        rep, rc = run_check("check_anchor_freshness.py", "--home", self.p(),
                            env_extra={"HARNESS_GATE_CONFIG": cfg})
        self.assertEqual(rep["status"], "na", rep["summary"])
        self.assertEqual(rc, 0, "N/A nie może dawać kodu wyjścia fail")
        self.assertIn("N/A", rep["summary"])
        for slowo in ("TAMPER", "atak", "manipulacj"):     # R-UX5: zakaz oskarżeń w trybie domyślnym
            self.assertNotIn(slowo.lower(), rep["summary"].lower())

    @unittest.skipUnless(_PROFIL, _PROFIL_SKIP)
    def test_wlaczony_parytet_bez_kotwicy_to_fail(self):
        """Gdy konfiguracja DEKLARUJE parytet, brak kotwicy jest błędem — nie ma czego mierzyć."""
        cfg = self.config({"schema": 1,
                           "parity": {"enabled": True, "repo": self.p("nie-ma-takiego")}})
        rep, _ = run_check("check_anchor_freshness.py", "--home", self.p(),
                           env_extra={"HARNESS_GATE_CONFIG": cfg})
        self.assertEqual(rep["status"], "fail", rep["summary"])
        self.assertIn("parytet włączony", rep["summary"])

    @unittest.skipUnless(_PROFIL, _PROFIL_SKIP)
    def test_brak_plikow_parytetu_to_fail_nie_skip(self):
        """Zbiór pusty to brak egzaminu — a nie „zgodne, bo nie ma czego porównywać"."""
        repo = self.tmp / "kotwica"
        (repo / ".git").mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "master"], cwd=repo, capture_output=True)
        cfg = self.config({"schema": 1, "parity": {"enabled": True, "repo": str(repo)}})
        rep, _ = run_check("check_anchor_freshness.py", "--home", self.p(),
                           env_extra={"HARNESS_GATE_CONFIG": cfg})
        self.assertEqual(rep["status"], "fail", rep["summary"])
        self.assertIn("nie ma czego porównywać", rep["summary"])


class StatusNa(_GateCase):
    def test_panel_liczy_na_i_nie_czerwienieje(self):
        """`na` nie jest niezaliczeniem (inaczej pierwsza instalacja bez konfiguracji byłaby
        czerwona, wbrew R-UX2), ale nadal jest WIDOCZNE w podsumowaniu."""
        import harness_eval
        rows = [{"id": "A-001", "status": "pass"}, {"id": "A-008", "status": "na"}]
        self.assertEqual(harness_eval.summarise(rows)["na"], 1)
        self.assertEqual(harness_eval.panel_exit_code(rows), 0)
        self.assertEqual(harness_eval.panel_exit_code(rows + [{"id": "A-004", "status": "skip"}]), 1,
                         "skip nadal jest blokerem — brak dowodu to nie brak wymagania")
        self.assertEqual(harness_eval.panel_exit_code([{"id": "A-004", "status": "fail"}]), 1)

    def test_na_symetryczne_jest_remisem(self):
        import harness_eval
        inc = [{"id": "A-008", "status": "na"}]
        cand = [{"id": "A-008", "status": "na"}]
        comp = harness_eval.compare(inc, cand)
        self.assertEqual(comp["blockers"], [])
        self.assertEqual(comp["ties"], ["A-008"])
        self.assertEqual(comp["losses"], [])

    def test_zmiana_stosowalnosci_jest_blokerem(self):
        """Wyłączenie zadania po jednej stronie porównania nie może być drogą do ACCEPT."""
        import harness_eval
        comp = harness_eval.compare([{"id": "A-008", "status": "pass"}],
                                    [{"id": "A-008", "status": "na"}])
        self.assertTrue(any("stosowalność:A-008" in b for b in comp["blockers"]), comp["blockers"])
        v, _ = harness_eval.verdict(comp, "1B")
        self.assertEqual(v, "REJECT")

    def test_gate_params_bez_limitow_nie_psuje_werdyktu(self):
        """Po przenosinach m/δ zostają JEDYNYM kryterium liczbowym werdyktu — nadal działają."""
        import harness_eval
        comp = {"wins": ["B-002"], "losses": [], "ties": [], "blockers": [], "p_value": 0.5}
        v, reason = harness_eval.verdict(comp, "1B")
        self.assertEqual(v, "ACCEPT", reason)


class TierBKonfiguracja(_GateCase):
    def _cfg(self, **tier_b):
        base = {"schema": 1, "hermes_home": self.p("home"), "eval_dir": self.p("home", "eval"),
                "cli": {"bin": "/opt/obcy/bin/hermes-x"},
                "probes_forbidden": ["/srv/sekret", "~/.ssh"],
                "tier_b": {"enabled": True, "model": "obcy/model-x", "provider": "obcyprov"}}
        base["tier_b"].update(tier_b)
        return self.config(base)

    def test_binarka_sondy_model_z_konfiguracji(self):
        cfg = self._cfg()
        env = dict(os.environ)
        env.update({"HARNESS_GATE_CONFIG": cfg})
        for k in ("EVAL_HERMES_BIN", "EVAL_MODEL", "EVAL_PROVIDER", "EVAL_FORBIDDEN_PROBES"):
            env.pop(k, None)
        code = ("import json, tier_b; print(json.dumps({'bin': tier_b.HERMES_BIN, "
                "'probes': tier_b.probes_forbidden(), 'model': tier_b.EVAL_MODEL, "
                "'prov': tier_b.EVAL_PROVIDER, 'home': tier_b.HERMES_HOME}))")
        p = subprocess.run([sys.executable, "-c", code], cwd=str(BASE), env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        d = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(d["bin"], "/opt/obcy/bin/hermes-x")
        self.assertEqual(d["probes"], ["/srv/sekret", os.path.expanduser("~/.ssh")])
        self.assertEqual(d["model"], "obcy/model-x")
        self.assertEqual(d["prov"], "obcyprov")
        self.assertEqual(d["home"], self.p("home"))

    def test_jawna_zmienna_wygrywa_z_konfiguracja(self):
        """Pokrętło jednorazowe (EVAL_MODEL) ma pierwszeństwo — jawna wola człowieka > plik."""
        cfg = self._cfg()
        env = dict(os.environ)
        env.update({"HARNESS_GATE_CONFIG": cfg, "EVAL_MODEL": "recznie/model-y",
                    "EVAL_HERMES_BIN": "/tmp/recznie-hermes"})
        code = ("import json, tier_b; print(json.dumps({'bin': tier_b.HERMES_BIN, "
                "'model': tier_b.EVAL_MODEL}))")
        p = subprocess.run([sys.executable, "-c", code], cwd=str(BASE), env=env,
                           capture_output=True, text=True, timeout=120)
        d = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(d["bin"], "/tmp/recznie-hermes")
        self.assertEqual(d["model"], "recznie/model-y")

    def test_runner_nie_ma_literalu_modelu(self):
        """Pin A-007 pilnuje literału w runnerze — po F2.3 nie ma go tam wcale."""
        src = (BASE / "tier_b.py").read_text(encoding="utf-8")
        m = re.search(r"""EVAL_MODEL\s*=\s*os\.environ\.get\([^,]+,\s*["']""", src)
        self.assertIsNone(m, "literał modelu wrócił do runnera (klasa defektu A-007)")

    def test_wylaczony_tier_b_odmawia(self):
        """Tier B kosztuje tokeny: bez `tier_b.enabled=true` runner ma odmówić (R-UX3)."""
        gate = self.world({"schema": 1, "tier_b": {"enabled": False}})
        p = self.runner(gate, "--tier-b", "--variant", "cokolwiek")
        self.assertEqual(p.returncode, 1, p.stdout[-400:] + p.stderr[-400:])
        # Werdykt idzie jako JSON z wcięciami (czytelny dla człowieka), więc szukamy KODU w treści,
        # a nie parsujemy ostatniej linii — ostatnia linia to klamra.
        self.assertIn("TIER_B_DISABLED", p.stdout, p.stdout[-600:])


class SondaSandboxu(_GateCase):
    """Sonda zapisu to INSTRUMENT — jej fałszywy alarm jest gorszy niż brak pomiaru.

    Zmierzone w świecie `w7_v2_selftest`: w domu bez katalogu `kb/` sonda raportowała
    `FileNotFoundError` (rozwiązywanie ścieżki kończy się przed kontrolą uprawnień), a kontrola
    negatywna bramki ogłaszała `pass: False`. Test pinuje rozstrzygnięcie: brak drogi = `blocked`,
    a nie „nie wiem".
    """

    @staticmethod
    def _module():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sandbox_selftest_probe", BASE / "sandbox_selftest.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_brak_katalogu_na_sciezce_niedozwolonej_to_blocked(self):
        mod = self._module()
        zamkniety = self.tmp / "zamkniety"
        (zamkniety / "pod").mkdir(parents=True)
        os.chmod(zamkniety, 0o500)                 # bez prawa zapisu: mkdir nie przejdzie
        try:
            wynik = mod.write_probe(str(zamkniety / "nie-ma" / ".__probe"))
        finally:
            os.chmod(zamkniety, 0o700)
        self.assertEqual(wynik, "blocked", wynik)

    def test_sciezka_dozwolona_to_allowed(self):
        mod = self._module()
        self.assertEqual(mod.write_probe(self.p("wolne", ".__probe")), "allowed")

    def test_sonda_dziala_jako_skrypt(self):
        """Runner woła ją jako PROCES (`python3 sandbox_selftest.py <scratch>`) — main-guard
        nie może zjeść jej wyjścia, bo wtedy cała kontrola negatywna zwraca BRAK-JSON.

        `HOME` przekierowany do katalogu testu: sonda pisze w `<HOME>/.hermes/...`, więc bez tego
        test zostawiałby (i kasował) pliki w KATALOGU PRODUKCYJNYM. Test nie dotyka produkcji —
        nawet „tylko plikiem tymczasowym".
        """
        env = dict(os.environ)
        env["HOME"] = self.p()
        p = subprocess.run([sys.executable, str(BASE / "sandbox_selftest.py"), self.p("scratch")],
                           capture_output=True, text=True, timeout=120, env=env)
        d = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(d["write_scratch"], "allowed")


if __name__ == "__main__":
    unittest.main()
