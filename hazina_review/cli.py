"""The command line: what the operator may ask for, and what a run says when it is over."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hazina_review import __version__, emit, preflight, run, support
from hazina_review.lanes.mining import DEFAULT_N
from hazina_review.providers.registry import (
    DEFAULT_MODELS,
    PROVIDERS,
    ProviderNotIsolated,
    ProviderUnavailable,
)
from hazina_scan import cli as scan
from hazina_scan import orchestrator
from hazina_scan.build.probe import BUILD_LEVELS
from hazina_scan.schema import EmissionRefused


def _model_id(text: str) -> str | None:
    """The id as the emission boundary will accept it, judged by the boundary's own rule.

    Asked here because the alternative is finding out after the turn has been paid for.
    """
    try:
        return emit.MODEL_ID.apply(text, "--model")
    except EmissionRefused:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a model id. Give the id as the provider spells it, never a path."
        ) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hazina-review",
        description="Measure what a repository's code actually contains.",
    )
    parser.add_argument("repo", nargs="*", help="path to a git repository")
    parser.add_argument(
        "--all",
        dest="all_dir",
        metavar="DIR",
        default=None,
        help="review every git repository directly inside DIR (one level, sorted by name); "
        "combine freely with named repositories",
    )
    parser.add_argument("--out", default=run.DEFAULT_OUT, help="output directory")
    parser.add_argument(
        "--resume",
        metavar="OUT_DIR",
        default=None,
        help="carry on with a run that stopped, with the options it was started with",
    )
    # The recorded options default to None so that one given with --resume can be told apart
    # from one left out; `_defaults` fills them in for a new run.
    parser.add_argument("--provider", choices=PROVIDERS, default=None)
    parser.add_argument(
        "--model",
        type=_model_id,
        default=None,
        help="model id to pass to the provider (required for providers without a pinned default)",
    )
    parser.add_argument("--budget-seconds", type=int, default=None)
    parser.add_argument("--mine-n", type=int, default=None, help="task enumeration ceiling")
    parser.add_argument(
        "--census-timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="the census lane's own ceiling; the run budget still bounds it",
    )
    parser.add_argument(
        "--mine-timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="the mining lane's own ceiling; the run budget still bounds it",
    )
    parser.add_argument(
        "--build",
        dest="build_level",
        choices=BUILD_LEVELS,
        default=None,
        help=f"How much of the build check to run (default {DEFAULT_BUILD_LEVEL}). It RUNS "
        "EACH REPOSITORY'S OWN install, build and test commands and changes the checkout, so "
        "run it on a throwaway copy: discover resolves dependencies, builds and lists the "
        "tests; full also runs them and reads coverage back; none runs nothing.",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="Same as --build none: review without running anything of the repository's own.",
    )
    parser.add_argument(
        "--build-budget-seconds",
        type=int,
        default=None,
        metavar="SECONDS",
        help=f"The build check's reserved share of --budget-seconds (default "
        f"{orchestrator.DEFAULT_BUILD_BUDGET_SECONDS}, never more than half of it).",
    )
    parser.add_argument(
        "--full-attempt-seconds",
        type=int,
        default=None,
        metavar="SECONDS",
        help=f"How long a --build full attempt may run before the check is finished at the "
        f"cheaper level (default {orchestrator.DEFAULT_FULL_ATTEMPT_SECONDS}).",
    )
    parser.add_argument(
        "--timeout-build",
        type=int,
        default=None,
        metavar="SECONDS",
        help=f"Ceiling for ONE command inside the build check (default "
        f"{orchestrator.DEFAULT_TIMEOUT_BUILD}), always subordinate to the budgets.",
    )
    parser.add_argument(
        "--max-build-projects",
        type=int,
        default=None,
        metavar="N",
        help=f"How many project roots inside one repository the build check may reach "
        f"(default {orchestrator.DEFAULT_MAX_BUILD_PROJECTS}).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="only check that everything a run needs is in place, then exit",
    )
    parser.add_argument(
        "--skip-model-check",
        action="store_true",
        help="leave out the one small paid session that confirms the model can read a file",
    )
    parser.add_argument("--version", action="version", version=f"hazina-review {__version__}")
    return parser


def _say(done: dict) -> None:
    """What the operator reads once the run is over, in the words step 1 uses for the same
    facts."""
    # Two lines however long the batch: where the results are, and the one file to send. Each
    # repository's outcome is in the summary and the log, in plain words.
    for result in done["results"]:
        if result["error"]:
            print(f"{result['name']}: FAILED -- {result['error']}")
    if any(result.get("written") for result in done["results"]):
        print(f"Results: {done['out_dir']}")
    if done["zip"] is not None:
        print(f"Zip to send: {done['zip']}")


#: The build check's level when none is named: the bundled scanner's own default.
DEFAULT_BUILD_LEVEL = "full"

#: What a new run uses for each recorded option left out.
DEFAULTS = {
    "provider": PROVIDERS[0],
    "budget_seconds": 9000,
    "mine_n": DEFAULT_N,
    "census_timeout": run.DEFAULT_LANE_TIMEOUT,
    "mine_timeout": run.DEFAULT_LANE_TIMEOUT,
    **run.BUILD_DEFAULTS,
    "build_level": DEFAULT_BUILD_LEVEL,
}
#: The flag for each recorded option, as the operator typed it.
FLAGS = {
    **{name: "--" + name.replace("_", "-") for name in support.RECORDED},
    "build_level": "--build/--no-build",
}


def _refuse(message: str) -> int:
    print(f"hazina-review: {message}", file=sys.stderr)
    return 2


def _resumed(args) -> tuple[support.Progress | None, str | None]:
    """The run `--resume` names, or why it cannot be carried on."""
    given = [FLAGS[name] for name in support.RECORDED if getattr(args, name) is not None]
    if args.out != run.DEFAULT_OUT:
        given.append("--out")
    if args.repo or args.check:
        given.append("repositories" if args.repo else "--check")
    if args.all_dir is not None:
        given.append("--all")
    if given:
        return None, (
            f"--resume carries on with the repositories and options the run was started "
            f"with, so {', '.join(given)} cannot be given with it: results in one batch must "
            f"all come from one model. To change them, start a new run with a new --out."
        )
    try:
        return support.Progress.load(Path(args.resume)), None
    except support.NoProgress as refused:
        return None, str(refused)


def _already_done(out_dir: Path) -> int:
    """What `--resume` says of a run whose every repository is done, in `_say`'s words."""
    print("Every repository in this run is already done.")
    print(f"Results: {out_dir}")
    packed = Path(out_dir).parent / run.ZIP_NAME
    if packed.is_file():
        print(f"Zip to send: {packed}")
    return 0


