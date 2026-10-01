# Changelog

All notable changes to hazina-review are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.4] - 2026-10-01

### Fixed

- On Windows every repository ended "this tool failed: ValueError" and its result went out
  without a record id: the output files were written with Windows line endings, which the
  record id step does not accept. They are now written with "\n" line endings on every
  platform, byte for byte as on macOS and Linux.

## [0.1.3] - 2026-09-30

### Changed

- README: "If it stops" is now part of Run, before Send; the request to read the short
  sentences before sending, and the cost estimate, are gone. The tool itself is unchanged.

## [0.1.2] - 2026-09-29

### Added

- `--all DIR` reviews every git repository directly inside one folder (one level, sorted by
  name), alone or together with repositories named on the command line; a repository named
  twice is reviewed once. Folders that are not git repositories are skipped with one line
  saying how many, and hidden folders are left out. If none is found, it says so and stops
  before any check. `--resume` carries on with the list the run started with.
- With more than five repositories, the check shows one line for all that pass and a line
  for each problem (ten, then a count pointing to the support log, once a run has started),
  and a batch says before it starts roughly how long each repository takes.

## [0.1.1] - 2026-09-29

### Fixed

- On macOS, the AI tool's own sign-in was not seen by the check or the review sessions, which
  stopped runs with 'not signed in'. The AI tool keeps that sign-in in the macOS Keychain and
  finds it by the user's name, and it was started without it. Every start of the AI tool (the
  sign-in check, the sandbox check, the model check and every review session) is now handed
  the user's name (`USER`, `LOGNAME`, and `USERNAME` on Windows) beside the home directory,
  and still no other variable of yours.

### Changed

- When the AI tool says it is not signed in, the check asks it once more with your own
  environment, as if you had typed the command yourself. If it is signed in that way, the
  check says the tool is signed in but hazina-review cannot see the sign-in on this computer,
  and asks you to email the check results to partners@hazinalabs.com, instead of telling you
  to sign in again. Only the yes or no is kept from either answer.
- The support log records, by name only, whether each of `HOME`, `USER`, `LOGNAME`, `TMPDIR`,
  `USERPROFILE`, `XDG_CONFIG_HOME`, `CLAUDE_CONFIG_DIR` and `CODEX_HOME` and each of the AI
  tool's sign-in variables is set and whether the AI tool is handed it, and on a Mac the
  macOS version. No value is ever written.

## [0.1.0] - 2026-09-29

### Added

- The build check is part of the review, on by default. After both model sessions, each
  repository's own install, build and test commands are run in its checkout by the same code
  and with the same flags and defaults as the build check always had (`--build none|discover|full`,
  default `full`; `--no-build`; `--build-budget-seconds`, `--full-attempt-seconds`,
  `--timeout-build`, `--max-build-projects`). It fills the `build` block of
  `measurement.json` and `build_ok` and `testable_at_head` in `codebase_repos.json` and `.csv`,
  so one run and one zip now carry the build results as well as the review. Its share of the budget is
  held back from the model sessions. It adds time and is not billed by the AI provider. The
  checks before a run warn that it changes the checkout, and name each repository with
  uncommitted changes it may overwrite. A progress line says how it ended
  (`build check done in 3m 10s (built, tests ran)`), the heartbeat names it while it runs,
  `--resume` keeps its level and limits, and the support log records them and each
  repository's outcome and duration. Invalid build limits are refused before any check.

- Each repository's `codebase_repos.json` now starts with a `record_id`, which lets us check
  that the four files were not changed after they were generated. The CSV row and
  `measurement.json` are unchanged.
- A batch says what it is doing: `[2/5] logrus: starting` before each repository, a line every
  minute while its model sessions run (`still working (census 4m, task mining 6m)`), a line
  when each session ends with how long it took (and the number of tasks) or, when it did not
  complete, why in plain words, and a summary at the end listing each repository as done,
  incomplete with the reason, or not started.
- A problem that would meet every repository stops the batch: the account's usage limit, a
  refused sign-in, a model the account cannot use, or a provider command that is missing or
  no longer isolated. The repository in progress finishes its sessions, no further one is
  started, what was done is zipped, and the message gives the exact next step: the sign-in
  command, what to do about the model, or when to resume. Ctrl-C stops the same way. A
  problem with one repository (a timeout, an unreadable answer, a crash) does not stop it.
