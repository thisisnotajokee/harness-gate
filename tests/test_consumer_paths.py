"""Guard: konsumenci bramki nie mogą wskazywać na nieistniejące pliki.

Powód istnienia (2026-11-09): F3 przeniosło checki do `checks/core/` i `packs/<nazwa>/checks/`,
a konsument — `gate_snapshot.sh` (pole `script` dziennego joba Flat-MOS, 07:40) — dalej wołał
`checks/check_panel_integrity.py`. Do promptu dziennego raportu poszedł surowy błąd Pythona
(„can't open file … No such file or directory”), a panel był zielony, bo nikt tego nie mierzył.
Ten test mierzy: każda ŚCIEŻKA BRAMKI, którą cytuje konsument (skrypt powłoki albo komenda
w `SKILL.md`), musi istnieć.

Zakres świadomie wąski (żeby nie zgłaszać historii jako błędu):
  - `scripts/**/*.sh` — całe linie (konsumenci uruchamiani bez człowieka),
  - `skills/**/SKILL.md` — WYŁĄCZNIE inline-owe bloki kodu (komendy do wykonania), nie proza;
    `references/*.md` są poza zakresem (tam wolno opisywać nieistniejące ścieżki historycznie).
U obcego (brak `scripts/`/`skills/` albo brak w nich odwołań do bramki) test jest pomijany.
"""
import os
import re
import unittest
from pathlib import Path

HOST = Path(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
GATE = HOST / "scripts" / "harness_eval"

# Prefiksy, po których następuje ścieżka WZGLĘDNA wobec katalogu bramki.
# `$HERE` świadomie NIE jest tu wymieniony: to katalog samego launchera (`…/bin`), więc jego
# `../harness_eval.py` rozwiązywałoby się błędnie jako ścieżka względna wobec katalogu bramki.
_PREFIX = re.compile(
    r"(?P<pre>\$E|\$\{E\}|"
    r"\$HOME/\.hermes/scripts/harness_eval|~/\.hermes/scripts/harness_eval|"
    r"/[\w./-]*\.hermes/scripts/harness_eval)"
    r"/(?P<rel>[\w./-]+\.(?:py|sh|json))")
# Ścieżki względne wobec bramki bez prefiksu. Tylko `checks/` i `packs/` — te dwa katalogi są
# wyłącznie bramkowe i to one się przenosiły (F3). Szerszy wzorzec (`tools/`, `tests/`) łapał
# odwołania do repo `hermes-agent` (np. `tools/skill_manager_tool.py`) jako fałszywe alarmy.
_REL = re.compile(r"(?<![\w./-])(?P<rel>(?:checks|packs)/[\w./-]+\.(?:py|sh|json))")
_INLINE = re.compile(r"`([^`\n]+)`")


def _consumers():
    """(lista plików do przeskanowania) — tylko istniejące, tylko konsumenci spoza bramki."""
    files = []
    scripts = HOST / "scripts"
    if scripts.is_dir():
        files += sorted(p for p in scripts.rglob("*.sh") if p.is_file())
    skills = HOST / "skills"
    if skills.is_dir():
        files += sorted(skills.rglob("SKILL.md"))
    return files


def _candidates(path: Path):
    """(linia, ścieżka_względna) — tylko w kontekstach wykonawczych."""
    out = []
    text = path.read_text(encoding="utf-8", errors="replace")
    is_skill = path.name == "SKILL.md"
    for lineno, line in enumerate(text.splitlines(), 1):
        spans = _INLINE.findall(line) if is_skill else [line]
        for span in spans:
            if "*" in span:                       # glob — nie da się rozstrzygnąć jednym plikiem
                continue
            for m in _PREFIX.finditer(span):
                out.append((lineno, m.group("rel")))
            if is_skill:
                # w SKILL.md komenda bywa bez prefiksu; bierzemy tylko tokeny ścieżkowe
                for token in span.split():
                    m = _REL.match(token.strip("\"'"))
                    if m:
                        out.append((lineno, m.group("rel")))
    return out


class ConsumerPathsTests(unittest.TestCase):
    def test_sciezki_bramki_cytowane_przez_konsumentow_istnieja(self):
        self.assertTrue(GATE.is_dir(), f"brak drzewa bramki: {GATE}")
        consumers = _consumers()
        if not consumers:
            self.skipTest("brak konsumentów do przeskanowania (obcy dom)")
        dangling, checked = [], 0
        for path in consumers:
            for lineno, rel in _candidates(path):
                checked += 1
                if not (GATE / rel).exists():
                    dangling.append(f"{path}:{lineno} → {rel}")
        if checked == 0:
            self.skipTest("żaden konsument nie cytuje ścieżki bramki")
        self.assertEqual(
            dangling, [],
            "Konsumenci wskazują nieistniejące pliki bramki (przenosiny układu?):\n  "
            + "\n  ".join(dangling)
            + f"\n(przeskanowano {checked} odwołań w {len(consumers)} plikach)")


if __name__ == "__main__":
    unittest.main()