#: Exit codes: 0 every repository is done; 1 the run went to the end and some repository is
#: not; 2 a usage error, a failed check before the run, or a run that stopped early.
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # Intermixed, so repositories may be named before and after an option such as --all.
    args = parser.parse_intermixed_args(argv)
    # `--no-build` is the plainer spelling of `--build none` and always wins over it, as in
    # the bundled scanner's own command line.
    if args.no_build:
        args.build_level = "none"
    progress = None
    skipped: list[Path] = []
    if args.resume is not None:
        progress, refused = _resumed(args)
        if refused:
            return _refuse(refused)
        for name, value in progress.options.items():
            setattr(args, name, value)
        remaining = [r for r in progress.repos if r.state != support.DONE]
        if not remaining:
            # Nothing to carry on with, so nothing to check: the checks would only bill a
            # model session for a run that has no repository left.
            return _already_done(progress.out_dir)
        repos = [Path(r.path) for r in remaining if Path(r.path).is_dir()]
        out = progress.out_dir
    else:
        if not args.check and not args.repo and args.all_dir is None:
            parser.error("the following arguments are required: repo (or --all DIR)")
        for name, value in DEFAULTS.items():
            if getattr(args, name) is None:
                setattr(args, name, value)
        found, skipped, refused = _found(args.all_dir)
        if refused:
            return _refuse(refused)
        # `--all code` with `code/api` names one checkout twice; it is reviewed once.
        repos = list(dict.fromkeys(Path(r).expanduser().resolve() for r in [*found, *args.repo]))
        out = Path(args.out)
    # An invalid limit is a command that cannot run, so it is refused before the checks, the
    # last of which is billed.
    try:
        run.check_limits(
            mine_n=args.mine_n,
            budget_seconds=args.budget_seconds,
            census_timeout=args.census_timeout,
            mine_timeout=args.mine_timeout,
            **_build(args),
        )
    except run.Refused as refused:
        return _refuse(str(refused))
    try:
        return _run(args, repos, out, progress, skipped)
    except KeyboardInterrupt:
        # Outside a repository's review, which keeps its own place: during the checks, or
        # while the zip was being written.
        return _refuse("stopped with Ctrl-C.")