- `--resume <output directory>` carries on with the repositories that are not done, with the
  options the run started with, after running the checks again. Mixing in `--model`,
  `--provider`, a limit or repositories is refused. A repository whose folder is gone is
  skipped and said. The zip is rewritten with every done repository.
- `hazina-review-progress.json` and `hazina-review-log.txt` in the output directory: where the
  run stands, and a support file to email to partners@hazinalabs.com. Every message about a
  problem ends by naming it. The log records versions, the checklist, each session's duration,
  outcome and exit code, and the masked excerpt; it never records answers, findings, file
  contents, sign-in details, keys, tokens or email addresses, and writes the home directory as
  `~`. Neither file is zipped.

- Checks before every run. The provider command is installed, offers the flags this tool
  needs and is signed in; its own sandbox can read a file where it has one; git is installed
  and each repository is a git repository; the output directory can be written. Last, one
  small billed session confirms the model answers and can read a file. Each prints one line,
  and a failure says what to do next and stops the run with exit code 2 before anything is
  written. `--check` runs only the checks; `--skip-model-check` leaves out the billed one.
  Nothing the provider command prints about the account is shown or stored.

### Fixed

- Two repositories can no longer be given one output folder. A repeated name is numbered
  with the lowest number that is not another repository's own name or another folder of the
  run, so `api`, `api` and `api-2` are written to `api`, `api-3` and `api-2` instead of the
  third overwriting the second and the zip holding one path twice.
- A `--mine-n`, `--budget-seconds`, `--census-timeout` or `--mine-timeout` that is not
  positive is refused before the checks, with exit code 2, so an invalid command never starts
  the billed model check.
- An earlier run's local files no longer stay in a reused output directory. Before writing,
  the run removes `INDEX.local.txt` directly in the output directory and `DETAIL.local.md`
  directly in any of its folders, when each is a regular file, and prints one line for each.
  Nothing else is removed, a linked folder is never entered, and a symbolic link at either
  name stops the run with nothing removed or written.
- The clean-up of an earlier run's local files no longer removes a file it did not write. A
  `DETAIL.local.md` is removed only from a folder that also holds an earlier run's
  `measurement.json` and `codebase_repos.json`, and `INDEX.local.txt` only when it starts with
  the header the earlier version wrote and every folder it lists that is still there is such a folder, so
  an index this tool did not write is kept. Any other file by those names is left, with one
  line saying so.
- The support log and the progress file are written only to a plain file of the run's own.
  Before the checks (so before the billed model check), a named pipe, a device, a folder, a
  symbolic link or a hard link at either name stops the run with exit code 2 and nothing
  written or billed; before, a pipe hung the run and a hard link let the log be added to a
  file outside the output directory. Every write opens the file never through a link and
  never waiting on a pipe, and checks what it opened before writing. The progress file's
  temporary name is made new each time and never written through.
- `--resume` on a run whose every repository is done says so, names the results and the zip,
  and exits `0` without running any check; before, it ran the checks, the billed model check
  among them, with no repository to review.

### Removed

- `DETAIL.local.md` and `INDEX.local.txt` are no longer written, and the closing lines that
  pointed to them are gone.

### Changed

- Every provider now has a pinned default model (the new one is `gpt-6-sol`), so `--model`
  is always optional.

- `measurer_version` in `measurement.json` and in `codebase_repo_mining.json` is now
  `hazina-review@<version>` (for example `hazina-review@0.1.0`), naming this tool and its
  version. Before, `measurement.json` named the bundled scanner and its version, and
  `codebase_repo_mining.json` held the bare version number.
- The end of a run is short however many repositories it had: one line for where the
  results are, one for the zip to send, and a summary that counts repositories done,
  incomplete and not started, naming only the incomplete ones (at most ten; the rest are in
  the support file). The per-file "wrote" lines and per-repository status lines are gone.

- Exit codes: `0` every repository is done; `1` the run went to the end and some repository is
  incomplete (before, a run whose sessions failed exited `0`); `2` the run stopped early or
  could not start.
