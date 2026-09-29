"""One run, start to finish: step 1's documents, with what the review found added to them.

Nothing is measured twice and nothing is written twice. The deterministic measurement, the
three files, the folder names and the zip writer are all step 1's, called as they stand. What
this module adds sits between them: a scratch copy of the repository and history, two
concurrent provider turns, and their shareable measurements. Each repository's folder, named
after its own directory, holds exactly the four files, and the zip holds those folders.

The two halves of `measurement.json` each pass their own boundary before they meet. Step 1's
comes back from `orchestrator.measure` already declared, scrubbed and audited; the `material`
block goes through `emit.material_block`. They are joined here, after both, because step 1's
schema has no business knowing this block exists and would rightly refuse it.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import stat
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from hazina_review import __version__, brief, emit, record, snapshot, support
from hazina_review.lanes import census, mining
from hazina_review.providers.registry import (
    DEFAULT_MODELS,
    ProviderNotIsolated,
    ProviderUnavailable,
    isolation_flags,
    resolve,
)
from hazina_scan import cli as scan
from hazina_scan import env, fileguard, orchestrator

#: This tool's own output directory and archive. hazina-scan writes `hazina-out` and
#: `hazina-out.zip`; sharing either would let one tool's run remove or overwrite the other's.
DEFAULT_OUT = "hazina-review-out"
ZIP_NAME = "hazina-review-out.zip"


class Refused(ValueError):
    """The run cannot start as asked, and the message is the whole of why."""


#: The files a review writes into each repository's folder, and packs into the zip.
OUTPUT_FILES = (*scan.OUTPUT_FILES, "codebase_repo_mining.json")


def _selected(repos: list[Path], out_dir: Path) -> tuple[list[Path], list[str]]:
    """The repositories as step 1 would select them, and the folder each one writes to."""
    chosen: list[Path] = []
    for raw in repos:
        path = Path(raw).expanduser().resolve()
        if not scan.is_git_repo(path):
            raise Refused(f"not a git repository: {path}")
        chosen.append(path)
    chosen = list(dict.fromkeys(chosen))
    names = scan.output_names(chosen)
    _safe_to_write(out_dir, chosen, names)
    return chosen, names


def _safe_to_write(out_dir: Path, chosen: list[Path], names: list[str]) -> None:
    """Raise `Refused` when writing these folders would go through a link or into a
    repository being reviewed."""
    blocker = scan._repo_containing(out_dir, chosen)
    if blocker is None:
        # Before the clash check, which resolves each folder: a link waiting at a folder is
        # named as a link, with what to do about it.
        unsafe = scan.unsafe_output(
            out_dir, names, OUTPUT_FILES, local_index=False, zip_name=ZIP_NAME
        )
        if unsafe is not None:
            raise Refused(unsafe)
        blocker = scan.out_name_clash(out_dir, chosen, names)
    if blocker is not None:
        raise Refused(
            f"--out would write inside the repository being reviewed, {blocker}. Choose a "
            f"directory outside it, for example --out {blocker.parent / DEFAULT_OUT}"
        )


#: What an earlier version of this tool left in an output directory, and why each is removed:
#: the local index beside the folders, and the raw answers inside each folder.
OLD_LOCAL_FILES = (
    ("INDEX.local.txt", "the local index from an earlier run"),
    ("DETAIL.local.md", "raw findings from an earlier run"),
)

#: The first line of the local index an earlier version wrote. It wrote the index with step 1's
#: own writer, so this is also the line hazina-scan writes: the line alone does not say which
#: tool wrote the file, and the folders the index names are what settle it.
OLD_INDEX_FIRST_LINE = (
    "# hazina-scan index -- LOCAL ONLY. This file is not included in hazina-out.zip."
)

#: What an earlier version's folder held beside its raw findings. A folder without these is
#: not one this tool wrote, whatever else is in it.
OLD_FOLDER_FILES = ("measurement.json", "codebase_repos.json")

#: The most of an old index that is read to judge it. The index is one short line per folder.
_INDEX_READ_LIMIT = 1 << 20


def _regular(path: Path) -> bool:
    """A regular file at `path` itself, never what a link there points at."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _earlier_review_folder(folder: Path) -> bool:
    """Does `folder` hold this tool's output files from an earlier run?"""
    return all(_regular(folder / name) for name in OLD_FOLDER_FILES)


