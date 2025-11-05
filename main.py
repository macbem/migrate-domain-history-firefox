#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Prettier width: 80

import argparse
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

FIREFOX_DIR = Path.home() / "Library" / "Application Support" / "Firefox"
PROFILES_DIR = FIREFOX_DIR / "Profiles"
PROFILES_INI = FIREFOX_DIR / "profiles.ini"

OLD_DOMAIN_SUFFIX = "test-domain.co"
NEW_DOMAIN_SUFFIX = "test-domain.co.uk"

VERBOSE = False

def vprint(msg: str):
    if VERBOSE:
        print(msg)

def print_err(msg: str):
    print(f"[!] {msg}", file=sys.stderr)

def print_ok(msg: str):
    print(f"[+] {msg}")

def is_firefox_running() -> bool:
    try:
        return (
            subprocess.run(
                ["pgrep", "-f", "Firefox"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
    except Exception:
        return False

def require_firefox_closed():
    if is_firefox_running():
        print_err("Firefox appears to be running. Please quit Firefox and retry.")
        sys.exit(1)

def parse_profiles_ini() -> List[Dict[str, str]]:
    if not PROFILES_INI.exists():
        raise FileNotFoundError(f"profiles.ini not found at {PROFILES_INI}")
    sections: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    with PROFILES_INI.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                current = {"__name__": line[1:-1]}
                sections.append(current)
            elif "=" in line and current is not None:
                k, v = line.split("=", 1)
                current[k.strip()] = v.strip()
    return sections

def path_from_section(s: Dict[str, str]) -> Path:
    path_str = s.get("Path", "")
    if not path_str:
        return Path("")
    if s.get("IsRelative", "1") == "1":
        return FIREFOX_DIR / path_str
    return Path(path_str)

def profile_health(p: Path) -> Dict[str, bool]:
    return {
        "exists": p.exists(),
        "has_places": (p / "places.sqlite").exists(),
        "has_prefs": (p / "prefs.js").exists(),
        "has_logins": (p / "logins.json").exists(),
        "has_cookies": (p / "cookies.sqlite").exists(),
    }

def pick_best_profile(
    forced_path: Optional[Path], forced_name: Optional[str]
) -> Path:
    if forced_path:
        p = forced_path.expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"--profile-path not found: {p}")
        vprint(f"Using forced profile path: {p}")
        return p

    sections = parse_profiles_ini()
    candidates = [
        s for s in sections if s.get("__name__", "").lower().startswith("profile")
    ]
    # If name forced, try to match by dir name suffix (profile dir names start with
    # random prefix and end with .<name>)
    if forced_name:
        for s in candidates:
            p = path_from_section(s)
            if p.name.endswith("." + forced_name):
                vprint(f"Using --profile match: {p}")
                return p
        raise RuntimeError(f"--profile '{forced_name}' not found in profiles.ini")

    # Primary: Default=1
    default_section = next((s for s in candidates if s.get("Default") == "1"), None)
    default_path = path_from_section(default_section) if default_section else None

    # Build a ranked list of existing profile dirs
    existing = [path_from_section(s) for s in candidates if path_from_section(s).exists()]

    def score(p: Path) -> Tuple[int, int]:
        h = profile_health(p)
        # Heuristic: prefer profiles that have places.sqlite, prefs.js, cookies.sqlite
        # and whose name endswith '.default-release'
        score1 = int(h["has_places"]) + int(h["has_prefs"]) + int(h["has_cookies"])
        score2 = 1 if p.name.endswith(".default-release") else 0
        return (score1, score2)

    ranked = sorted(existing, key=score, reverse=True)

    chosen = None
    if default_path and default_path.exists():
        # If default looks healthy, use it; else fall back to best-ranked
        h = score(default_path)[0]
        if h >= 2:
            chosen = default_path
    if chosen is None and ranked:
        chosen = ranked[0]

    if not chosen:
        raise RuntimeError("Could not determine a valid Firefox profile directory.")

    vprint("Profile candidates:")
    for p in ranked:
        h = profile_health(p)
        vprint(
            f"  - {p} "
            f"(places={int(h['has_places'])}, prefs={int(h['has_prefs'])}, "
            f"cookies={int(h['has_cookies'])}, logins={int(h['has_logins'])})"
        )
    vprint(f"Selected profile: {chosen}")
    return chosen

def detect_default_profile_path(
    forced_path: Optional[Path] = None, forced_name: Optional[str] = None
) -> Path:
    p = pick_best_profile(forced_path, forced_name)
    vprint(f"Detected default profile: {p}")
    return p

def timestamp_str() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

def summarize_dir(path: Path) -> Tuple[int, int]:
    total_bytes = 0
    total_files = 0
    for root, _, files in os.walk(path):
        for f in files:
            p = Path(root) / f
            try:
                total_bytes += p.stat().st_size
                total_files += 1
            except FileNotFoundError:
                pass
    return total_files, total_bytes

def human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    s = float(n)
    for u in units:
        if s < 1024.0:
            return f"{s:.1f} {u}"
        s /= 1024.0
    return f"{s:.1f} PB"

def copytree_verbose(src: Path, dst: Path):
    if dst.exists():
        raise FileExistsError(f"Destination already exists: {dst}")
    vprint(f"Copying profile directory:\n  from: {src}\n  to:   {dst}")
    shutil.copytree(src, dst)
    files, bytes_ = summarize_dir(dst)
    print_ok(f"Copied profile to: {dst}")
    print_ok(f"Backup contains {files} files, total size {human_size(bytes_)}")

def backup_profile(dst_dir: Optional[Path], prof: Path):
    require_firefox_closed()
    ts = timestamp_str()
    if dst_dir is None:
        backups_root = Path.cwd() / "firefox-profile-backups"
        backups_root.mkdir(parents=True, exist_ok=True)
        dst = backups_root / f"{prof.name}-backup-{ts}"
    else:
        d = dst_dir.expanduser().resolve()
        d.mkdir(parents=True, exist_ok=True)
        dst = d / f"{prof.name}-backup-{ts}"
    print_ok(f"Backup destination: {dst}")
    copytree_verbose(prof, dst)
    print_ok("Backup completed successfully.")

def restore_profile(src_dir: Path, prof: Path):
    require_firefox_closed()
    src = src_dir.expanduser().resolve()
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"Backup path not found or not a directory: {src}")

    pre_restore_dir = prof.parent / f"{prof.name}-pre-restore-{timestamp_str()}"
    print_ok(f"Creating pre-restore backup at: {pre_restore_dir}")
    copytree_verbose(prof, pre_restore_dir)

    print_ok(f"Clearing current profile contents: {prof}")
    for item in prof.iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    print_ok(f"Restoring from: {src}")
    for item in src.iterdir():
        dest = prof / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    print_ok("Restore completed. You can relaunch Firefox now.")

def open_sqlite(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=rwc", uri=True, timeout=15.0)

def backup_file(path: Path) -> Path:
    b = path.with_suffix(path.suffix + f".pre_migration_{timestamp_str()}.bak")
    shutil.copy2(path, b)
    vprint(f"Backed up {path.name} to: {b}")
    return b

HOST_SUFFIX_RE = re.compile(
    rf"(https?://)([^/@]+?){re.escape(OLD_DOMAIN_SUFFIX)}(?=[:/]|$)", re.IGNORECASE
)

def replace_host_suffix_in_url(url: str) -> Optional[str]:
    def _repl(m: re.Match) -> str:
        return f"{m.group(1)}{m.group(2)}{NEW_DOMAIN_SUFFIX}"
    new_url, n = HOST_SUFFIX_RE.subn(_repl, url, count=1)
    return new_url if n > 0 else None

def rewrite_history(prof: Path, dry_run: bool = False) -> Tuple[int, int]:
    db_path = prof / "places.sqlite"
    if not db_path.exists():
        raise FileNotFoundError(f"places.sqlite not found at: {db_path}")
    backup_file(db_path)
    conn = open_sqlite(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        like_pattern = f"%{OLD_DOMAIN_SUFFIX}%"
        rows = conn.execute(
            "SELECT id, url FROM moz_places WHERE url LIKE ?", (like_pattern,)
        ).fetchall()
        candidate_count = len(rows)
        if dry_run:
            precise = sum(1 for _, url in rows if replace_host_suffix_in_url(url))
            print_ok(f"Dry-run history: candidates {candidate_count}, matches {precise}")
            return candidate_count, 0
        conn.execute("BEGIN IMMEDIATE;")
        updated = 0
        for pid, url in rows:
            new_url = replace_host_suffix_in_url(url)
            if new_url and new_url != url:
                conn.execute(
                    "UPDATE moz_places SET url = ? WHERE id = ?", (new_url, pid)
                )
                updated += 1
        conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(
                "UPDATE moz_origins SET host = REPLACE(host, ?, ?)",
                (OLD_DOMAIN_SUFFIX, NEW_DOMAIN_SUFFIX),
            )
            conn.execute(
                "UPDATE moz_origins SET rev_host = REPLACE(rev_host, ?, ?)",
                (OLD_DOMAIN_SUFFIX[::-1], NEW_DOMAIN_SUFFIX[::-1]),
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
        print_ok(f"Updated {updated} history URL rows.")
        return candidate_count, updated
    finally:
        conn.close()

def rewrite_bookmarks(prof: Path, dry_run: bool = False) -> Tuple[int, int]:
    db_path = prof / "places.sqlite"
    if not db_path.exists():
        raise FileNotFoundError(f"places.sqlite not found at: {db_path}")
    backup_file(db_path)
    conn = open_sqlite(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        like_pattern = f"%{OLD_DOMAIN_SUFFIX}%"
        rows = conn.execute(
            """
            SELECT mp.id, mp.url
            FROM moz_bookmarks mb
            JOIN moz_places mp ON mp.id = mb.fk
            WHERE mp.url LIKE ?
            """,
            (like_pattern,),
        ).fetchall()
        candidate_count = len(rows)
        if dry_run:
            precise = sum(1 for _, url in rows if replace_host_suffix_in_url(url))
            print_ok(
                f"Dry-run bookmarks: candidates {candidate_count}, matches {precise}"
            )
            return candidate_count, 0
        conn.execute("BEGIN IMMEDIATE;")
        updated = 0
        for pid, url in rows:
            new_url = replace_host_suffix_in_url(url)
            if new_url and new_url != url:
                conn.execute(
                    "UPDATE moz_places SET url = ? WHERE id = ?", (new_url, pid)
                )
                updated += 1
        conn.commit()
        print_ok(f"Updated {updated} bookmark URLs.")
        return candidate_count, updated
    finally:
        conn.close()

def rewrite_form_history(prof: Path, dry_run: bool = False) -> Tuple[int, int]:
    db_path = prof / "formhistory.sqlite"
    if not db_path.exists():
        print_ok("formhistory.sqlite not found; skipping form history.")
        return 0, 0
    backup_file(db_path)
    conn = open_sqlite(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(moz_formhistory);")]
        if "origin" not in cols:
            print_ok("moz_formhistory has no 'origin' column; nothing to rewrite.")
            return 0, 0
        like_pattern = f"%{OLD_DOMAIN_SUFFIX}%"
        rows = conn.execute(
            "SELECT id, origin FROM moz_formhistory WHERE origin LIKE ?",
            (like_pattern,),
        ).fetchall()
        candidate_count = len(rows)
        if dry_run:
            precise = sum(1 for _, o in rows if o and replace_host_suffix_in_url(o))
            print_ok(
                f"Dry-run formhistory: candidates {candidate_count}, matches {precise}"
            )
            return candidate_count, 0
        conn.execute("BEGIN IMMEDIATE;")
        updated = 0
        for fid, origin in rows:
            if not origin:
                continue
            new_origin = replace_host_suffix_in_url(origin)
            if new_origin and new_origin != origin:
                conn.execute(
                    "UPDATE moz_formhistory SET origin = ? WHERE id = ?",
                    (new_origin, fid),
                )
                updated += 1
        conn.commit()
        print_ok(f"Updated {updated} form history origin entries.")
        return candidate_count, updated
    finally:
        conn.close()

def rewrite_cookies(prof: Path, dry_run: bool = False) -> Tuple[int, int]:
    db_path = prof / "cookies.sqlite"
    if not db_path.exists():
        print_ok("cookies.sqlite not found; skipping cookies.")
        return 0, 0

    backup_file(db_path)
    conn = open_sqlite(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        like_pattern = f"%{OLD_DOMAIN_SUFFIX}%"
        rows = conn.execute(
            """
            SELECT id, name, host, path, originAttributes
            FROM moz_cookies
            WHERE host LIKE ?
            """,
            (like_pattern,),
        ).fetchall()

        def replace_cookie_host(host: str) -> Optional[str]:
            if not host:
                return None
            leading_dot = host.startswith(".")
            core = host[1:] if leading_dot else host
            if core.lower().endswith(OLD_DOMAIN_SUFFIX):
                new_core = core[: -len(OLD_DOMAIN_SUFFIX)] + NEW_DOMAIN_SUFFIX
                return ("." if leading_dot else "") + new_core
            return None

        candidates = 0
        will_update = []
        for cid, name, host, path, oa in rows:
            new_host = replace_cookie_host(host or "")
            if new_host and new_host != host:
                candidates += 1
                will_update.append((cid, name, host, path, oa, new_host))

        if dry_run:
            # Count potential conflicts by probing existing target rows
            conflicts = 0
            for _, name, _, path, oa, new_host in will_update:
                exists = conn.execute(
                    """
                    SELECT 1 FROM moz_cookies
                    WHERE name = ? AND host = ? AND path = ? AND originAttributes = ?
                    LIMIT 1
                    """,
                    (name, new_host, path, oa),
                ).fetchone()
                if exists:
                    conflicts += 1
            print_ok(
                f"Dry-run cookies: candidates {candidates}, "
                f"would-update {len(will_update)}, potential conflicts {conflicts}"
            )
            return candidates, 0

        updated = 0
        conflicts_resolved = 0
        conn.execute("BEGIN IMMEDIATE;")
        for cid, name, old_host, path, oa, new_host in will_update:
            # If a row with the target tuple exists, delete it first to satisfy UNIQUE
            exists = conn.execute(
                """
                SELECT id FROM moz_cookies
                WHERE name = ? AND host = ? AND path = ? AND originAttributes = ?
                LIMIT 1
                """,
                (name, new_host, path, oa),
            ).fetchone()
            if exists:
                conn.execute("DELETE FROM moz_cookies WHERE id = ?", (exists[0],))
                conflicts_resolved += 1

            # Now update the source row to the new host
            conn.execute(
                "UPDATE moz_cookies SET host = ? WHERE id = ?",
                (new_host, cid),
            )
            updated += 1

        conn.commit()
        print_ok(
            f"Updated {updated} cookie host entries. "
            f"Conflicts resolved by delete-then-update: {conflicts_resolved}."
        )
        return candidates, updated
    except Exception as e:
        conn.rollback()
        print_err(f"Cookie rewrite failed: {e}")
        return 0, 0
    finally:
        conn.close()

def rewrite_logins(prof: Path, dry_run: bool = False) -> Tuple[int, int]:
    path = prof / "logins.json"
    if not path.exists():
        print_ok("logins.json not found; skipping saved logins.")
        return 0, 0
    backup_file(path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    cand = 0
    upd = 0
    for login in data.get("logins", []):
        for field in ("hostname", "formSubmitURL"):
            val = login.get(field)
            if val and OLD_DOMAIN_SUFFIX in val:
                cand += 1
                new_val = replace_host_suffix_in_url(val) or val
                if new_val != val:
                    login[field] = new_val
                    upd += 1
        realm = login.get("httpRealm")
        if realm and realm.startswith(("http://", "https://")) and OLD_DOMAIN_SUFFIX in realm:
            cand += 1
            new_realm = replace_host_suffix_in_url(realm) or realm
            if new_realm != realm:
                login["httpRealm"] = new_realm
                upd += 1
    if dry_run:
        print_ok(f"Dry-run logins.json: candidates {cand}, matches {upd} (est.)")
        return cand, 0
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print_ok(f"Updated {upd} saved login URL fields.")
    return cand, upd

def rewrite_all(prof: Path, dry_run: bool = False):
    totals = {"candidates": 0, "updated": 0}
    for fn in (
        rewrite_history,
        rewrite_bookmarks,
        rewrite_form_history,
        rewrite_cookies,
        rewrite_logins,
    ):
        c, u = fn(prof, dry_run=dry_run)
        totals["candidates"] += c
        totals["updated"] += u
    print_ok(
        f"All done. Candidates seen: {totals['candidates']}. "
        f"Rows/fields updated: {totals['updated']}."
    )

def list_profiles():
    sections = parse_profiles_ini()
    candidates = [
        s for s in sections if s.get("__name__", "").lower().startswith("profile")
    ]
    if not candidates:
        print_err("No profiles found in profiles.ini")
        return
    print("Found profiles:")
    for s in candidates:
        p = path_from_section(s)
        h = profile_health(p)
        flags = []
        if s.get("Default") == "1":
            flags.append("Default=1")
        if p.name.endswith(".default-release"):
            flags.append("default-release")
        size = "n/a"
        if p.exists():
            try:
                files, bytes_ = summarize_dir(p)
                size = f"{files} files, {human_size(bytes_)}"
            except Exception:
                pass
        print(
            f"- {p} [{', '.join(flags) if flags else 'no-flags'}] "
            f"(places={int(h['has_places'])}, prefs={int(h['has_prefs'])}, "
            f"cookies={int(h['has_cookies'])}, logins={int(h['has_logins'])}); {size}"
        )

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate.py",
        description="Backup/restore Firefox profile and rewrite URLs for domain change (macOS).",
        add_help=False,
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose output.")
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Profile name suffix (e.g., default-release).",
    )
    parser.add_argument(
        "--profile-path",
        type=str,
        default=None,
        help="Explicit path to profile directory.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("help")
    sub.add_parser("list-profiles")

    p_backup = sub.add_parser("backup")
    p_backup.add_argument("--dest", type=str, default=None)

    p_restore = sub.add_parser("restore")
    p_restore.add_argument("--from", dest="from_path", type=str, required=True)

    p_hist = sub.add_parser("rewrite-history")
    p_hist.add_argument("--dry-run", action="store_true")

    p_all = sub.add_parser("rewrite-all")
    p_all.add_argument("--dry-run", action="store_true")

    return parser

def main(argv=None):
    global VERBOSE
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    VERBOSE = bool(args.verbose)

    forced_path = Path(args.profile_path).expanduser() if args.profile_path else None
    forced_name = args.profile

    if not args.command or args.command == "help":
        print("Usage:")
        print("  python3 migrate.py [--verbose] [--profile NAME | --profile-path PATH] help")
        print("  python3 migrate.py [--verbose] [--profile/--profile-path] list-profiles")
        print("  python3 migrate.py [--verbose] [--profile/--profile-path] backup [--dest DIR]")
        print("  python3 migrate.py [--verbose] [--profile/--profile-path] restore --from DIR")
        print("  python3 migrate.py [--verbose] [--profile/--profile-path] rewrite-history [--dry-run]")
        print("  python3 migrate.py [--verbose] [--profile/--profile-path] rewrite-all [--dry-run]")
        print("")
        print("Tips:")
        print("- list-profiles shows all profiles and which contain places.sqlite.")
        print("- If automatic detection picked egujunnf.default, try --profile default-release.")
        return

    if args.command == "list-profiles":
        list_profiles()
        return

    # Resolve the profile now
    try:
        prof = detect_default_profile_path(forced_path, forced_name)
    except Exception as e:
        print_err(str(e))
        sys.exit(1)

    try:
        if args.command == "backup":
            dst = Path(args.dest).expanduser() if args.dest else None
            backup_profile(dst, prof)
        elif args.command == "restore":
            restore_profile(Path(args.from_path), prof)
        elif args.command == "rewrite-history":
            require_firefox_closed()
            c, u = rewrite_history(prof, dry_run=args.dry_run)
            if args.dry_run:
                print_ok(f"Dry-run complete. History candidates: {c}.")
            else:
                print_ok(f"History rewrite complete. Candidates: {c}. Updated: {u}.")
        elif args.command == "rewrite-all":
            require_firefox_closed()
            rewrite_all(prof, dry_run=args.dry_run)
        else:
            print_err("Unknown command. Use 'help'.")
            sys.exit(1)
    except Exception as e:
        print_err(str(e))
        sys.exit(1)

if __name__ == "__main__":
    main()