- A repository stopped by a problem that meets every repository is no longer packed into the
  zip; a resumed run redoes it. The per-lane `[lane]` and `[alive]` lines are no longer printed;
  the progress lines above replace them.

- The output directory is now `hazina-review-out` by default, and the zip is
  `hazina-review-out.zip`, so the folder and the zip no longer share a name with another
  tool's.

- New output layout. Each repository's folder is named after its directory (a repeated name
  gets `-2`, `-3`, ...) and holds exactly `measurement.json`, `codebase_repos.json`,
  `codebase_repos.csv` and `codebase_repo_mining.json`. `hazina-out.zip`, still written beside
  the output directory, holds `<folder name>/<the four files>` for every repository of the
  run; the anonymous `repo-<hex>` folder names are gone from the zip and from the closing
  lines.
- Three values in `measurement.json` are written differently:
  `tree.linters_and_formatters.<tool>` masks the config file
  name (`"pyproject.toml"` becomes `"[file]"`), `ext_signals.history.ref_choice_reason` is
  `"the model was unavailable; used the deepest reachable history"`, and
  `ext_signals.history.development_substance_error` is `"the model was unavailable"`.
- The census and task mining requests were replaced with new fixed wording. The mining
  request asks for commit hashes in `source_commit_shas`.
- The prepared history has a new layout: `HISTORY-OVERVIEW.md`, and one file per written
  commit under `commit-diffs/`, named by its place in the sample and its short hash. The table
  and each written commit now carry the commit hash, a written commit whose credential-shaped
  files were left out says so at its end, and the size limits count characters.
- In the history, a credential-shaped file is now recognised by its own name only. A file
  inside a folder with such a name, for example `.env/prod`, is still left out of the copy but
  its changes now appear in the history.
- The census request no longer asks for more than the bar it sets: extra conditions (a plain
  yes, when in doubt leave it out, tests counted only when seen, and similar) are gone, and
  it now says the run reports how many files were withheld from the history.
- The task mining request asks for a full headcount of the tasks the repository could supply,
  says a count is only what is listed, asks for real paths and patch numbers, prefers
  long-horizon work across many files, and uses a fixed set of answer keys. Extra
  exclusions that lowered the count were removed. The ceiling is described as a limit, not a
  target, after a live run listed exactly 150 tasks.
- A census answer that can be read is scored as it stands. A category the model left out
  counts as zero; before, an incomplete answer was written as `null`.
- Sentences, themes and the summary are masked and judged by a revised set of text rules,
  and the whole block is masked again and audited before it is written.
- Census failures use eight fixed kinds: `rate_limited`, `auth`, `model_unavailable`,
  `timeout`, `no_json`, `crashed`, `cli_missing` and `unknown`. Other labels are gone. An
  unfinished block now says why in `error`, `logic_depth_unavailable_reason`,
  `census_retryable` and `census_error_detail`, a masked excerpt of what the provider command
  printed, emptied if it still names anything. Every block carries a timing note.
- Task mining's `mine_unavailable_reason` is now a sentence, not a label: for an empty list,
  for a reached ceiling, and for a failed turn, where it can quote the start of what the
  command printed. A single object in the answer counts as one task.
- A run is marked `lanes_timed_out` only when the census ran out of time. A mining timeout
  is `lanes_unavailable`.
- Replies are read more leniently: an answer found anywhere in the output is used, with or
  without the provider's JSON envelope.
- The second provider's census request no longer sends an answer schema.
- The history is always written out in full: every git call has 900 seconds, and the history
  step no longer stops early to save time. A commit git says nothing about is skipped, and the
  sessions are started even when the history lists no commit.

### Added

- A throttled or dropped census session is tried again, up to three attempts, with short
  jittered waits. Task mining is attempted once.
- `--census-timeout` and `--mine-timeout`, each 14400 seconds by default. Each session gets
  the smaller of its own limit and what is left of `--budget-seconds`, and at least one
  second. The 60-second minimum that used to skip a session is gone.

### Security

- A checkout's own git configuration can no longer start programs while it is measured. The
  filesystem monitor and signature display are switched off on every git call, external diff
  and text conversion are switched off on `log`, `show` and `diff`, and locally deleted
  files are found without reading file contents, so no clean filter runs.