def _earlier_review_index(out_dir: Path, index: Path) -> bool:
    """Is `index` the local index an earlier version of this tool wrote?

    Its first line is the one that version wrote, and every folder it names that is still
    there is an earlier review's folder with its raw findings in it, and at least one is.
    hazina-scan writes the same first line, and its folders never hold raw findings.
    """
    try:
        with fileguard.open_binary(index, None) as handle:
            text = handle.read(_INDEX_READ_LIMIT).decode("utf-8")
    except (OSError, ValueError):
        return False
    lines = text.splitlines()
    if not lines or lines[0] != OLD_INDEX_FIRST_LINE:
        return False
    detail = OLD_LOCAL_FILES[1][0]
    named = 0
    for line in lines[1:]:
        if not line or line.startswith("#"):
            continue
        name = line.split("\t", 1)[0]
        if name in ("", ".", "..") or "/" in name or "\\" in name:
            return False
        folder = out_dir / name
        if not os.path.lexists(folder):
            continue
        if not (
            fileguard.is_directory(folder, None)
            and _earlier_review_folder(folder)
            and _regular(folder / detail)
        ):
            return False
        named += 1
    return named > 0


def _old_local_files(out_dir: Path) -> tuple[list[tuple[Path, str]], list[Path]]:
    """The earlier version's local files in `out_dir`, each with why it goes; and the files
    by those names that are left, because nothing says this tool wrote them.

    Only `INDEX.local.txt` directly in `out_dir` and `DETAIL.local.md` directly in one of its
    folders, and only a regular file at that name. A detail file goes only from a folder that
    also holds this tool's output files from an earlier run; the index goes only when it is
    the one an earlier version wrote (`_earlier_review_index`). A linked folder is never
    entered. A link at either name is refused, as a link at any output path is, and before
    anything is removed: deleting it would destroy something that was put there on purpose,
    and leaving it would keep what it points at beside the results.
    """
    (index, index_why), (detail, detail_why) = OLD_LOCAL_FILES
    candidates = [(out_dir / index, index_why)]
    try:
        with os.scandir(out_dir) as listing:
            folders = sorted(e.path for e in listing if e.is_dir(follow_symlinks=False))
    except (FileNotFoundError, NotADirectoryError):
        return [], []
    candidates += [(Path(folder) / detail, detail_why) for folder in folders]
    regular = []
    for path, why in candidates:
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise Refused(
                f"refusing to remove the old {path.name} at {path}: it is a symbolic link, "
                f"and nothing a link points at is touched. Remove the link, or choose an "
                f"empty directory for --out."
            )
        if stat.S_ISREG(mode):
            regular.append((path, why))
    # Judged before anything goes: the index is recognised by the raw findings beside it.
    found, left = [], []
    for path, why in regular:
        if path.name == index:
            ours = _earlier_review_index(out_dir, path)
        else:
            ours = _earlier_review_folder(path.parent)
        (found if ours else left).append((path, why) if ours else path)
    return found, left


def _remove_old_local_files(out_dir: Path) -> None:
    """Remove what `_old_local_files` found, saying so once for each file, and say once for
    each file it left why it is still there."""
    found, left = _old_local_files(out_dir)
    for path in left:
        _tell(
            "",
            f"left {path} in place: it does not look like this tool's output from an earlier "
            "run, so it was not removed",
        )
    for path, why in found:
        path.unlink()  # never follows a link, and a link was refused above
        _tell("", f"removed old {path.name} from {path.parent} ({why})")


_TELLING = threading.Lock()


def _tell(label: str, message: str) -> None:
    """Progress and warnings go to stderr, under the repository's label. Each line is written
    whole, under a lock: both lanes can end at the same moment, and `print` writes the text
    and its newline separately."""
    line = f"{label}: {message}" if label else message
    with _TELLING:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()


