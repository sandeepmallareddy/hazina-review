# hazina-review

Step 2 of the evaluation with [Hazina Labs](https://hazinalabs.com). It asks an AI model, on
your machine and under your own account, what substantial work your repository holds, and
writes one zip of counts and short sentences for you to send us.

It runs on your machine and sends nothing to Hazina Labs by itself. Your code is read only by
the AI provider you are already signed in to.

Questions: **[partners@hazinalabs.com](mailto:partners@hazinalabs.com)**

## Quick start

**1. You need:** Python 3.11 or newer, `git`, and
[Claude Code](https://claude.com/claude-code) installed and signed in (`claude auth login`).
[Codex CLI](https://developers.openai.com/codex/cli) also works; see
[Requirements and providers](#requirements-and-providers).

**2. Install** (once):

```bash
pipx install git+https://github.com/sandeepmallareddy/hazina-review.git
```

This repository is private: ask us for access, or install the wheel file we send you with
`pipx install ./hazina_review-0.1.0-py3-none-any.whl`.

**3. Check your machine** (free, takes a few seconds):

```bash
hazina-review --check --skip-model-check
```

No line should start with `✗`; if one does, it says what to fix. (`–` means a check was skipped.)

**4. Run it** on a copy of each repository you are sharing:

```bash
hazina-review ~/code/my-repo
hazina-review ~/code/api ~/code/web ~/code/mobile      # several, one after another
```

Each repository takes roughly 10 to 30 minutes and is billed to your AI provider account
(a small repository cost about $5 to $8 at API prices in our tests; larger ones cost more).
You can leave it running: it prints a line every minute to show it is still working.

**5. Send** the zip it names at the end, `hazina-review-out.zip`, to
**[partners@hazinalabs.com](mailto:partners@hazinalabs.com)**. Glance at the sentences first
(see [Before you send the zip](#before-you-send-the-zip)). If we also asked you for step 1's
zip from `hazina-scan`, send that too.

## If it stops

The last lines always say what happened and what to type next. The usual cases:

| What you see | What to do |
|---|---|
| Your account reached its usage limit | Wait until the limit resets, then run `hazina-review --resume hazina-review-out`. Finished repositories are kept. |
| Not signed in, or the sign-in was refused | Run `claude auth login` (or `codex login`), then `hazina-review --resume hazina-review-out`. |
| A `✗` in the checks before it starts | Fix what that line says, and run the same command again. Nothing was billed. |
| Anything else | Email the file it names, `hazina-review-out/hazina-review-log.txt`, to [partners@hazinalabs.com](mailto:partners@hazinalabs.com). It holds versions, timings and errors, never your code or sign-in details. |

`--resume` only redoes what did not finish, with the same settings as the first run.

---

# Details

## Requirements and providers

Needs Python 3.11+, git, and **one of these commands, installed and signed in**:

| `--provider` | Command | Where it can read |
|---|---|---|
| `claude` | [Claude Code](https://claude.com/claude-code) | Its file tools are limited to the temporary copy and prepared history. |
| `codex` | [Codex CLI](https://developers.openai.com/codex/cli) | Its read-only sandbox prevents writes, but it can inspect other local files readable by your account. |

For Codex, name the model you want with `--model`; the tool does not let that CLI silently
choose a changing default. See [The pre-run checks](#the-pre-run-checks) to check that everything is
in place.

The Claude integration requires `--safe-mode`, `--restricted`, `--strict-mcp-config` and
`--no-session-persistence`. The Codex integration uses `--sandbox read-only`, `--ephemeral`,
`--ignore-rules`, `--ignore-user-config`, and disables repository agent instructions. It
also disables approval escalation. If the selected command lacks a required switch, the
run stops and names it. It never reports an empty result instead.

Install the review wheel supplied by Hazina Labs in its own environment:

```bash
pipx install ./hazina_review-0.1.0-py3-none-any.whl
```

The wheel bundles the scanner library and both prompts. Its version is independent of the
scan release. Use separate pipx environments for the scan and review distributions.

## The pre-run checks

Every run checks this machine first, before it touches a repository, and prints one line per
check: `✓` passed, `✗` failed, `–` skipped, `!` a warning. If any check fails the run stops
with exit code 2 and writes nothing. To run only the checks:

```bash
hazina-review --check /path/to/your-repo
hazina-review --check --provider codex --model <id>
```

| Check | If it fails |
|---|---|
| The provider command is installed | Install it and make sure it is on your PATH. |
| It offers every flag this tool needs | Upgrade it to its latest version. |
| It is signed in (asked of the command itself, or an API key in the environment; no model is called) | Sign in with `claude auth login` or `codex login`, or set the provider's API key variable. An older version that cannot answer is a warning, not a failure. |
| Codex only: its own sandbox can read a file (Linux and macOS) | On Ubuntu 23.10 and later, AppArmor stops programs creating the user namespaces the sandbox needs. The check prints the two commands an administrator runs once to allow it for Codex's sandbox alone, with the path filled in for your machine. Or use `--provider claude`, which needs no sandbox setup. macOS needs no setup. |
| git is installed, and each repository is a git repository | Install git, or pass the top folder of a checkout. A repository with nothing committed is a warning: it is measured, but its review does not complete. |
| The output directory can be written, is not a link, and is not inside a repository | Choose another directory with `--out`. |
| The model answers and can read a file | Named by what went wrong: a usage or rate limit (wait, or check your plan), sign-in (sign in again), a model the account cannot use (pass another with `--model`), a timeout or a dropped connection (check your network). |

The last check starts one small real session, built exactly as the review's own sessions are,
and asks the model to read a file back. **It is billed to your account, a small amount.**
`--skip-model-check` leaves it out; a run can then still stop on your account's limits or
the model. What the provider command prints about your account, such as an email address or
an organisation, is never shown or stored: only whether it is signed in.

## Running, time and billing

```bash
hazina-review /path/to/your-repo
```

Several repositories, one after another:

```bash
hazina-review /path/to/api /path/to/web --out /path/to/hazina-review-out
```

Each repository starts two concurrent model sessions: material census and task mining. Each
is given its own time limit (four hours by default) or whatever is left of the repository
budget (default 150 minutes) when the sessions are set up, whichever is less. Both are billed
to your provider account. If the provider throttles the census or the connection drops, the
census is tried again, up to three attempts in all, with short waits between them; each
attempt is billed. Task mining is attempted once.
The review itself runs no install, build or tests. Claude is given file-reading tools only.
Codex runs under its read-only sandbox, but retains its general command tool; the tool grants
no approval to perform writes.

**This does not replace step 1.** The review does not run the build check that
`hazina-scan` runs, so its `measurement.json` has nothing in it about whether
your code builds. Please send us the zip from step 1 as well as this one.

## Before you send the zip

Every run writes one folder per repository in the output directory (`hazina-review-out` unless you
give `--out`), and one zip of those folders, **`hazina-review-out.zip`**, beside it. The
names differ from hazina-scan's (`hazina-out`, `hazina-out.zip`) so the two tools never share
a folder or overwrite each other's zip; keep them apart if you give `--out` yourself.

**Before you send it, read the sentences it carries.** They are in the two files named below
in each repository's folder: `measurement.json` (the `material` block) and
`codebase_repo_mining.json` (`mined_task_summaries`). The zip also carries short notes the
tool writes itself: how long each stage took and, when a session did not complete, why, with
a masked excerpt of what the provider command printed. The filter
removes anything shaped like a path, a file, a symbol, an address or a capitalised name. **A
name written as an ordinary lowercase word is the one thing it cannot catch**, because nothing
tells such a name from a word. You are the last check. If a sentence names your company, a
product, a customer or a person, do not send the zip; write to us instead.

Then email the zip to **[partners@hazinalabs.com](mailto:partners@hazinalabs.com)**.

Nothing is sent unless you send it.

## What a run looks like

```text
$ hazina-review ~/code/logrus ~/code/api
✓ Provider command: `claude` is installed at ~/.local/bin/claude.
...
[1/2] logrus: starting
[1/2] logrus: still working (census 1m, task mining 1m)
...
[1/2] logrus: census done in 5m 12s
[1/2] logrus: task mining done in 7m 03s (72 tasks)
[2/2] api: starting
...
Results: /home/you/hazina-review-out
Zip to send: /home/you/hazina-review-out.zip

Summary: all 2 repositories done.
```

While the two model sessions run, a line every minute says which are still working. The
times above are an illustration, not a measurement.

## If something goes wrong

Every problem is said in plain words, with the exact command that deals with it, and every
message about a problem ends with where the support file is:

```text
If you need help, email /home/you/hazina-review-out/hazina-review-log.txt to partners@hazinalabs.com.
```

**Problems that would meet every repository stop the run** once the repository in progress
has finished its sessions: your account's usage limit, a refused sign-in, a model your
account cannot use, or a provider command that is missing or no longer offers the switches
this tool needs. No further repository is started, so nothing more is billed. What was done
is kept and zipped. The summary counts repositories done, incomplete and not started, and
names only the incomplete ones with the reason (the first ten; the rest are in the support
file). The message then says what to do, for example:

```text
Summary: 1 of 3 repositories done, 1 incomplete, 1 not started.
  api — Claude's usage limit was reached

The run stopped: your Claude account reached its usage limit.
Nothing that was finished is lost: the zip holds the 1 finished repository.
When the limit resets, carry on with:
    hazina-review --resume /home/you/hazina-review-out
```

For a refused sign-in it gives the sign-in command (`claude auth login` or `codex login`); for
a model the account cannot use, it says a resumed run keeps its model, and that a new run
with another `--model` needs a new `--out`. **Ctrl-C** stops the same way: what is done is
kept and zipped, and the resume command is printed.

**Problems with one repository do not stop the run**: a session that runs out of time,
an answer that cannot be read, a provider command that crashed. That repository is written
with what could be measured and listed as `incomplete`, the others carry on, and the summary
offers the resume command to try just the incomplete ones again.

**Resuming.** `hazina-review --resume <output directory>` carries on with the repositories
that are not done, with the provider, model and limits the run started with; the checks in
[The pre-run checks](#the-pre-run-checks) run again first. Giving `--model`, `--provider`, a limit or
repositories with `--resume` is refused, because every result in one batch must come from one
model; to change them, start a new run with a new `--out`. Repositories already done are not
run again, and the zip is rewritten with every done repository. A repository whose folder is
no longer there is skipped, said plainly, and left as not started. Resuming a run whose every
repository is already done runs no check and starts nothing: it says so, names the results
and the zip, and exits with code 0.

**The two files beside the folders.** `hazina-review-progress.json` records the run's options
and where each repository stands, and is what `--resume` reads. `hazina-review-log.txt` is the
support file: what happened, one timestamped line at a time, added to by every run and resume
in that directory. It holds the tool, system, Python, git and provider command versions, the
checklist, each repository's start and end, how long each session took, how it ended and the
provider command's exit code, the masked excerpt the results carry, why the run stopped and
the summary. It never holds the model's answers or findings, file contents, sign-in details,
email addresses, keys or tokens, and your home directory is written as `~`. Read it before you
send it, as you would the zip. Neither file goes into the zip. If something other than a
plain file of the run's own is at either name (a symbolic link, a named pipe, a folder, or a
hard link to a file elsewhere), the run stops before the checks with exit code 2, nothing
written and nothing billed; remove it, or choose another `--out`.

If a check fails before the run starts, nothing is written; copy the checklist lines into an
email to [partners@hazinalabs.com](mailto:partners@hazinalabs.com).

## What the model can see

- **A temporary copy of your committed files, never your working folder.** The copy is made
  from git at `HEAD`, so uncommitted edits and untracked files (a live `.env`, for example)
  are not in it, and a file you have deleted but not yet committed is not in it either. It is
  deleted when the run ends, however the run ends.
- **Your commit history**, written out as read-only files next to that copy, and deleted with
  it. The history is written out in full whatever time is left, and the sessions are started
  even if git lists no commit that meets the bar below. Not all of it: only the commits on any branch that changed two or more source files and
  between 20 and 10,000 lines, merges left out. Each is listed with its commit hash, its date,
  its subject, how much it changed and the first few paths it touched, and up to 160 of them,
  spread evenly from newest to oldest, are written out in full. Nobody's name or address is
  written.
- **Password-shaped files are left out** of the copy and out of the history: `.env*`,
  `credentials*`, `secrets.*`, key and certificate files, the files a tool keeps a login in, and
  similar, recognised by name.
- **In the history, a file is recognised by its own name only.** A file inside a folder with
  such a name (for example `.env/prod`) is left out of the copy, but its changes are written
  out with the rest of the history.
- **A password file that was later renamed to an ordinary name cannot be recognised by name.**
  Its old content is still in your history and would be visible to your own model provider.
  It is never visible to Hazina Labs.
- **What the selected CLI can read differs.** Claude's file tools are held to those two
  folders. Codex's read-only sandbox prevents writes, but does not limit reads to those
  folders; it can inspect other files your account can read. Choose that provider only if
  you accept this wider read scope.
- Configuration files committed to your repository (agent instructions, settings, hooks, tool
  declarations) are ignored. The repository cannot configure the model that reads it.
- **The provider command runs under your own login and home directory, which is how it uses
  your sign-in.** Of your environment it is handed its own sign-in variables, by name, and no
  other credential. User configuration and repository instructions are ignored for this run.
- The session is not saved, so no transcript of it is kept.

## Output layout

```text
hazina-review-out/
  requests/                     one folder per repository, named after its directory
    measurement.json
    codebase_repos.json
    codebase_repos.csv
    codebase_repo_mining.json
  api/
  api-2/                        a second repository whose directory is also called api
  hazina-review-progress.json   where the run stands, for --resume (never zipped)
  hazina-review-log.txt         the support file (never zipped)
hazina-review-out.zip                  beside the output directory
```

Each repository's folder holds exactly these four files; beside the folders the run keeps only
its progress file and support log. The
folder is the name of the repository's directory on your machine; when two repositories
share a name, the second gets `-2`, the third `-3`, and so on, skipping any number that
would give another repository's own name: `api`, `api` and `api-2` are written to `api`,
`api-3` and `api-2`. Every repository of a run gets a folder of its own.

The zip holds every repository of the run whose files were written, each as `<folder name>/`
with its four files at the top of the archive, for example `requests/measurement.json`. A
repository listed as incomplete because of one repository's problem is included, with its
empty counts; one stopped by a problem that meets every repository (a usage limit, sign-in,
the model or the provider command) is left out until a resumed run redoes it. It never holds
anything else found in a reused output folder.

An output directory used by an earlier version may hold that version's local files:
`INDEX.local.txt` beside the folders and `DETAIL.local.md` inside them, with the model's raw
findings. The run removes them before it writes anything, printing one line for each, for
example `removed old DETAIL.local.md from hazina-review-out/requests (raw findings from an earlier
run)`. It removes only what it can tell an earlier run of this tool wrote: a `DETAIL.local.md`
in a folder that also holds that run's `measurement.json` and `codebase_repos.json`, and an
`INDEX.local.txt` that starts with the header the earlier version wrote and whose folders still
there are all such folders (hazina-scan's index starts with the same header, but its folders never hold
`DETAIL.local.md`). A file by either name that does not look like this tool's output is left
where it is, with one line saying so. Nothing else in the directory is removed. If either name
is a symbolic link, the run stops before anything is removed or written; remove the link, or
choose an empty directory.

## What is in the zip

Three measurement files with the same names as `hazina-scan` writes, plus
`codebase_repo_mining.json`. They differ from step 1's
in two ways. The review does not run the build check, so `measurement.json` has no `build`
block here and `build_ok` in the other two files is empty; the step 1 zip is where we read
those from. And `measurement.json` gains one block, `material`:

| Field | Contents |
|---|---|
| Counts | How many findings in each of nine categories (complex logic, substantial features, defect repairs, and so on), a 0-6 depth band, a 0-4 self-containment score. |
| Sentences | **One sentence per finding**, describing the shape of the work and naming nothing. Up to five short themes, a short summary, and one sentence on what the model could not assess. |
| What was masked | Which kinds of thing were masked out of those sentences, as labels from a fixed list of twelve (`a filesystem path`, `a class name`, and so on). Never the thing itself. |
| The run | The name of the measurement (`material_census`), which provider and model you asked for, whether the review completed, and a note of how long each stage took in seconds. |
| A review that did not complete | Why, as one of eight fixed kinds (`rate_limited`, `auth`, `model_unavailable`, `timeout`, `no_json`, `crashed`, `cli_missing`, `unknown`), how many attempts were made, whether another try could go differently, a sentence saying what happened, and a short excerpt of what the provider command printed. The excerpt is masked the same way as the sentences, and emptied if it still names anything. |

Each repository's `codebase_repos.json` also carries a `record_id` that lets us check the files
were not changed after they were generated.

`null` means *not measured*. If the model times out, the provider refuses it, or no JSON
can be read from its answer, the run still finishes, that lane's counts are `null`, and its
block says why. A completed assessment from the other lane is kept. The same happens, before
any model session is started or billed, when there is nothing sound to start one on: no
commit or no committed file to read, or a temporary copy that came out incomplete. That never
reads as "nothing found".

An answer that can be read is scored as it stands. If the model leaves a category out, that
category counts as zero; nothing is refused for being incomplete.

## Task mining

Task mining enumerates substantial exercises grounded in the source and history. Its four
categories are `net_new`, `bug_repair`, `repo_evolution` and `agentic` (other complex tasks).
`codebase_repo_mining.json` contains the total, the four counts, and descriptions tagged by
category. The same counts appear under `material.task_type_counts` in `measurement.json`.
Evidence paths, titles and the full answers are not written anywhere.

A task whose description names something still counts, but that description is omitted.
There can therefore be fewer descriptions than tasks. The census uses its own counting rule:
it counts surviving descriptions. Neither lane changes your repository.

The default task ceiling is 150. `--mine-n 80` lowers it. The ceiling is a limit, not a
target: the model is asked for every task that clears the bar, up to that many. Every task
returned counts, even past the ceiling. `mine_unavailable_reason` is a sentence written by
the tool: when the ceiling is reached it says the counts are a floor rather than a total (the
CLI says so too); a completed empty task list has zero counts and a sentence saying none
cleared the bar; a failed turn has null counts and a sentence saying what stopped it, which
can quote the start of what the provider command printed, masked and emptied if it still
names anything. Each object in the answer is one task, and a single object is one task; a
task with a malformed list field stops the lane. The mining `scored` flag indicates whether
any candidates were counted, so use null counts to distinguish a failed run from measured zero.

A row is `measured` only when both assessments completed. Otherwise it is `partial`, with
`lanes_timed_out` (the census ran out of time) or `lanes_unavailable` (any other failure,
including mining running out of time) beside it in JSON, CSV and the CLI. Valid results from the other lane are retained.

## Options

| Flag | Meaning |
|---|---|
| `--out DIR` | Where to write results (default `./hazina-review-out`). Must be outside the repositories. |
| `--resume DIR` | Carry on with the run whose output directory is `DIR`, with the options it started with. See [If something goes wrong](#if-something-goes-wrong). |
| `--provider claude\|codex` | Which command to use. The default is Claude. |
| `--model ID` | Exact model id. Claude defaults to the pinned `claude-opus-5`; Codex requires this flag. Different models can produce different measurements. |
| `--mine-n N` | Maximum tasks to enumerate (positive integer, default 150). |
| `--budget-seconds N` | Time budget for the repository (positive, default 9000). Step 1's measurement comes out of it, and each model session is limited to what is left of it when the sessions are set up. Preparing the copy and the history happens after that and is not deducted, so a run can take somewhat longer than this. |
| `--census-timeout N` | The census session's own limit in seconds, retries and waits included (positive, default 14400). |
| `--mine-timeout N` | The mining session's own limit in seconds (positive, default 14400). |
| `--check` | Run only the checks in [The pre-run checks](#the-pre-run-checks), then exit: `0` when all pass, `2` otherwise. Repositories are optional. |
| `--skip-model-check` | Leave out the one small billed session that confirms the model can read a file. |

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Every repository is done. |
| `1` | The run went to the end, and some repository is incomplete or was not started (for example, its folder was gone on resume). |
| `2` | The run stopped early (a problem that meets every repository, or Ctrl-C), or could not start: a usage error, a failed check (see [The pre-run checks](#the-pre-run-checks)), or nothing to resume. A run that could not start writes nothing. |

---

**Hazina Labs** · [hazinalabs.com](https://hazinalabs.com) ·
[partners@hazinalabs.com](mailto:partners@hazinalabs.com)
