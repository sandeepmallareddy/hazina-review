"""The development history, written out as read-only files before the agent starts.

The agent gets no shell, so it cannot ask git anything. `write_brief` answers ahead of time,
and it answers with a fixed set of files, byte for byte the same for the same repository:
`HISTORY-OVERVIEW.md`, a table of every substantive commit on any ref, newest first, and
`commit-diffs/`, the full patch of each one sampled. The words the model reads there are
fixed text, written out exactly as given here.

A commit is substantive by the thresholds step 1 counts mineable commits with: at least two
paths that are not vendored, generated, locked or binary by their name, and a churn, summed
over every path the commit touched, inside the plausible band. Merges are left out.

Nothing about who wrote a commit is asked of git, so no name or address can be written.

Credential-shaped files are withheld by git pathspec, matched against a path's last segment,
so their content is never produced; the table leaves their names out of the paths it shows.
Two things that name cannot catch: a credential committed under an ordinary name, or renamed
to one, and a file inside a directory whose own name is credential-shaped (`.env/prod`). Both
are ordinary source as far as this module can tell, and are treated as such.

Every git call goes through `hazina_scan.env.run_git`, so the repository's own configuration
cannot start a program here: the filesystem monitor and signature display are off, and log and
show are given `--no-ext-diff --no-textconv`. For a file its repository routes through a text
converter, the line counts in the table are therefore the raw ones rather than the converted
text's; for any other file they are git's own.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from hazina_scan import vocab
from hazina_scan.env import run_git
from hazina_scan.history import MAX_CHURN, MIN_CHURN, MIN_IMPL_FILES

#: Shapes of file name, and wide on purpose where a shape is unmistakable: leaving out the
#: diff of `secrets.yml` loses a line or two of history, and showing it can lose a credential.
#: Bare `*token*` and `*secret*` are deliberately absent -- they would swallow ordinary source.
CREDENTIAL_GLOBS = (
    # environment files
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    # files a tool keeps a login in
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    ".htpasswd",
    ".dockercfg",
    # keys, certificates and the stores that hold them
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.p8",
    "*.jks",
    "*.jceks",
    "*.keystore",
    "*.ppk",
    "*.asc",
    "*.gpg",
    "*.kdbx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials",
    "credentials.*",
    "*.credentials",
    "secrets.*",
    "*.secrets",
    "secrets.yaml",
    "secrets.yml",
    "secrets.json",
    "service-account*.json",
    "*serviceaccount*.json",
    "kubeconfig",
    "*.kubeconfig",
)
#: One pathspec a shape, matching a file of that name at any depth, in any case.
_EXCLUDE_PATHSPECS = tuple(f":(exclude,glob,icase)**/{glob}" for glob in CREDENTIAL_GLOBS)

#: Where things go under the brief's directory. The prompts name both.
OVERVIEW_NAME = "HISTORY-OVERVIEW.md"
DIFF_SUBDIR = "commit-diffs"

#: How much of a commit the table shows beside its numbers, in characters.
PATHS_SHOWN = 8
SUBJECT_CHARS = 160
#: Every git call's ceiling. Nothing about the run's budget lowers it: a brief cut short would
#: be read as a history with less in it.
GIT_TIMEOUT = 900

#: The log walk's layout: a NUL opens each commit and a unit separator parts its fields.
_LOG_FORMAT = "--format=%x00%H%x1f%cd%x1f%s"
#: What a patch opens with: the full hash, the date and the subject, then a blank line.
_PATCH_FORMAT = "--format=%H%n%cd%n%s%n"

# The text the model reads, word for word.
_WITHHELD_NOTE = (
    "\n[one or more credential-shaped files changed in this commit; their "
    "contents are deliberately not included]\n"
)
_TRUNCATED_NOTE = "\n[patch truncated at the size limit]\n"
_PREAMBLE = (
    "# Development history of the repository under measurement",
    "",
    "This file and the patches beside it were produced by the measurement tool, not by the",
    "repository. They exist because the agent reading them has no shell: everything below was",
    "computed with a fixed, read-only `git` argv before the session started.",
    "",
)
_SAMPLED_LINE = (
    "- the patches were sampled at even intervals across the whole history, so "
    "the oldest work is represented as well as the newest"
)
_TABLE_INTRO = (
    f"`patch` names the file in `{DIFF_SUBDIR}/` holding that commit's full diff, and is "
    "empty for a commit that only has the summary line below."
)
_TABLE_HEAD = (
    "| commit | date | files | churn | patch | subject | changed paths |",
    "|---|---|---|---|---|---|---|",
)


def is_credential_path(path: str) -> bool:
    """True when the last segment of `path`, as git printed it, has a credential shape.

    Only the last segment is judged, and a rename is judged as the one string git printed
    for it, quotation marks and braces included; this is what the pathspecs withhold, so the
    count and the table agree with what the patches hold."""
    name = path.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(name, glob.lower()) for glob in CREDENTIAL_GLOBS)


def _non_impl(path: str) -> bool:
    return any(pattern.search(path) for pattern in vocab.NON_IMPL_PATH_PATTERNS)


def _commits(log: str) -> list[dict]:
    """Every commit the log walk named, in git's order, with what the bar is judged on.

    A record that does not open with three fields is dropped, as is a numstat line that is not
    three tab-separated parts. `impl` counts the paths that are first-party source of any kind,
    tests included; `churn` adds every numeric count of every path, so a lockfile's lines count
    though the lockfile is not one of the files, and a binary file adds nothing."""
    found: list[dict] = []
    for record in log.split("\x00"):
        lines = [line for line in record.splitlines() if line.strip()]
        if not lines:
            continue
        fields = lines[0].split("\x1f")
        if len(fields) < 3:
            continue
        commit = {"sha": fields[0], "when": fields[1], "subject": fields[2]}
        commit.update(paths=[], impl=0, churn=0)
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            commit["paths"].append(parts[2])
            commit["impl"] += not _non_impl(parts[2])
            commit["churn"] += sum(int(count) for count in parts[:2] if count.isdigit())
        found.append(commit)
    return found


def _substantive(commit: dict) -> bool:
    return commit["impl"] >= MIN_IMPL_FILES and MIN_CHURN <= commit["churn"] <= MAX_CHURN


def _sample(commits: list, ceiling: int) -> list:
    """At most `ceiling` of them, the first always, the rest at even intervals down the list,
    so that an old migration is as likely to be shown as a recent fix. A ceiling under one
    is no ceiling."""
    if ceiling < 1 or len(commits) <= ceiling:
        return list(commits)
    stride = len(commits) / ceiling
    return [commits[int(i * stride)] for i in range(ceiling)]


def _cell(text: str) -> str:
    """Text that cannot end its own table cell."""
    return text.replace("|", "\\|")


def _row(commit: dict, name: str) -> str:
    # A credential-shaped path is left out by name as well as by content; the count of
    # files beside it is of everything the commit changed, shown or not.
    shown = [path for path in commit["paths"] if not is_credential_path(path)]
    more = f" +{len(shown) - PATHS_SHOWN} more" if len(shown) > PATHS_SHOWN else ""
    patch = f"`{name}`" if name else ""
    # The subject is escaped first and cut after, so an escape can be cut in half.
    subject = _cell(commit["subject"])[:SUBJECT_CHARS]
    paths = ", ".join(_cell(path) for path in shown[:PATHS_SHOWN])
    return (
        f"| `{commit['sha'][:12]}` | {commit['when']} | {len(commit['paths'])} | "
        f"{commit['churn']} | {patch} | {subject} | {paths}{more} |"
    )


def _overview(
    commits: list[dict], names: dict[str, str], refs: int, reachable: str, withheld: int
) -> str:
    """The file the agent is told to read first."""
    lines = [
        *_PREAMBLE,
        f"- refs in the repository: {refs}",
        f"- commits reachable from all refs: {reachable}",
        f"- commits that look like real multi-file development: {len(commits)}",
        f"- of those, full patches written to `{DIFF_SUBDIR}/`: {len(names)}",
    ]
    if withheld:
        lines.append(f"- patches with credential-shaped files withheld: {withheld}")
    if len(commits) > len(names):
        lines.append(_SAMPLED_LINE)
    lines += ["", "## Substantive commits, newest first", "", _TABLE_INTRO, "", *_TABLE_HEAD]
    lines += [_row(commit, names.get(commit["sha"], "")) for commit in commits]
    return "\n".join(lines) + "\n"


def write_brief(
    repo: Path,
    out: Path,
    *,
    max_commits: int = 160,
    max_diff_bytes: int = 200_000,
    max_total_bytes: int = 48_000_000,
) -> dict:
    """Write `HISTORY-OVERVIEW.md` and `commit-diffs/` under `out`, and return the counts.

    The two size ceilings are counted in characters of decoded text, whatever their names
    say. A patch is the commit's hash, date and subject and its diff with credential-shaped
    files left out; when the commit touched such a file a note saying so is added, and then a
    patch over `max_diff_bytes` is cut there and marked. Patches are written for the sampled
    commits in order, numbered by their place in the sample, until the running total reaches
    `max_total_bytes`; a blank answer from git is passed over, keeping its number unused.

    Every git call is given `GIT_TIMEOUT` seconds, and a failure reads as an empty answer, so
    a log walk that named nothing gives a table with no rows and the turn is still taken.

    Returned, and never written: `commits_listed` (merges aside), `commits_substantive`,
    `patches_written`, `patches_empty` (sampled commits git answered blank for),
    `credential_paths_excluded` (patches that carry the withheld note), `bytes`, and `chosen`
    and `patched`, the sampled commits and those given a patch, as full hashes, newest first.
    """
    out = Path(out)
    diffs = out / DIFF_SUBDIR
    diffs.mkdir(parents=True, exist_ok=True)

    def ask(*args: str) -> str:
        return run_git(repo, *args, timeout=GIT_TIMEOUT)

    # Committer dates, and nothing about who: no format here asks git for a name or an address.
    listed = _commits(ask("log", "--all", "--no-merges", "--numstat", "--date=short", _LOG_FORMAT))
    commits = [commit for commit in listed if _substantive(commit)]
    chosen = _sample(commits, max_commits)

    names: dict[str, str] = {}
    empty = withheld = total = 0
    for index, commit in enumerate(chosen):
        if total >= max_total_bytes:
            break
        text = ask("show", commit["sha"], _PATCH_FORMAT, "--date=short", "--", *_EXCLUDE_PATHSPECS)
        if not text.strip():
            empty += 1
            continue
        if any(is_credential_path(path) for path in commit["paths"]):
            withheld += 1
            text += _WITHHELD_NOTE
        if len(text) > max_diff_bytes:
            text = text[:max_diff_bytes] + _TRUNCATED_NOTE
        names[commit["sha"]] = f"{index:04d}-{commit['sha'][:12]}.diff"
        (diffs / names[commit["sha"]]).write_text(text, encoding="utf-8")
        total += len(text)

    refs = sum(
        1 for line in ask("for-each-ref", "--format=%(refname:short)").splitlines() if line.strip()
    )
    reachable = ask("rev-list", "--all", "--count").strip() or "unknown"
    (out / OVERVIEW_NAME).write_text(
        _overview(commits, names, refs, reachable, withheld), encoding="utf-8"
    )
    return {
        "ok": bool(commits),
        "commits_listed": len(listed),
        "commits_substantive": len(commits),
        "patches_written": len(names),
        "patches_empty": empty,
        "credential_paths_excluded": withheld,
        "bytes": total,
        "chosen": [commit["sha"] for commit in chosen],
        "patched": [commit["sha"] for commit in chosen if commit["sha"] in names],
    }