#: Each model lane's own ceiling, in seconds, when the operator names none. Four hours: far
#: past where any real repository lands, so it only stops a command
#: that has hung. The run's budget is what bounds a run.
DEFAULT_LANE_TIMEOUT = 14400


#: How often, in seconds, a repository whose model sessions are still running says so.
HEARTBEAT_SECONDS = 60

#: Each model lane as the person reading the progress lines knows it.
LANE_WORDS = {"census": "census", "mining": "task mining"}


class _QuietClock(orchestrator.LaneClock):
    """Step 1's clock, keeping its record for the block's note and printing nothing: this
    tool's own progress lines say what is happening, in words meant for a person."""

    def _say(self, message: str) -> None:
        pass


class _Watch:
    """One repository's two model lanes, as they are reported: a line when each ends, and
    while any is still running, one every `interval` seconds naming those still at work."""

    def __init__(self, label: str, interval: float, *, provider: str, model, mine_n: int):
        self.label, self.interval = label, interval
        self.provider, self.model, self.mine_n = provider, model, mine_n
        self.outcomes: dict[str, dict] = {}
        self._started: dict[str, float] = {}
        self._lock = threading.Lock()
        self._quiet = threading.Event()
        self._beat: threading.Thread | None = None

    def __enter__(self):
        self._beat = threading.Thread(target=self._beating, daemon=True)
        self._beat.start()
        return self

    def __exit__(self, *exc) -> None:
        self._quiet.set()
        if self._beat is not None:
            self._beat.join(timeout=2)

    def _beating(self) -> None:
        while not self._quiet.wait(self.interval):
            now = time.monotonic()
            with self._lock:
                running = [
                    f"{LANE_WORDS[name]} {support.short(now - began)}"
                    for name, began in self._started.items()
                    if name not in self.outcomes
                ]
            if running:
                _tell(self.label, f"still working ({', '.join(running)})")

    def begin(self, name: str) -> None:
        with self._lock:
            self._started[name] = time.monotonic()

    def end(self, name: str, block: dict | None, outcome: dict) -> None:
        with self._lock:
            took = time.monotonic() - self._started.get(name, time.monotonic())
            self.outcomes[name] = {**outcome, "seconds": took}
        word, spent = LANE_WORDS[name], support.duration(took)
        kind = outcome.get("kind")
        if kind is not None:
            why = support.reason(kind, self.provider, self.model)
            _tell(self.label, f"{word} did not complete after {spent}: {why}")
        elif name == "census":
            _tell(self.label, f"census done in {spent}")
        else:
            found = block["total_candidates"]
            said = f"{found} task{'s' if found != 1 else ''}"
            if found >= self.mine_n:
                said += ", the most it was asked for, so the counts are a lower bound"
            _tell(self.label, f"task mining done in {spent} ({said})")


def _ceiling(deadline, cap: int) -> int:
    """A lane's seconds: its own ceiling or what the run has left, whichever is less, and never
    under one second. A lane is always started; one given too little comes back timed out."""
    return max(1, deadline.slice(cap))


