#!/usr/bin/env python3
"""skill_telemetry.py — pomiar AKTYWACJI, nie inwentarza.
„Dostępny” != „załadowany” != „wpłynął na wynik”. Czyta state.db WYŁĄCZNIE w trybie
read-only (mode=ro + timeout), żeby nie blokować żywej sesji.

Dwie różne liczby (świadomie):
  * calls_total — ile RAZY wołano skill_view/skill_manage. Cross-check z surowym SQL
    (`messages.tool_name`) musi się zgadzać co do jednego wywołania.
  * targets — ile razy wskazano KONKRETNY skill. Jedno wywołanie może wskazać kilka
    (skill_manage z tablicą `operations`), a może nie wskazać żadnego (unresolved)."""
import argparse, json, os, re, sqlite3, sys, time

HOME = os.path.realpath(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
DB = os.path.join(HOME, "state.db")
DEAD_DAYS = 90
TRACKED = ("skill_view", "skill_manage")


def active_skills(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, "skills")):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git", ".archive")]
        if "SKILL.md" in filenames:
            out[os.path.basename(dirpath)] = os.path.relpath(dirpath, os.path.join(root, "skills"))
    return out


def disabled_skills(config):
    try:
        txt = open(config, encoding="utf-8", errors="replace").read()
    except OSError:
        return set()
    m = re.search(r"^skills:\s*$.*?^\s*disabled:\s*\n((?:\s*-\s*\S+\n)+)", txt, re.M | re.S)
    return set(re.findall(r"-\s*(\S+)", m.group(1))) if m else set()


def extract_names(args):
    """Nazwy skilli wskazane przez wywołanie — także w tablicy `operations`."""
    names = set()
    if isinstance(args, dict):
        for k in ("name", "skill", "skill_name"):
            if isinstance(args.get(k), str) and args[k]:
                names.add(args[k])
        for op in args.get("operations") or []:
            if isinstance(op, dict):
                for k in ("name", "skill", "skill_name"):
                    if isinstance(op.get(k), str) and op[k]:
                        names.add(op[k])
            if isinstance(op, dict) and isinstance(op.get("file_path"), str):
                names.add(op["file_path"].split("/")[0])
    return names


def scan(db_path, tracked=TRACKED):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    cur = con.cursor()
    like = " OR ".join(f"tool_calls LIKE '%\"{t}\"%'" for t in tracked)
    seq = cur.execute(
        f"SELECT session_id, timestamp, tool_calls FROM messages WHERE {like} ORDER BY timestamp"
    ).fetchall()
    raw = cur.execute(
        "SELECT COUNT(*) FROM messages WHERE tool_name IN (%s)" % ",".join("?" * len(tracked)),
        tracked).fetchone()[0]
    n_sessions = cur.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    con.close()

    per, unresolved, calls_total = {}, 0, 0
    sessions_seen = {}
    for sid, ts, blob in seq:
        try:
            calls = json.loads(blob or "[]")
        except Exception:
            continue
        for c in calls:
            fn = c.get("function") or {}
            if fn.get("name") not in tracked:
                continue
            calls_total += 1
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            names = extract_names(args)
            if not names:
                unresolved += 1
                continue
            for nm in names:
                rec = per.setdefault(nm, {"calls": 0, "sessions": set(), "last_use": 0,
                                          "manage": 0, "view": 0})
                rec["calls"] += 1
                rec["sessions"].add(sid)
                rec["last_use"] = max(rec["last_use"], ts or 0)
                rec[fn["name"].split("_")[1]] += 1
            sessions_seen.setdefault(sid, []).append(ts)
    return per, {"calls_total": calls_total, "raw_sql_calls": raw, "unresolved": unresolved,
                 "sessions_total": n_sessions, "sessions_with_skill_calls": len(sessions_seen)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=HOME)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", default=os.path.join(HOME, "eval", "skill-usage.json"))
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args()

    skills = active_skills(a.root)
    disabled = disabled_skills(os.path.join(a.root, "config.yaml"))
    per, meta = scan(a.db)
    now = time.time()
    report = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **meta,
              "skills_active": len(skills), "skills_disabled_in_config": sorted(disabled),
              "cross_check_ok": meta["calls_total"] == meta["raw_sql_calls"],
              "never_loaded": [], "dead_over_90d": [], "loaded": {}}
    for name, path in sorted(skills.items()):
        rec = per.get(name)
        if not rec:
            report["never_loaded"].append({"skill": name, "path": path,
                                           "disabled_in_config": name in disabled})
            continue
        age = (now - rec["last_use"]) / 86400 if rec["last_use"] else None
        entry = {"calls": rec["calls"], "view": rec["view"], "manage": rec["manage"],
                 "sessions": len(rec["sessions"]),
                 "last_use": (time.strftime("%Y-%m-%d", time.localtime(rec["last_use"]))
                              if rec["last_use"] else None),
                 "age_days": round(age, 1) if age is not None else None}
        report["loaded"][name] = entry
        if age is not None and age > DEAD_DAYS:
            report["dead_over_90d"].append({"skill": name, **entry})
    nl_on = [x for x in report["never_loaded"] if not x["disabled_in_config"]]
    report["never_loaded_but_enabled"] = sorted(x["skill"] for x in nl_on)
    report["unknown_targets"] = sorted(set(per) - set(skills))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(report, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if a.summary:
        print(f"wywołań skill_view/manage: {meta['calls_total']} (surowy SQL: {meta['raw_sql_calls']})"
              f" -> cross-check {'OK' if report['cross_check_ok'] else 'ROZJAZD!'}")
        print(f"wywołań bez rozpoznanej nazwy: {meta['unresolved']}")
        print(f"skille aktywne: {len(skills)} · disabled w config: {len(disabled)}")
        print(f"załadowane kiedykolwiek: {len(report['loaded'])} · nigdy: {len(report['never_loaded'])}")
        print(f"  -> nigdy, ale AKTYWNE: {len(nl_on)} {report['never_loaded_but_enabled'][:8]}")
        print(f"  -> nazwy poza repo (usunięte skille w historii): {report['unknown_targets'][:6]}")
        print(f"martwe >{DEAD_DAYS} dni: {len(report['dead_over_90d'])} · zapisano: {a.out}")
    return 0 if report["cross_check_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
