# Security and privacy

Report a concern privately to **partners@hazinalabs.com**. Include the tool version and a
minimal reproduction without source, secrets or raw model output from a client repository.

## What runs

The review launches the selected provider command under the operator's account. The model
provider receives the source it reads. Nothing is uploaded to Hazina Labs by this tool.
No project install, build or test command is run by a review.

Both model sessions read one temporary snapshot and one prepared history directory. Files
with credential-shaped names, symbolic links and submodules are excluded. In the snapshot a file is
also excluded when a folder above it has a credential-shaped name; in the history only the
file's own name is judged, so the changes to a file such as `.env/prod` are written out. Untracked files
and uncommitted content are not copied. Tracked files deleted locally are also omitted.
Secrets committed under ordinary names cannot be recognized by this name-based exclusion.

A local checkout carries its own git configuration, and some settings there name a program
for git to run with the operator's full access, outside the provider's sandbox. Every git
command the review and the bundled scanner run while measuring a repository switches off the
filesystem monitor (`core.fsmonitor`) and signature display on log (`log.showSignature`), and
passes `--no-ext-diff` and `--no-textconv` to `log`, `show` and `diff`. Clean filters cannot
be switched off by name, so no git command compares working-tree contents: history is read from
commits, and locally deleted files are found from the index without reading any file. The
signature checker override is not exercised by tests, because it only runs for signed
commits.

The deterministic measurement reads the working tree, not the snapshot. It reads a file only
when it is a regular file, not a symbolic link, whose resolved path is inside the repository,
and it never enters a linked directory, so a link to a file elsewhere or to a FIFO is skipped.

The selected provider must advertise its required flags before a review begins. The default's
restricted mode removes command and web tools and limits its file tools to the prepared
directories. The second command runs in read-only mode with approval escalation disabled, but its
general command tool and wider read access remain: it can inspect other local files
readable by the operator. Both
integrations ignore repository instructions and avoid local session persistence. Only
the provider's named authentication variables and home directory cross the model
process boundary. Checking help text cannot prove that a binary honors its flags.

Temporary files are removed on normal completion and handled exceptions. An operating-system
crash or forced process termination can leave temporary files behind. Provider retention and
billing follow the operator's account terms, independently of local session persistence.

## What can be shared

The zip contains exactly the four declared measurement files: deterministic measurements,
review counts, and filtered task descriptions. Other files in reused output folders are excluded.
Each repository's `codebase_repos.json` carries a `record_id` that lets us check the files were
not changed after they were generated; editing them afterwards makes that check fail.
Repository and owning-company identity fields follow the scanner's identity policy; the zip
is not a promise of complete anonymity. Model-derived prose is checked for paths, symbols,
addresses and capitalized names. A lowercase proper name can still resemble an ordinary word.
Read the sentences in `measurement.json` (`material`) and `codebase_repo_mining.json` before
sending the zip. Each repository's folder, in the output directory and in the zip, carries the
name of the repository's directory on the operator's machine.

A reused output directory is checked before anything runs and again at every write: a
symbolic link at a repository's folder, at any file in it or at the zip is refused rather
than written through, and the zip never packs a link.

Raw answers and evidence paths are not kept once the run ends. An earlier version kept them
in `DETAIL.local.md` in each repository's folder and `INDEX.local.txt` beside the folders; when
an output directory is reused, the run removes those files before it writes anything and prints
one line for each. Only a regular file with exactly that name, in exactly that place, is
removed, and only when it is recognisably an earlier review's: a `DETAIL.local.md` whose folder
also holds that run's `measurement.json` and `codebase_repos.json` as regular files, and an
`INDEX.local.txt` whose first line is the header the earlier version wrote and whose listed
folders still there are all such folders. Any other file by those names is left in place with a one-line
warning; a symbolic link at either name stops the run with nothing removed or written, and a
linked folder is never entered. Never send the output directory wholesale in place of the
generated zip.

Failed sessions and answers with no readable JSON produce null counts, not measured zeros.
A readable answer is scored as it stands: a category the model left out counts as zero. A
throttled or dropped census session is retried, up to three attempts, each billed to the
operator's account. When a session fails, the shareable block carries its failure kind and a
short excerpt of what the provider command printed, masked like model prose and emptied if it
still names anything.
Subprocess output is currently captured without a size bound. Build operations offered by the
bundled scan command have a different execution policy; use throwaway clones for those.

## The progress file and the support log

Beside the repository folders, a run keeps two files of its own. Neither is packed into the
zip, and neither is removed by the clean-up of an earlier version's local files.

`hazina-review-progress.json` records the run's options (provider, model, limits, task
ceiling, tool version), each repository's local path and folder name, and where each stands.
It is what `--resume` reads, and it is replaced whole after every repository, never edited in
place. It holds full local paths, so it is not meant to be sent.

Both files are this run's own. Before the checks (and so before the billed model check), each
name is looked at without following a link, and anything there that is not a regular file
with exactly one name stops the run with exit code 2 and nothing written or billed: a
symbolic link, a named pipe (writing to it would wait forever), a device, a folder, or a hard
link (a second name for a file that may be anywhere on the disk). Every write checks again: the
file is opened never through a link and never waiting on a pipe, and the opened file must be a
regular file with one name, the same one now at that name in the output directory, before a
byte is written. The progress file is written to a temporary name that is made new each time
and moved into place.

`hazina-review-log.txt` is the support file an operator may email to us. Every run and resume
in the directory adds to it under its own header. It contains: a timestamp on every line; the
tool version; the operating system and its version; the Python and git versions; the provider
command's name and the first line of its `--version` output; the pre-run checklist; for each
repository, its folder name, when it started and ended, how long each session took, how it
ended (the failure kind) and the provider command's exit code, and the masked excerpt that the
shareable block itself carries; why the run stopped; and the final summary.

It never contains: the model's answers or findings, file contents, sign-in status details
(account, organisation, email address), keys, tokens or passwords. Every line is scrubbed as
it is written: the values of the provider's sign-in variables and of any environment variable
whose name suggests a credential are removed, as are email addresses and token-shaped strings,
and the home directory is written as `~`. The log does name repository folders and paths under the home directory, so read it before
sending it.