def _model_lanes(
    repo: Path,
    clock,
    deadline,
    *,
    provider: str,
    model: str | None,
    mine_n: int = mining.DEFAULT_N,
    census_timeout: int = DEFAULT_LANE_TIMEOUT,
    mine_timeout: int = DEFAULT_LANE_TIMEOUT,
    watch: _Watch | None = None,
):
    """Both model lanes against one scratch copy, removed only after both finish.

    The agent's read tools are aimed at a directory, so the directory it is given is never
    the operator's: it is the files committed at `HEAD` with the credential-shaped ones left
    out, beside the history prepared the same way.

    Each lane's seconds are settled once, before anything else here starts, as its own ceiling
    or what the run has left. Preparing the copy and the history does not come out of them.
    The history is written and the turns taken whatever the history turned out to hold; only
    a repository with nothing committed, or a copy that came out incomplete, has no turn.
    """
    model = model or DEFAULT_MODELS[provider]
    census_seconds = _ceiling(deadline, census_timeout)
    mine_seconds = _ceiling(deadline, mine_timeout)

    def no_turn(reason: str):
        return {
            "census": (census.no_turn(provider, model, reason, census_seconds), None),
            "mining": (mining.no_turn(provider, model, reason), None),
            "no_turn": reason,
        }

    if not env.run_git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").strip():
        return no_turn("nothing_to_read")
    with tempfile.TemporaryDirectory(
        prefix="hazina-review-run-", ignore_cleanup_errors=True
    ) as scratch:
        copy, history = Path(scratch) / "snapshot", Path(scratch) / "history"
        watch = watch or _Watch(
            "", HEARTBEAT_SECONDS, provider=provider, model=model, mine_n=mine_n
        )
        git_seconds = max(1, deadline.slice(orchestrator.DEFAULT_TIMEOUT_GIT))
        with clock.lane("snapshot"), env.git_ceiling(git_seconds):
            copied = snapshot.write_snapshot(repo, copy)
        if copied["truncated"]:
            return no_turn("copy_incomplete")
        if not copied["files_written"]:
            # An empty directory would be answered with "nothing here", and that would be
            # scored as a finding about the repository. The turn is not taken, and the
            # working tree is never what it falls back to.
            return no_turn("nothing_to_read")
        with clock.lane("history brief"):
            brief.write_brief(repo, history)

        def collect(name):
            lane = census if name == "census" else mining
            options = {"timeout": census_seconds}
            if name == "mining":
                options = {"timeout": mine_seconds, "n": mine_n}
            outcome: dict = {}
            watch.begin(name)
            try:
                with clock.lane(name):
                    found = lane.collect(
                        copy, history, provider=provider, model=model, outcome=outcome, **options
                    )
            except ProviderNotIsolated:
                watch.end(name, None, {"kind": support.NOT_ISOLATED, "returncode": None})
                raise
            except ProviderUnavailable:
                watch.end(name, None, {"kind": "cli_missing", "returncode": None})
                raise
            watch.end(name, found[0], outcome)
            return found

        # Both workers finish before the temporary source/history directories are removed,
        # and a lane that ends early leaves the other to finish: it is already paid for.
        with watch, cf.ThreadPoolExecutor(max_workers=2) as pool:
            pending = {name: pool.submit(collect, name) for name in ("census", "mining")}
            return {name: future.result() for name, future in pending.items()}


def review_status(row: dict, material: dict, mined: dict) -> tuple[str, str | None]:
    """Only the census's clock running out is a timeout."""
    if material.get("timed_out"):
        return "partial", "lanes_timed_out"
    if not material.get("scored") or mined.get("total_candidates") is None:
        return "partial", "lanes_unavailable"
    return row["status"], row.get("skip_reason")


