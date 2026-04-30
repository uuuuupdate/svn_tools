#!/usr/bin/env python3
"""
svn_merge.py – A Python CLI tool for SVN branch merging.

Subcommands:
  merge      Merge commits from another branch into the current branch.
  status     Show the working-copy status.
  log        Show the commit log of a branch/URL.
  conflicts  List files that currently have conflicts.

Run ``python svn_merge.py <subcommand> --help`` for per-command help.
"""

import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict
from textwrap import indent

# ---------------------------------------------------------------------------
# Helpers – subprocess / encoding
# ---------------------------------------------------------------------------

ENCODINGS = ("utf-8", "gbk", "latin-1")


def _decode(data: bytes) -> str:
    """Decode bytes trying UTF-8, GBK, then latin-1 as a last resort."""
    for enc in ENCODINGS:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace")


def run_svn(*args, capture: bool = True, check: bool = True):
    """
    Run an SVN command.

    Returns (stdout_str, stderr_str) when *capture* is True.
    When *capture* is False the command's output goes directly to the
    terminal and (None, None) is returned.
    """
    cmd = ["svn"] + list(args)
    if capture:
        result = subprocess.run(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        stdout = _decode(result.stdout)
        stderr = _decode(result.stderr)
        if check and result.returncode != 0:
            raise RuntimeError(
                f"SVN command failed (exit {result.returncode}):\n"
                f"  Command : {' '.join(cmd)}\n"
                f"  stderr  : {stderr.strip()}"
            )
        return stdout, stderr
    else:
        result = subprocess.run(cmd)
        if check and result.returncode != 0:
            raise RuntimeError(
                f"SVN command failed (exit {result.returncode}):\n"
                f"  Command : {' '.join(cmd)}"
            )
        return None, None


# ---------------------------------------------------------------------------
# SVN helpers
# ---------------------------------------------------------------------------

def get_wc_root() -> str:
    """Return the working-copy root URL reported by ``svn info``."""
    stdout, _ = run_svn("info", "--xml")
    root = ET.fromstring(stdout)
    entry = root.find("entry")
    if entry is None:
        raise RuntimeError("Could not determine working-copy info.")
    url_el = entry.find("url")
    if url_el is None or not url_el.text:
        raise RuntimeError("Could not determine working-copy URL.")
    return url_el.text


def get_merged_revisions(branch_url: str) -> set:
    """
    Return the set of revision numbers already merged from *branch_url*
    into the current working copy (via ``svn mergeinfo``).
    """
    try:
        stdout, _ = run_svn("mergeinfo", "--show-revs=merged", branch_url)
    except RuntimeError:
        return set()
    revs = set()
    for line in stdout.splitlines():
        line = line.strip().lstrip("r")
        if line.isdigit():
            revs.add(int(line))
    return revs


def get_log(url: str, limit: int = 0, revision: str = "") -> list:
    """
    Fetch the SVN log for *url* and return a list of dicts:
      {"rev": int, "author": str, "date": str, "msg": str, "paths": [str]}

    *limit* – maximum entries (0 = no limit).
    *revision* – passed as ``-r <revision>`` when non-empty.
    """
    args = ["log", "--xml", "--verbose", url]
    if limit:
        args += ["--limit", str(limit)]
    if revision:
        args += ["-r", revision]
    stdout, _ = run_svn(*args)
    root = ET.fromstring(stdout)
    entries = []
    for le in root.findall("logentry"):
        rev = int(le.get("revision", 0))
        author = (le.findtext("author") or "").strip()
        date = (le.findtext("date") or "").strip()
        msg = (le.findtext("msg") or "").strip()
        paths = []
        paths_el = le.find("paths")
        if paths_el is not None:
            for p in paths_el.findall("path"):
                if p.text:
                    paths.append(p.text.strip())
        entries.append({
            "rev": rev,
            "author": author,
            "date": date,
            "msg": msg,
            "paths": paths,
        })
    return entries


def get_status_xml() -> list:
    """
    Run ``svn status --xml`` and return a list of dicts:
      {"path": str, "item": str, "props": str}
    """
    stdout, _ = run_svn("status", "--xml")
    root = ET.fromstring(stdout)
    entries = []
    for entry in root.iter("entry"):
        path = entry.get("path", "")
        wc_status = entry.find("wc-status")
        item = ""
        props = ""
        if wc_status is not None:
            item = wc_status.get("item", "")
            props = wc_status.get("props", "")
        entries.append({"path": path, "item": item, "props": props})
    return entries


def get_conflicts() -> list:
    """Return paths that have a conflict status."""
    entries = get_status_xml()
    conflicts = []
    for e in entries:
        if e["item"] == "conflicted" or e["props"] == "conflicted":
            conflicts.append(e["path"])
    return conflicts


def check_no_conflicts(phase: str) -> None:
    """
    Abort with an error message if conflicts are detected.
    *phase* is a human-readable description used in the message.
    """
    conflicts = get_conflicts()
    if conflicts:
        print(f"\n[ERROR] Conflicts detected {phase}:")
        for path in conflicts:
            print(f"  ✗  {path}")
        print(
            "\nPlease resolve the conflicts manually and then commit with:\n"
            "  svn commit -m \"your message\"\n"
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Recursive dependency resolution
# ---------------------------------------------------------------------------

def resolve_dependencies(source_url: str, selected_revs: list,
                          already_merged: set) -> list:
    """
    Starting from *selected_revs* (list of ints), recursively discover
    earlier revisions on *source_url* that touch the same files and have
    not yet been merged (i.e., not in *already_merged*).

    Returns a sorted (ascending) list of all revision numbers that must
    be merged (selected + discovered dependencies).
    """
    # Fetch all log entries for the source branch once to avoid repeated
    # network round-trips.
    print("\n[INFO] Fetching full log from source branch for dependency "
          "analysis …")
    try:
        all_entries = get_log(source_url)
    except RuntimeError as exc:
        print(f"[WARN] Could not fetch log: {exc}")
        return sorted(selected_revs)

    # Build a map: revision → entry
    rev_map = {e["rev"]: e for e in all_entries}

    # Build a map: path → sorted list of revisions that touched that path
    path_revs: dict = {}
    for e in all_entries:
        for p in e["paths"]:
            path_revs.setdefault(p, []).append(e["rev"])

    # Work queue – BFS / DFS doesn't matter here; we just expand the set.
    needed: set = set(selected_revs)
    frontier = list(selected_revs)

    while frontier:
        rev = frontier.pop()
        entry = rev_map.get(rev)
        if entry is None:
            continue
        for path in entry["paths"]:
            for earlier_rev in path_revs.get(path, []):
                if earlier_rev < rev and earlier_rev not in already_merged \
                        and earlier_rev not in needed:
                    needed.add(earlier_rev)
                    frontier.append(earlier_rev)

    return sorted(needed)


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

BATCH_SIZE = 50  # maximum revisions per single ``svn merge`` invocation


def do_merge_revisions(source_url: str, revisions: list) -> None:
    """
    Merge *revisions* (sorted ascending) from *source_url* into the current
    working copy.  Large lists are split into batches of *BATCH_SIZE* so that
    command-line length limits are respected and later revisions are never
    applied before earlier ones.
    """
    batches = [revisions[i:i + BATCH_SIZE]
               for i in range(0, len(revisions), BATCH_SIZE)]

    for batch_idx, batch in enumerate(batches, 1):
        rev_args = ",".join(f"r{r}" for r in batch)
        if len(batches) > 1:
            print(f"\n[INFO] Merging batch {batch_idx}/{len(batches)}: "
                  f"revisions {batch[0]}–{batch[-1]} …")
        else:
            print(f"\n[INFO] Merging revisions: {rev_args} …")

        # Build the merge command using individual -c flags which is cleaner
        # than a comma-separated list across SVN versions.
        merge_args = ["merge"]
        for r in batch:
            merge_args += ["-c", str(r)]
        merge_args.append(source_url)

        try:
            run_svn(*merge_args, capture=False)
        except RuntimeError as exc:
            print(f"\n[ERROR] Merge failed: {exc}")
            sys.exit(1)

        # Check for conflicts after each batch.
        check_no_conflicts(f"after merging batch {batch_idx}")

    print("\n[OK] All revisions merged successfully.")


def do_full_merge(source_url: str) -> None:
    """Merge all unmerged commits from *source_url* into the working copy."""
    print(f"\n[INFO] Performing full merge from {source_url} …")
    try:
        run_svn("merge", source_url, capture=False)
    except RuntimeError as exc:
        print(f"\n[ERROR] Merge failed: {exc}")
        sys.exit(1)
    check_no_conflicts("after full merge")
    print("\n[OK] Full merge completed successfully.")


def ask_commit(revisions: list, source_url: str) -> None:
    """
    Ask the user whether to commit and, if yes, prompt for a message.
    """
    answer = input("\nMerge completed. Would you like to commit now? [y/N] "
                   ).strip().lower()
    if answer not in ("y", "yes"):
        print("[INFO] Commit skipped. You can commit manually when ready.")
        return

    if revisions:
        default_msg = (
            f"Merged revision(s) {','.join(str(r) for r in revisions)} "
            f"from {source_url}"
        )
    else:
        default_msg = f"Merged from {source_url}"

    print(f"Default commit message:\n  {default_msg}")
    custom = input("Enter custom commit message (or press Enter to use "
                   "default): ").strip()
    msg = custom if custom else default_msg

    print(f"\n[INFO] Committing with message: {msg}")
    try:
        run_svn("commit", "-m", msg, capture=False)
    except RuntimeError as exc:
        print(f"\n[ERROR] Commit failed: {exc}")
        sys.exit(1)
    print("[OK] Committed successfully.")


# ---------------------------------------------------------------------------
# Interactive revision selection
# ---------------------------------------------------------------------------

def interactive_select_revisions(entries: list) -> list:
    """
    Present a numbered list of *entries* (log dicts) and let the user pick
    revisions interactively.

    Returns a list of selected revision numbers, or the special string
    ``"all"`` when the user types ``all``.
    """
    print("\nAvailable revisions (newest first):\n")
    for i, e in enumerate(entries, 1):
        short_msg = e["msg"].splitlines()[0][:72] if e["msg"] else "(no message)"
        print(f"  {i:4d}.  r{e['rev']:>7}  {e['author']:<16}  {short_msg}")

    print(
        "\nEnter revision numbers to merge (space or comma-separated),\n"
        "or type 'all' to merge everything, or 'q' to quit:\n"
    )
    raw = input("> ").strip()

    if raw.lower() in ("q", "quit", "exit"):
        print("[INFO] Aborted by user.")
        sys.exit(0)

    if raw.lower() == "all":
        return "all"  # type: ignore[return-value]

    # Parse numbers / ranges like "1 3 5-7"
    tokens = raw.replace(",", " ").split()
    selected_indices = set()
    for tok in tokens:
        if "-" in tok:
            parts = tok.split("-", 1)
            if parts[0].isdigit() and parts[1].isdigit():
                lo, hi = int(parts[0]), int(parts[1])
                selected_indices.update(range(lo, hi + 1))
        elif tok.isdigit():
            selected_indices.add(int(tok))

    revisions = []
    for idx in sorted(selected_indices):
        if 1 <= idx <= len(entries):
            revisions.append(entries[idx - 1]["rev"])
        else:
            print(f"[WARN] Index {idx} is out of range – ignored.")

    if not revisions:
        print("[ERROR] No valid revisions selected.")
        sys.exit(1)

    return revisions


def print_revision_plan(revisions: list, rev_map: dict) -> None:
    """Print a formatted table of revisions that will be merged."""
    print("\nRevisions to be merged (in order):\n")
    print(f"  {'Rev':>8}  {'Author':<16}  {'Message'}")
    print("  " + "-" * 72)
    for r in sorted(revisions):
        e = rev_map.get(r, {})
        author = e.get("author", "?")
        msg = (e.get("msg") or "").splitlines()
        msg = msg[0][:60] if msg else "(no message)"
        print(f"  r{r:>7}  {author:<16}  {msg}")
    print()


# ---------------------------------------------------------------------------
# Subcommand: merge
# ---------------------------------------------------------------------------

def cmd_merge(args: argparse.Namespace) -> None:
    source_url = args.source_url

    # Pre-flight: no existing conflicts in the working copy.
    print("[INFO] Checking for pre-existing conflicts …")
    check_no_conflicts("before merge")

    if args.all:
        # ── Full merge ──────────────────────────────────────────────────
        do_full_merge(source_url)
        ask_commit([], source_url)
        return

    # ── Interactive / selective merge ───────────────────────────────────
    print(f"\n[INFO] Fetching log from {source_url} …")
    limit = args.limit if args.limit else 100
    try:
        entries = get_log(source_url, limit=limit)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)

    if not entries:
        print("[INFO] No commits found on the source branch.")
        sys.exit(0)

    # Filter out already-merged revisions so the list stays manageable.
    already_merged = get_merged_revisions(source_url)
    unmerged = [e for e in entries if e["rev"] not in already_merged]
    if not unmerged:
        print("[INFO] All visible revisions have already been merged.")
        sys.exit(0)

    selected = interactive_select_revisions(unmerged)

    if selected == "all":
        # User chose 'all' in the interactive prompt → full merge.
        do_full_merge(source_url)
        ask_commit([], source_url)
        return

    # ── Recursive dependency resolution ─────────────────────────────────
    print(f"\n[INFO] Resolving dependencies for {len(selected)} selected "
          f"revision(s) …")
    final_revisions = resolve_dependencies(source_url, selected,
                                           already_merged)

    # Build a lookup map from all fetched entries for display.
    rev_map = {e["rev"]: e for e in entries}

    print_revision_plan(final_revisions, rev_map)

    deps = [r for r in final_revisions if r not in selected]
    if deps:
        print(f"[INFO] {len(deps)} additional dependency revision(s) were "
              f"discovered and included: "
              f"{', '.join('r' + str(r) for r in sorted(deps))}\n")

    confirm = input("Proceed with merging the above revision(s)? [y/N] "
                    ).strip().lower()
    if confirm not in ("y", "yes"):
        print("[INFO] Merge cancelled by user.")
        sys.exit(0)

    # ── Perform the merge ────────────────────────────────────────────────
    do_merge_revisions(source_url, final_revisions)
    ask_commit(final_revisions, source_url)


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Print the working-copy status."""
    print("[INFO] Running svn status …\n")
    try:
        run_svn("status", capture=False)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand: log
# ---------------------------------------------------------------------------

def cmd_log(args: argparse.Namespace) -> None:
    """Show the commit log for a URL or the current working copy."""
    url = args.url or "."
    limit = args.limit or 20
    print(f"[INFO] Fetching last {limit} log entries for {url} …\n")
    try:
        entries = get_log(url, limit=limit)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)

    if not entries:
        print("(no log entries found)")
        return

    for e in entries:
        date_short = e["date"][:19].replace("T", " ") if e["date"] else "?"
        print(f"r{e['rev']}  |  {e['author']}  |  {date_short}")
        if e["msg"]:
            print(indent(e["msg"], "    "))
        else:
            print("    (no message)")
        print("-" * 72)


# ---------------------------------------------------------------------------
# Subcommand: conflicts
# ---------------------------------------------------------------------------

def cmd_conflicts(args: argparse.Namespace) -> None:  # noqa: ARG001
    """List files with conflicts in the current working copy."""
    print("[INFO] Checking for conflicts …\n")
    try:
        conflicts = get_conflicts()
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)

    if not conflicts:
        print("[OK] No conflicts detected.")
    else:
        print(f"[WARN] {len(conflicts)} conflicted file(s):\n")
        for path in conflicts:
            print(f"  ✗  {path}")
        print(
            "\nResolve conflicts manually, then mark them resolved with:\n"
            "  svn resolve --accept working <file>\n"
            "and commit:\n"
            "  svn commit -m \"your message\"\n"
        )


# ---------------------------------------------------------------------------
# CLI setup
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="svn_merge",
        description=(
            "SVN branch merge helper.\n\n"
            "Subcommands:\n"
            "  merge      Merge commits from another branch.\n"
            "  status     Show working-copy status.\n"
            "  log        Show commit log.\n"
            "  conflicts  List conflicted files.\n\n"
            "Run 'svn_merge <subcommand> --help' for detailed help."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sub = parser.add_subparsers(dest="command", metavar="<subcommand>")
    sub.required = True

    # ── merge ────────────────────────────────────────────────────────────
    p_merge = sub.add_parser(
        "merge",
        help="Merge commits from another branch into the current branch.",
        description=(
            "Merge commits from SOURCE_URL into the current working copy.\n\n"
            "Without --all the tool shows an interactive list of unmerged\n"
            "commits and lets you pick which ones to include.  It then\n"
            "performs recursive dependency tracing to ensure dependent\n"
            "earlier revisions are also merged.\n\n"
            "Conflicts are checked before and after every merge operation.\n"
            "If any conflict is found the tool exits without committing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_merge.add_argument(
        "source_url",
        metavar="SOURCE_URL",
        help="URL of the source branch to merge from.",
    )
    p_merge.add_argument(
        "--all", "-a",
        action="store_true",
        default=False,
        help=(
            "Merge ALL unmerged commits (equivalent to 'svn merge SOURCE_URL')."
            " Skips interactive selection and dependency tracing."
        ),
    )
    p_merge.add_argument(
        "--limit", "-l",
        type=int,
        default=100,
        metavar="N",
        help="Maximum number of recent log entries to display (default: 100).",
    )
    p_merge.set_defaults(func=cmd_merge)

    # ── status ───────────────────────────────────────────────────────────
    p_status = sub.add_parser(
        "status",
        help="Show the working-copy status.",
        description="Run 'svn status' and display the output.",
    )
    p_status.set_defaults(func=cmd_status)

    # ── log ──────────────────────────────────────────────────────────────
    p_log = sub.add_parser(
        "log",
        help="Show the commit log.",
        description=(
            "Fetch and display the commit log for a URL or the current "
            "working copy."
        ),
    )
    p_log.add_argument(
        "url",
        nargs="?",
        default=None,
        metavar="URL",
        help=(
            "Branch URL or path to show the log for. "
            "Defaults to the current working copy."
        ),
    )
    p_log.add_argument(
        "--limit", "-l",
        type=int,
        default=20,
        metavar="N",
        help="Number of log entries to show (default: 20).",
    )
    p_log.set_defaults(func=cmd_log)

    # ── conflicts ────────────────────────────────────────────────────────
    p_conflicts = sub.add_parser(
        "conflicts",
        help="List conflicted files in the working copy.",
        description=(
            "Check the working copy for conflicted files and print them.\n"
            "If conflicts are found, instructions for resolving them are also "
            "printed."
        ),
    )
    p_conflicts.set_defaults(func=cmd_conflicts)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user.")
        sys.exit(130)


if __name__ == "__main__":
    main()
