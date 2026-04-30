# svn_tools

A Python command-line tool for SVN branch merging and workflow assistance.

## Requirements

* Python 3.7+
* `svn` available on the system `PATH`

## Usage

```
python svn_merge.py <subcommand> [options]
```

Run `python svn_merge.py --help` for a list of subcommands.

---

### `merge` – Merge commits from another branch

```
python svn_merge.py merge SOURCE_URL [--all] [--limit N]
```

| Option | Description |
|--------|-------------|
| `SOURCE_URL` | URL of the source branch to merge from |
| `--all` / `-a` | Merge **all** unmerged commits (full merge). Skips interactive selection. |
| `--limit N` / `-l N` | Max log entries to show in interactive mode (default: 100) |

#### Interactive mode (no `--all`)

1. The tool fetches the log of `SOURCE_URL` and filters out already-merged revisions.
2. A numbered list is displayed; enter space/comma-separated indices, ranges (`3-7`), or `all`.
3. For each selected revision the tool **recursively traces** earlier revisions that touch any of the same files and haven't been merged yet.  All discovered dependency revisions are added automatically.
4. A full summary of every revision that will be merged is printed for confirmation before anything changes in the working copy.
5. Large revision sets are split into batches of up to 50 and applied in ascending order.
6. Conflicts are checked after each batch; the process halts if any are found.
7. On success, you are asked whether to commit and may supply a custom message (or accept the auto-generated `Merged revision(s) … from …` default).

#### Full merge (`--all`)

Runs `svn merge SOURCE_URL` directly, then checks for conflicts and offers to commit.

---

### `status` – Working-copy status

```
python svn_merge.py status
```

Runs `svn status` and prints the output to the terminal.

---

### `log` – Commit log

```
python svn_merge.py log [URL] [--limit N]
```

Shows the most recent `N` log entries (default 20) for the given URL or the current working copy.

---

### `conflicts` – List conflicted files

```
python svn_merge.py conflicts
```

Scans the working copy for conflicted files and prints them with resolution instructions.

---

## Windows encoding

The tool captures all SVN output as raw bytes and tries decoding in order: **UTF-8 → GBK → latin-1**. This prevents `UnicodeDecodeError` crashes on Chinese Windows systems where SVN output is GBK-encoded.

## Example workflow

```bash
# See what's on the feature branch
python svn_merge.py log http://svn.example.com/repo/branches/feature --limit 50

# Interactively pick specific revisions to merge
python svn_merge.py merge http://svn.example.com/repo/branches/feature

# Or merge everything at once
python svn_merge.py merge http://svn.example.com/repo/branches/feature --all

# Check for conflicts after a manual merge
python svn_merge.py conflicts
```