def _review_one(
    repo: Path,
    name: str,
    out_dir: Path,
    *,
    label: str,
    provider: str,
    model: str | None,
    budget_seconds: int,
    mine_n: int,
    census_timeout: int = DEFAULT_LANE_TIMEOUT,
    mine_timeout: int = DEFAULT_LANE_TIMEOUT,
    heartbeat_seconds: float | None = None,
) -> dict:
    """Review one repository and write its folder. Raises only what must stop the whole run.

    Anything else that goes wrong here is this repository's failure and is returned as one,
    for the reason step 1 gives: the others are still waiting, and so is the zip. A provider
    turn that fails is not a failure in that sense at all. It yields a block of nulls naming
    what happened, and the run around it is complete. How each lane ended is returned under
    `lanes`, for the batch to decide whether to go on and for the support log.
    """
    clock = _QuietClock(label="")
    deadline = orchestrator.Deadline(budget_seconds)
    watch = _Watch(
        label,
        HEARTBEAT_SECONDS if heartbeat_seconds is None else heartbeat_seconds,
        provider=provider,
        model=model or DEFAULT_MODELS[provider],
        mine_n=mine_n,
    )
    result = {"name": name, "repo": str(repo), "status": None, "error": None, "lanes": {}}
    try:
        # Step 1 first: it is free, and a repository it cannot measure is not worth a paid turn.
        row, measurement = orchestrator.measure(repo, clock=clock, deadline=deadline)
        lanes = _model_lanes(
            repo,
            clock,
            deadline,
            provider=provider,
            model=model,
            mine_n=mine_n,
            census_timeout=census_timeout,
            mine_timeout=mine_timeout,
            watch=watch,
        )
        block, _answer = lanes["census"]
        mined, _mining_answer = lanes["mining"]
        skipped = lanes.get("no_turn")
        mined = emit.mining_block(
            {
                **mined,
                "repo_id": row["fake_repo_name"],
                "measurer_version": __version__,
                "mined_at": datetime.now(UTC).isoformat(),
            }
        )
        material = emit.material_block(
            {
                **block,
                "task_type_counts": {name: mined[name] for name in mining.COUNT_KEYS},
                # where the time went, in the one prose field the block has for the run itself
                "census_platform_note": clock.note(),
            }
        )
        row["status"], row["skip_reason"] = review_status(row, material, mined)
        target = out_dir / name
        # The row goes out with its record id first and blank; once all four files are on
        # disk, the id is computed over their bytes and written over the blank (`record`).
        written = orchestrator.write_outputs(
            target,
            {"record_id": record.BLANK, **row},
            {**measurement, "material": material},
            out_root=out_dir,
        )
        mining_path = target / "codebase_repo_mining.json"
        fileguard.write_text(mining_path, json.dumps(mined, indent=2) + "\n", out_dir)
        written.append(mining_path)
        record.seal(target, out_dir)
    except (ProviderUnavailable, ProviderNotIsolated):
        raise
    except Exception as exc:  # noqa: BLE001 -- one repository, not the run
        _tell(label, f"FAILED: {type(exc).__name__}: {exc}")
        return {**result, "error": f"{type(exc).__name__}: {exc}", "lanes": watch.outcomes}
    if skipped:
        _tell(
            label,
            f"the review did not complete: {support.reason(skipped, provider, model)}, so no "
            "model session was started. Its counts are written as empty, meaning 'not "
            "measured', never as zero.",
        )
    return {
        **result,
        "status": row.get("status"),
        "skip_reason": row.get("skip_reason"),
        "mining": mined,
        "material": material,
        "written": written,
        "no_turn": skipped,
        "lanes": watch.outcomes,
    }


def check_limits(*, mine_n: int, budget_seconds: int, census_timeout: int, mine_timeout: int):
    """Raise `Refused` when a limit the operator gave cannot bound a run.

    The command line asks this before its checks, the last of which is billed, and
    `review_all` asks it again for any caller that did not.
    """
    if type(mine_n) is not int or mine_n < 1 or budget_seconds <= 0:
        raise Refused("the task ceiling and time budget must be positive")
    if census_timeout <= 0 or mine_timeout <= 0:
        raise Refused("each lane's timeout must be positive")


def _settle(record: support.Repo, result: dict) -> str | None:
    """Record how one repository ended, and return the stop kind when the batch must stop.

    A repository is done when both of its sessions completed. Otherwise it is incomplete,
    under the kind that explains it best: one that would stop the batch first, then the
    reason no session was started, then the census's, then task mining's. Its folder is
    packed, as it always was, whenever its files were written, except when a problem that
    meets every repository stopped it: that one is redone on resume.
    """
    lanes = result.get("lanes") or {}
    kinds = [lanes[name].get("kind") for name in ("census", "mining") if name in lanes]
    stop = next((kind for kind in kinds if kind in support.STOP_KINDS), None)
    if result["error"] is not None:
        record.state, record.kind, record.packed = support.INCOMPLETE, stop or support.ERROR, False
        return stop
    if result["status"] == "measured":
        record.state, record.kind, record.packed = support.DONE, None, True
        return None
    named = stop or result.get("no_turn") or next((k for k in kinds if k), None) or "unknown"
    record.state, record.kind, record.packed = support.INCOMPLETE, named, stop is None
    return stop