#: More repositories than this, and a batch is told before it starts roughly how long it takes.
LONG_BATCH = preflight.REPO_LINES


def _found(all_dir: str | None) -> tuple[list[Path], list[Path], str | None]:
    """The repositories `--all` names, the other folders it passed over, or why it cannot run.
    Found with the bundled scanner's own rule."""
    if all_dir is None:
        return [], [], None
    point = "Point --all at the folder that holds your repositories."
    root = Path(all_dir).expanduser().resolve()
    if not root.is_dir():
        return [], [], f"{all_dir} is not a folder. {point}"
    found, skipped = scan.repos_in(root)
    if not found:
        return [], [], f"No git repositories found directly inside {all_dir}. {point}"
    return found, skipped, None


def _skipped_line(all_dir: str, skipped: list[Path]) -> str:
    if len(skipped) == 1:
        return f"Skipped 1 folder in {all_dir} that is not a git repository."
    return f"Skipped {len(skipped)} folders in {all_dir} that are not git repositories."


def _build(args) -> dict:
    """The build check's level and sizes, as the run takes them."""
    return {name: getattr(args, name) for name in run.BUILD_DEFAULTS}


def _run(args, repos: list[Path], out: Path, progress, skipped: list[Path]) -> int:
    # The run's own two files first: something else at either name is refused before the
    # checks, the last of which is billed.
    try:
        support.check_run_files(out)
    except support.UnsafeRunFile as refused:
        return _refuse(str(refused))
    # Before any repository is touched. The checklist goes where the run's own progress goes.
    stream = sys.stdout if args.check else sys.stderr
    if skipped:
        print(_skipped_line(args.all_dir, skipped), file=stream)
    checked = preflight.run_checks(
        args.provider,
        args.model,
        repos,
        out,
        model_check=not args.skip_model_check,
        build_level=args.build_level,
    )
    # Every line goes into the support log once the run starts; a check alone writes none.
    logged = not args.check and preflight.passed(checked)
    preflight.say(
        checked, stream, repos=len(repos), log=Path(out) / support.LOG_NAME if logged else None
    )
    if args.check:
        return 0 if preflight.passed(checked) else 2
    if not preflight.passed(checked):
        return _refuse(
            "a check above failed, so the run did not start and nothing was written. If you "
            f"need help, copy the lines above into an email to {support.SUPPORT_ADDRESS}."
        )
    if len(repos) > LONG_BATCH:
        print(
            f"{len(repos)} repositories: roughly 15 to 60 minutes each; you can stop with Ctrl-C "
            "and carry on later with --resume.",
            file=sys.stderr,
        )
    try:
        done = run.review_all(
            repos,
            out,
            provider=args.provider,
            model=args.model or DEFAULT_MODELS[args.provider],
            budget_seconds=args.budget_seconds,
            mine_n=args.mine_n,
            census_timeout=args.census_timeout,
            mine_timeout=args.mine_timeout,
            checklist=checked,
            resume=progress,
            skipped_folders=[folder.name for folder in skipped],
            **_build(args),
        )
    except (ProviderUnavailable, ProviderNotIsolated, run.Refused, support.UnsafeRunFile) as stop:
        return _refuse(str(stop))
    _say(done)
    lines, code = support.report(done, sys.stderr)
    for line in lines:
        print(line, file=sys.stderr)
    sys.stderr.flush()
    return code
