#!/bin/sh
# install.sh — instalacja bramki harnessu u użytkownika (bez sudo, bez pytań).
#
# Co robi (R-UX1: jedna komenda, zero pytań, zero sudo):
#   1. kopiuje drzewo bramki do `$HERMES_HOME/scripts/harness_eval`,
#   2. zakłada kotwicę Git (git init + commit) — bez niej A-005 nie ma z czym porównać,
#   3. przezbraja manifest (`panel_lock.py --write`) na SKOPIOWANE drzewo,
#   4. tworzy `eval/variants/` (W3: brak katalogu egzaminu nie jest stanem czystym),
#   5. dowiązuje `~/.local/bin/harness-gate` → `bin/harness-gate.sh`.
#
# Czego NIE robi (świadomie):
#   - NIE kopiuje `gate.config.json` — obcy dostaje wartości domyślne (język `en`, paczki OFF,
#     Tier B OFF, parytet OFF). Nasz profil jest nasz; instalowanie go u kogoś to cudze czerwone.
#   - NIE dotyka `memories/`, `skills/`, `kb/`, `config.yaml` — bramka mierzy dom, nie zmienia go.
#   - NIE używa sudo i NIE pyta o nic.
#
# Użycie:
#   sh install.sh                  # instaluj do $HERMES_HOME (domyślnie ~/.hermes)
#   sh install.sh --home DIR       # wskaż inny dom Hermesa
#   sh install.sh --dest DIR       # wskaż inny katalog bramki (domyślnie <home>/scripts/harness_eval)
#   sh install.sh --force          # nadpisz istniejącą instalację (domyślnie: odmowa)
#   sh install.sh --no-link        # nie twórz dowiązania w ~/.local/bin
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
DEST=""
FORCE=0
LINK=1

while [ $# -gt 0 ]; do
  case "$1" in
    --home) HOME_DIR="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --no-link) LINK=0; shift ;;
    -h|--help) sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "nieznany argument: $1 (użyj --help)" >&2; exit 2 ;;
  esac
done

DEST="${DEST:-$HOME_DIR/scripts/harness_eval}"

say() { printf '%s\n' "$*"; }
die() { printf 'BŁĄD: %s\n' "$*" >&2; exit 1; }

[ -f "$HERE/harness_eval.py" ] || die "brak harness_eval.py obok instalatora — uruchom z katalogu bramki"
command -v python3 >/dev/null 2>&1 || die "brak python3 w PATH"
command -v git >/dev/null 2>&1 || die "brak git w PATH — kotwica Git jest wymagana (zainstaluj git, bez sudo nie zrobimy tego za Ciebie)"

if [ -d "$DEST" ] && [ -n "$(ls -A "$DEST" 2>/dev/null)" ] && [ "$FORCE" -ne 1 ]; then
  die "katalog docelowy już istnieje i nie jest pusty: $DEST (użyj --force, żeby nadpisać)"
fi

# --- 1. kopiowanie drzewa ---------------------------------------------------
SKIP_DIRS="tests tools docs __pycache__ .git"
SKIP_FILES="gate.config.json"
say "instaluję bramkę: $HERE → $DEST"
mkdir -p "$DEST"
DEST_ABS=$(CDPATH= cd -- "$DEST" && pwd)
HERE_ABS="$HERE"