def _log_repository(log: support.Log, record: support.Repo, result: dict | None) -> None:
    """What the support log keeps of one repository: how long each session took, how it
    ended and its exit status, and the masked excerpt the block itself carries. Never an
    answer, a finding or a path."""
    for name, outcome in ((result or {}).get("lanes") or {}).items():
        said = outcome.get("kind") or "done"
        line = f"{record.folder}: {LANE_WORDS[name]} ended after "
        line += f"{support.duration(outcome.get('seconds', 0))}: {said}"
        if outcome.get("returncode") is not None:
            line += f", exit code {outcome['returncode']}"
        if outcome.get("kind"):
            if name == "census":
                excerpt = ((result or {}).get("material") or {}).get("census_error_detail")
            else:
                excerpt = ((result or {}).get("mining") or {}).get("mine_unavailable_reason")
            if excerpt:
                line += f"; masked excerpt: {excerpt}"
        log.write(line)
    if result and result.get("no_turn"):
        log.write(f"{record.folder}: no session was started: {result['no_turn']}")
    if result and result.get("error"):
        log.write(f"{record.folder}: this tool failed: {result['error'].split(':')[0]}")
    kind = f" ({record.kind})" if record.kind else ""
    log.write(f"{record.folder}: finished: {record.state}{kind}")