- The deterministic readers no longer follow a symbolic link. A tracked `LICENSE` linked to a
  file outside the repository put that file's copyright holder into the shareable
  `measurement.json`; a repository file is now read only when it is a regular file, not a
  link, whose resolved path is inside the repository.
- A symbolic link to a FIFO, or a FIFO in the working tree, can no longer stall a review past
  its budget. Every deterministic reader (the tree walk, structure, the content digest,
  identity, and the build check's manifest reads) now skips links and anything that is not a
  regular file, walks directories without following links, and opens files with
  `O_NONBLOCK` and `O_NOFOLLOW` where the platform has them.
- A reused output folder can no longer redirect a write. A symbolic link at a repository's
  output folder, at any file in it or at the zip is refused before the
  run starts and again at each write, a folder that resolves inside the repository is
  refused, and the zip refuses a linked member. The message names the path and says to
  remove the link or choose an empty directory for `--out`.

### Changed

- Mining now asks for up to 150 tasks by default, instead of 40.
  The ceiling is part of the request, so 40 made the model list fewer tasks.
- If the model returns more tasks than the ceiling, all of them now count. Before, only the
  first tasks up to the ceiling counted.
- A session the provider refused is now named for why:
  `rate_limited` when the account is throttled or out of its usage allowance, `auth` when
  the sign-in was refused, and `model_unavailable` when the requested model was. Each was
  reported as `exit` before, which reads like a crash. Only the name is recorded; nothing
  the provider printed is kept.

### Added

- A second CLI provider can run both model lanes when given an explicit model id. It uses
  read-only mode, disables repository instructions and approval escalation, and cleans up
  its temporary answer file. The census request supplies a JSON output schema so incomplete
  response shapes do not become measured zeros. Its read scope is documented in the security
  policy.

- **The material census.** One read-only model session per repository, through the provider
  command under your own account, counting substantial work in nine categories and
  describing each finding in one sentence that names nothing.
- The model reads a temporary copy of your committed files and a read-only copy of your
  history, never your working folder. Password-shaped files are left out of both, and both
  are deleted when the run ends.
- The run stops before measuring anything if the selected provider command is missing or
  lacks a required flag for its mode. The two modes have different file-read scopes, as
  documented in the security policy.
- The model reads the history the way a reviewer would: a list of the commits on any branch
  that changed two or more source files and between 20 and 10,000 lines, each with its date,
  subject, size and first few paths, and up to 160 of them written out in full, spread evenly
  from newest to oldest. Nobody's name or address is written.
- The instruction given to the model sets a high bar for what counts as substantial, tells it
  to expect a few such instances in a typical repository rather than dozens, and spells out
  every rule a sentence is checked against.
- With no `--model`, the default provider uses one pinned id. The second provider requires
  an explicit id, and the block records whichever id was used.
- `measurement.json` holds the repository's measurement, with one `material` block added. Every value in the
  block is checked against a fixed list of allowed fields, and a sentence that names a path,
  a file, a symbol or a capitalised name goes out empty.
- `DETAIL.local.md`: what is being sent, then everything the model found, for you to compare.
  It is never put in the zip.
- A review that does not complete writes `null`, never zero, and says why. No model session
  is started on a temporary copy that came out incomplete, on a history git did not hand
  over, or with too little of the time budget left for one.

- Task mining in four categories, a default ceiling of 40, and `--mine-n` to change it.
- Concurrent model lanes sharing one prepared snapshot, history brief and time budget.
- `codebase_repo_mining.json`, mirrored task counts in the material block, and full mining
  evidence in the local detail file. A rejected task description does not remove its count.
- Independently versioned review distribution, bundled prompts and review-specific release docs.

### Fixed

- Incomplete reviews report `partial` in the summary, JSON, CSV and local index, with a
  timeout or unavailable reason. Successful results from the other lane are preserved.
- Review archives include only the four declared output files, excluding unrelated files
  left in a reused output folder.
- Empty objects, missing census categories and malformed task arrays are unmeasured rather
  than being interpreted as successful assessments with zero findings.

### Validation remaining before publication

- Paid end-to-end validation of both model sessions across the repository corpus.
- Release-platform checks on systems other than the development host.
