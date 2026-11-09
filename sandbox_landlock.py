#!/usr/bin/env python3
"""sandbox_landlock.py — sandbox procesu potomnego na Landlock LSM (kernel >= 5.13).

Dlaczego nie bwrap: na tym hoście user namespaces są zablokowane przez AppArmor
(`kernel.apparmor_restrict_unprivileged_userns=1`), więc bwrap wymagałby wyłączenia
hardeningu całej maszyny. Landlock działa bez żadnych uprawnień.

Dlaczego osobny proces, a nie `preexec_fn`: restrykcja Landlock jest NIEODWRACALNA
w obrębie procesu i dziedziczona przez dzieci. Gdyby nałożył ją runner w swoim
procesie, sam nie mógłby zapisać werdyktu do ledger.jsonl. Python dokumentuje
`preexec_fn` jako niebezpieczny (fork + wątki = ryzyko zakleszczenia), dlatego
restrykcja jest nakładana tutaj, a następnie proces robi execve na komendzie.

Użycie:
  sandbox_landlock.py --rw DIR [--rw DIR]... [--allow-net-port 443] [--no-net]
                      [--restrict-read] [--keep-env NAME] [--no-verify]
                      -- CMD [ARGS...]

Kody wyjścia: 0 = komenda OK, 2 = błąd konfiguracji/kernel, 3 = fail-closed
(weryfikacja sandboxa nie przeszła — komenda NIE została uruchomiona).
"""
import argparse
import ctypes
import os
import sys

SYS_CREATE, SYS_ADD_RULE, SYS_RESTRICT = 444, 445, 446
RULE_PATH_BENEATH, RULE_NET_PORT = 1, 2
PR_SET_NO_NEW_PRIVS = 38
O_PATH, O_CLOEXEC = getattr(os, "O_PATH", 0o10000000), os.O_CLOEXEC

# --- dostęp FS (ABI 4) ---
F_EXECUTE = 1 << 0
F_WRITE_FILE = 1 << 1
F_READ_FILE = 1 << 2
F_READ_DIR = 1 << 3
F_REMOVE_DIR = 1 << 4
F_REMOVE_FILE = 1 << 5
F_MAKE_CHAR = 1 << 6
F_MAKE_DIR = 1 << 7
F_MAKE_REG = 1 << 8
F_MAKE_SOCK = 1 << 9
F_MAKE_FIFO = 1 << 10
F_MAKE_BLOCK = 1 << 11
F_MAKE_SYM = 1 << 12
F_REFER = 1 << 13
F_TRUNCATE = 1 << 14

WRITE_BITS = (F_WRITE_FILE | F_REMOVE_DIR | F_REMOVE_FILE | F_MAKE_CHAR | F_MAKE_DIR |
              F_MAKE_REG | F_MAKE_SOCK | F_MAKE_FIFO | F_MAKE_BLOCK | F_MAKE_SYM |
              F_REFER | F_TRUNCATE)
READ_BITS = F_READ_FILE | F_READ_DIR | F_EXECUTE

NET_BIND_TCP = 1 << 0
NET_CONNECT_TCP = 1 << 1

# Katalogi systemowe, których odczyt musi zostać dozwolony w trybie --restrict-read.
READ_ALLOW = ["/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
              "/etc", "/opt", "/proc", "/dev", "/sys", "/run"]

# Urządzenia, do których zapis MUSI zostać dozwolony: normalny proces (i Hermes
# w trybie one-shot) otwiera /dev/null do zapisu. Bez tego rollout pada na
# PermissionError zanim w ogóle wystartuje.
DEV_WRITE_ALLOW = ["/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom"]

# Zmienne środowiskowe zawsze usuwane w Tier B (sesyjne klucze Hermesa).
ENV_ALWAYS_DROP = {"HERMES_SESSION_KEY", "TERMINAL_DOCKER_SHARED_CONTAINER_KEY",
                   "HERMES_REDACT_SECRETS"}