def review_all(
    repos: list[Path],
    out_dir: Path,
    *,
    provider: str,
    model: str | None,
    budget_seconds: int,
    mine_n: int = mining.DEFAULT_N,
    census_timeout: int = DEFAULT_LANE_TIMEOUT,
    mine_timeout: int = DEFAULT_LANE_TIMEOUT,
    heartbeat_seconds: float | None = None,
    checklist: list | None = None,
    resume: support.Progress | None = None,
) -> dict:
    """Review every repository in turn, then write the zip.

    Repositories are processed one after another; each one's model lanes run concurrently
    under its own deadline. A problem that would meet every repository the same way (the
    account's usage limit, a refused sign-in, a model the account cannot use, a provider
    command that is gone or no longer isolated) stops the batch once the repository it met
    has finished, and so does Ctrl-C. What was done is packed either way, and the progress
    file in `out_dir` records where the batch stands, so that `resume` (that file, loaded)
    carries on with only what is left.

    Returns `{"results", "zip", "asked", "stopped", "stop_kind", "stop_detail", "repos",
    "out_dir", "provider", "model"}`. `stopped` is the exception or kind that ended the run
    early, or None; `repos` is every repository of the batch with its state.
    """
    # All of these raise, and all before anything is measured or written: a run that cannot
    # be trusted should leave nothing behind that looks like a result.
    check_limits(
        mine_n=mine_n,
        budget_seconds=budget_seconds,
        census_timeout=census_timeout,
        mine_timeout=mine_timeout,
    )
    if model is None and DEFAULT_MODELS[provider] is None:
        raise Refused(f"--provider {provider} requires --model with an explicit model id")
    executable = resolve(provider)
    isolation_flags(provider, executable)
    # Never left to the command to choose: it would pick its newest, and the block would not
    # be able to say which model that was.
    model = model or DEFAULT_MODELS[provider]

    if resume is None:
        out_dir = Path(out_dir).expanduser().resolve()
        chosen, names = _selected(repos, out_dir)
        records = [support.Repo(str(path), name) for path, name in zip(chosen, names, strict=True)]
    else:
        out_dir = resume.out_dir
        records = resume.repos
        present = [r for r in records if r.state != support.DONE and Path(r.path).is_dir()]
        for record in present:
            if not scan.is_git_repo(Path(record.path)):
                raise Refused(f"not a git repository: {record.path}")
        _safe_to_write(out_dir, [Path(r.path) for r in present], [r.folder for r in present])
    # An earlier version kept raw answers beside the results. They are removed before this run
    # writes anything, so a reused output directory holds only what this version writes.
    _remove_old_local_files(out_dir)
    try:
        log = support.Log(out_dir)
    except ValueError as refused:
        raise Refused(str(refused)) from None
    out_dir.mkdir(parents=True, exist_ok=True)
    options = {
        "provider": provider,
        "model": model,
        "budget_seconds": budget_seconds,
        "mine_n": mine_n,
        "census_timeout": census_timeout,
        "mine_timeout": mine_timeout,
    }
    progress = support.Progress(out_dir, options, records)
    log.begin(
        resumed=resume is not None,
        provider=provider,
        executable=executable,
        options=options,
        out_dir=out_dir,
        repos=records,
        checklist=checklist,
    )
    progress.save()

    results: list[dict] = []
    stopped = stop_kind = stop_detail = None
    total = len(records)
    for position, record in enumerate(records, 1):
        if record.state == support.DONE:
            continue
        label = f"[{position}/{total}] {record.folder}"
        if not Path(record.path).is_dir():
            record.state, record.kind, record.packed = support.NOT_STARTED, support.GONE, False
            _tell(
                label,
                f"skipped: its folder {record.path} is no longer there. Put it back and "
                "resume again to review it.",
            )
            log.write(f"{record.folder}: skipped: its folder is no longer there")
            progress.save()
            continue
        _tell(label, "starting")
        log.write(f"{record.folder}: started")
        result = None
        try:
            result = _review_one(
                Path(record.path),
                record.folder,
                out_dir,
                label=label,
                provider=provider,
                model=model,
                budget_seconds=budget_seconds,
                mine_n=mine_n,
                census_timeout=census_timeout,
                mine_timeout=mine_timeout,
                heartbeat_seconds=heartbeat_seconds,
            )
        except (ProviderUnavailable, ProviderNotIsolated) as stop:
            # The command was there when the run began and is not now, or no longer offers
            # what it must. Nothing more can be done with it on this machine.
            isolated = isinstance(stop, ProviderNotIsolated)
            stopped, stop_kind = stop, support.NOT_ISOLATED if isolated else "cli_missing"
            stop_detail = str(stop)
            record.state, record.kind, record.packed = support.INCOMPLETE, stop_kind, False
        except KeyboardInterrupt:
            stopped = stop_kind = support.INTERRUPTED
            record.state, record.kind, record.packed = support.INCOMPLETE, stop_kind, False
            _tell(label, "stopped with Ctrl-C")
        else:
            results.append(result)
            stop_kind = _settle(record, result)
            stopped = stop_kind
        _log_repository(log, record, result)
        progress.save()
        if stop_kind is not None:
            break

    # Each folder goes into the zip under the name it has on disk, at the top of the archive.
    entries = [
        (r.folder, r.folder)
        for r in records
        if r.packed and all((out_dir / r.folder / name).is_file() for name in OUTPUT_FILES)
    ]
    try:
        packed = (
            scan.write_zip(
                out_dir, entries, filenames=OUTPUT_FILES, top_folder=False, zip_name=ZIP_NAME
            )
            if entries
            else None
        )
    except fileguard.UnsafeOutput as refused:
        raise Refused(str(refused)) from None
    progress.stopped = stop_kind
    progress.save()
    log.write(f"stopped: {stop_kind}" if stop_kind else "finished")
    for record in records:
        log.write(
            f"summary: {record.folder}: {record.state}"
            + (f" ({record.kind})" if record.kind and record.state != support.DONE else "")
        )
    if packed is not None:
        log.write(f"zip: {packed} holds {len(entries)} of {total} repositories")
    return {
        "results": results,
        "zip": packed,
        "asked": total,
        "stopped": stopped,
        "stop_kind": stop_kind,
        "stop_detail": stop_detail,
        "repos": records,
        "out_dir": out_dir,
        "provider": provider,
        "model": model,
        "log": log.path,
    }


def review(
    repo: Path,
    out_dir: Path,
    *,
    provider: str,
    model: str | None,
    budget_seconds: int,
    mine_n: int = mining.DEFAULT_N,
    census_timeout: int = DEFAULT_LANE_TIMEOUT,
    mine_timeout: int = DEFAULT_LANE_TIMEOUT,
) -> dict:
    """A run of one repository, and that repository's result."""
    done = review_all(
        [repo],
        out_dir,
        provider=provider,
        model=model,
        budget_seconds=budget_seconds,
        mine_n=mine_n,
        census_timeout=census_timeout,
        mine_timeout=mine_timeout,
    )
    if not done["results"] and isinstance(done["stopped"], BaseException):
        raise done["stopped"]
    return done["results"][0]