copied=0
for entry in "$HERE"/* "$HERE"/.[!.]*; do
  [ -e "$entry" ] || continue
  name=$(basename -- "$entry")
  skip=0
  for d in $SKIP_DIRS; do [ "$name" = "$d" ] && skip=1; done
  for f in $SKIP_FILES; do [ "$name" = "$f" ] && skip=1; done
  case "$name" in *.pyc) skip=1 ;; esac
  [ "$skip" -eq 1 ] && continue
  # instalator nie kopiuje sam siebie do środka drzewa
  if [ "$entry" = "$HERE_ABS/install.sh" ]; then continue; fi
  if [ -d "$entry" ]; then
    rm -rf "$DEST_ABS/$name"
    cp -R "$entry" "$DEST_ABS/$name"
    find "$DEST_ABS/$name" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find "$DEST_ABS/$name" -name '*.pyc' -delete 2>/dev/null || true
  else
    cp -p "$entry" "$DEST_ABS/$name"
  fi
  copied=$((copied + 1))
done
say "  skopiowano $copied pozycji (bez gate.config.json, tests/, tools/, docs/)"

# --- 2-4. kotwica, manifest, katalog egzaminu -------------------------------
python3 "$DEST_ABS/panel_lock.py" --write >/dev/null || die "nie udało się przezbroić manifestu"
ok_anchor=0
if [ ! -d "$DEST_ABS/.git" ]; then
  ( cd "$DEST_ABS" && git init -q -b master ) || true
fi
( cd "$DEST_ABS" \
  && git add -A \
  && git -c user.email="$(git config user.email 2>/dev/null || echo install@local)" \
         -c user.name="$(git config user.name 2>/dev/null || echo instalator)" \
         commit -qm "instalacja bramki harnessu ($(date '+%Y-%m-%d %H:%M'))" ) >/dev/null 2>&1 && ok_anchor=1
mkdir -p "$HOME_DIR/eval/variants" "$HOME_DIR/eval/runs"
say "  kotwica Git: $([ "$ok_anchor" -eq 1 ] && echo 'utworzona (commit)' || echo 'commit bez zmian (drzewo już zacommitowane)')"
say "  manifest: $(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))['files']),'plików')" "$DEST_ABS/panel.lock" 2>/dev/null || echo '?')"
say "  katalog egzaminu: $HOME_DIR/eval/variants"

# --- 5. dowiązanie ----------------------------------------------------------
# Katalog dowiązania liczymy z DOMU BRAMKI, nie z `$HOME`. Gdy `HERMES_HOME` jest niestandardowy
# (instalacja na próbę, w piaskownicy, w kontenerze), pisanie do prawdziwego `~/.local/bin`
# PRZEPINAŁOBY działającą instalację użytkownika — zmierzone 2026-11-09: instalator
# z `HERMES_HOME=/tmp/...` wskazał produkcyjne `~/.local/bin/harness-gate` na drzewo w /tmp.
# Reguła: `<rodzic domu bramki>/.local/bin`; dla domyślnego `~/.hermes` to dokładnie
# `~/.local/bin`, więc nic się nie zmienia.
LINK_DIR="$(dirname "$HOME_DIR")/.local/bin"
LINK_PATH="$LINK_DIR/harness-gate"
if [ "$LINK" -eq 1 ] && [ -L "$LINK_PATH" ] && \
   [ "$(readlink "$LINK_PATH")" != "$DEST_ABS/bin/harness-gate.sh" ] && [ "$FORCE" -ne 1 ]; then
  say "  UWAGA: $LINK_PATH wskazuje na inną instalację — nie przepinam (użyj --force, żeby przejąć)"
  LINK=0
fi
if [ "$LINK" -eq 1 ]; then
  mkdir -p "$LINK_DIR"
  chmod +x "$DEST_ABS/bin/harness-gate.sh" 2>/dev/null || true
  ln -sf "$DEST_ABS/bin/harness-gate.sh" "$LINK_PATH"
  say "  dowiązanie: $LINK_PATH"
  case ":$PATH:" in
    *":$LINK_DIR:"*) ;;
    *) say "  UWAGA: $LINK_DIR nie jest w PATH — dodaj je, żeby działało samo harness-gate" ;;
  esac
fi
say ""
say "Co dalej:"
say "  1. HERMES_HOME=$HOME_DIR harness-gate            # panel po ludzku (0 tokenów, ~1 s)"
say "  2. HERMES_HOME=$HOME_DIR harness-gate --verbose  # pełna tabela kontroli"
say "  Konfiguracja (opcjonalna): skopiuj gate.config.json.example i dostosuj;"
say "  po każdej zmianie plików bramki: harness-gate --fix"
say ""
say "Czego nie skopiowano: gate.config.json (domyślnie: język en, paczki OFF, Tier B OFF, parytet OFF)."