# Wzorce nazw zmiennych uznawanych za sekrety.
ENV_SECRET_HINTS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL",
                    "APIKEY", "API_KEY", "PRIVATE_KEY")
ENV_KEEP_BASE = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG",
                 "LC_ALL", "TMPDIR", "PWD", "HOSTNAME", "COLORTERM", "VIRTUAL_ENV",
                 "PYTHONPATH", "DISPLAY", "XDG_RUNTIME_DIR", "TZ"}


class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64),
                ("handled_access_net", ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class NetPortAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


def die(msg, code=2):
    print(f"sandbox_landlock: BŁĄD: {msg}", file=sys.stderr)
    sys.exit(code)


def landlock_abi():
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    abi = libc.syscall(SYS_CREATE, None, 0, 1)
    if abi < 1:
        die(f"Landlock niedostępny w jądrze (errno={ctypes.get_errno()})")
    return libc, abi


def add_path_rule(libc, ruleset_fd, path, access):
    if not os.path.exists(path):
        return False
    fd = os.open(path, O_PATH | O_CLOEXEC)
    try:
        attr = PathBeneathAttr(allowed_access=access, parent_fd=fd)
        if libc.syscall(SYS_ADD_RULE, ruleset_fd, RULE_PATH_BENEATH,
                        ctypes.byref(attr), 0) < 0:
            die(f"landlock_add_rule({path}) errno={ctypes.get_errno()}")
        return True
    finally:
        os.close(fd)


def sanitize_env(keep):
    env, dropped = {}, []
    for k, v in os.environ.items():
        if k in ENV_ALWAYS_DROP or k in keep:
            if k not in keep:
                dropped.append(k)
                continue
        if k in keep or k in ENV_KEEP_BASE:
            env[k] = v
            continue
        if any(h in k.upper() for h in ENV_SECRET_HINTS):
            dropped.append(k)
            continue
        env[k] = v
    return env, dropped


def verify(write_dir, forbidden_probes):
    """Fail-closed: sprawdź PRZED exec, że zapis jest dozwolony tam, gdzie ma być,
    i zablokowany tam, gdzie nie wolno."""
    if write_dir:
        probe = os.path.join(write_dir, ".__sandbox_write_probe")
        try:
            with open(probe, "w") as f:
                f.write("ok")
            os.unlink(probe)
        except Exception as e:
            die(f"fail-closed: zapis w dozwolonym katalogu {write_dir} nie działa "
                f"({type(e).__name__}: {e}) — komenda NIE uruchomiona", 3)
    for forbidden_probe in (forbidden_probes or []):
        try:
            with open(forbidden_probe, "w") as f:
                f.write("x")
            os.unlink(forbidden_probe)
            die(f"fail-closed: zapis w ZABRONIONYM miejscu {forbidden_probe} się powiódł "
                f"— sandbox nie działa, komenda NIE uruchomiona", 3)
        except PermissionError:
            continue
        except Exception as e:
            die(f"fail-closed: nieoczekiwany błąd próby zapisu ({type(e).__name__}: {e})", 3)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--rw", action="append", default=[], help="katalog z prawem zapisu")
    ap.add_argument("--allow-net-port", action="append", type=int, default=[],
                    help="zezwól na connect TCP tylko na ten port (można wielokrotnie)")
    ap.add_argument("--no-net", action="store_true", help="zablokuj cały connect/bind TCP")
    ap.add_argument("--restrict-read", action="store_true",
                    help="etap 2: ogranicz też ODCZYT (domyślne katalogi systemowe + --rw)")
    ap.add_argument("--keep-env", action="append", default=[],
                    help="nie usuwaj tej zmiennej środowiskowej (można wielokrotnie)")
    ap.add_argument("--no-verify", action="store_true", help="pomiń fail-closed (nie zalecane)")
    ap.add_argument("--forbidden-probe", action="append", default=[],
                    help="ścieżka, w której zapis MUSI się nie udać (domyślnie ~/.hermes)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        die("brak komendy po '--'")
    if not args.rw and not args.no_verify:
        die("brak --rw: bez katalogu zapisywalnego sandbox nie ma sensu")

    libc, abi = landlock_abi()
    handled_fs = WRITE_BITS | (READ_BITS if args.restrict_read else 0)
    net_handled = NET_BIND_TCP | NET_CONNECT_TCP if (args.allow_net_port or args.no_net) else 0

    attr = RulesetAttr(handled_access_fs=handled_fs, handled_access_net=net_handled)
    ruleset = libc.syscall(SYS_CREATE, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset < 0:
        die(f"landlock_create_ruleset errno={ctypes.get_errno()} (ABI {abi})")

    rw = [os.path.realpath(p) for p in args.rw]
    for d in rw:
        if not os.path.isdir(d):
            die(f"--rw {d} nie jest katalogiem")
        add_path_rule(libc, ruleset, d, WRITE_BITS | (READ_BITS if args.restrict_read else 0))

    if args.restrict_read:
        for d in READ_ALLOW:
            add_path_rule(libc, ruleset, d, READ_BITS)
        # interpreter i venv mogą żyć poza katalogami systemowymi (nasz przypadek!)
        for extra in {os.path.dirname(os.path.realpath(sys.executable)),
                      os.path.dirname(os.path.dirname(os.path.realpath(sys.executable))),
                      os.path.dirname(os.path.realpath(cmd[0])) if os.path.sep in cmd[0] else ""}:
            if extra:
                add_path_rule(libc, ruleset, extra, READ_BITS)

    for dev in DEV_WRITE_ALLOW:
        add_path_rule(libc, ruleset, dev, F_WRITE_FILE)

    for port in set(args.allow_net_port):
        # MUSZĄ być oba prawa: `net_handled` obejmuje BIND i CONNECT, więc reguła przyznająca
        # sam CONNECT **blokuje bind na każdym porcie** (Landlock: co jest handled, a nie ma
        # reguły — jest zabronione). Długo tego nie było widać, bo ówczesny `hermes -z` nie
        # dźwigał bramki; gdy Hermes zaczął wymagać bramki (binduje lokalny port), każdy rollout
        # padał na `Gateway draining: not_ready` — bez śladu, że winna jest reguła sieciowa.
        rule = NetPortAttr(allowed_access=NET_BIND_TCP | NET_CONNECT_TCP, port=port)
        if libc.syscall(SYS_ADD_RULE, ruleset, RULE_NET_PORT, ctypes.byref(rule), 0) < 0:
            die(f"reguła sieciowa dla portu {port} odrzucona (errno={ctypes.get_errno()})")

    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        die("prctl(NO_NEW_PRIVS) nieudane")
    if libc.syscall(SYS_RESTRICT, ruleset, 0) < 0:
        die(f"landlock_restrict_self errno={ctypes.get_errno()}")

    if not args.no_verify:
        try:
            os.makedirs(os.path.join(os.path.expanduser("~"), ".hermes"), exist_ok=True)
        except Exception:
            pass
        probes = args.forbidden_probe or [os.path.join(os.path.expanduser("~"), ".hermes")]
        verify(rw[0] if rw else None,
               [os.path.join(p, ".__sandbox_forbidden_probe") for p in probes])

    env, dropped = sanitize_env(set(args.keep_env))
    if not args.quiet:
        print(f"sandbox_landlock: ABI={abi} rw={rw} "
              f"net={'off' if args.no_net else (args.allow_net_port or 'bez ograniczeń')} "
              f"read={'restricted' if args.restrict_read else 'open'} "
              f"env_dropped={len(dropped)}", file=sys.stderr)
    try:
        os.execvpe(cmd[0], cmd, env)
    except FileNotFoundError:
        die(f"komenda nie znaleziona: {cmd[0]}", 2)


if __name__ == "__main__":
    main()
