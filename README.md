# hazina-review

hazina-review measures how much substantial engineering work your repositories hold. It runs
on your computer, uses your own AI account (Claude or Codex) to read the code, and gives you
one zip to send to Hazina Labs.

Nothing leaves your computer until you send the zip yourself.

Questions: **[partners@hazinalabs.com](mailto:partners@hazinalabs.com)**

## 1. What you need

- Python 3.11 or newer, `git` and `pipx`
- **One** AI tool, installed and signed in: **Claude Code** or **Codex**

Claude Code is used by default. To use Codex, add `--provider codex` to the commands below.
hazina-review is tested on macOS and Linux.

Already have everything? Skip to [Install](#2-install). Otherwise, open the steps you need:

<details>
<summary><b>Python, git and pipx</b></summary>

Check what you have:

```bash
python3 --version      # needs 3.11 or newer
git --version
pipx --version
```

Install what is missing:

| | macOS ([Homebrew](https://brew.sh)) | Ubuntu / Debian |
|---|---|---|
| Python | `brew install python` | `sudo apt install python3` |
| git | `brew install git` | `sudo apt install git` |
| pipx | `brew install pipx` | `sudo apt install pipx` |

Then run `pipx ensurepath` once and open a new terminal.

</details>

<details>
<summary><b>Claude Code</b> (install and sign in)</summary>

Install:

```bash
curl -fsSL https://claude.ai/install.sh | bash      # macOS or Linux
brew install --cask claude-code                     # or, on macOS with Homebrew
```

On Windows (PowerShell): `irm https://claude.ai/install.ps1 | iex`

Sign in:

```bash
claude auth login
```

A browser window opens. Log in with a Claude Pro, Max, Team or Enterprise account, or with
your Claude Console account. Check it worked:

```bash
claude auth status      # should say "loggedIn": true
```

Using an API key instead? Set `ANTHROPIC_API_KEY` in your terminal before you run the tool.

</details>

<details>
<summary><b>Codex</b> (install and sign in)</summary>

Install:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh     # macOS or Linux
brew install --cask codex                                # or, on macOS with Homebrew
npm install -g @openai/codex                             # or, with npm
```

Sign in:

```bash
codex login
```

Choose **Sign in with ChatGPT** and use a Plus, Pro, Business, Edu or Enterprise account.
Check it worked:

```bash
codex login status      # should say you are logged in
```

Using an API key instead? `printenv OPENAI_API_KEY | codex login --with-api-key`

On Ubuntu 23.10 and later, Codex also needs a one-time setup by an administrator. The check in
step 3 prints the exact commands.

</details>

The AI tool's use is billed to that account: a subscription's usage limits, or API charges.

## 2. Install

```bash
pipx install git+https://github.com/sandeepmallareddy/hazina-review.git@v0.1.3
```

Or download the `.whl` file from the
[latest release](https://github.com/sandeepmallareddy/hazina-review/releases/latest) and run
`pipx install ./hazina_review-0.1.3-py3-none-any.whl`.

## 3. Check your computer

```bash
hazina-review --check --skip-model-check
```

This is free and takes a few seconds. If a line starts with `✗`, it tells you what to fix.

## 4. Run

Use a **fresh copy** of each repository, not the folder you work in. The tool runs the
project's own install, build and tests, and that changes files.

```bash
git clone <your-repository-url> ~/hazina/my-repo
hazina-review ~/hazina/my-repo
```

Several repositories, one after another:

```bash
hazina-review ~/hazina/api ~/hazina/web ~/hazina/mobile
```

Many repositories: clone them all into one folder, then review every repository in it:

```bash
hazina-review --all ~/hazina
```

- It usually takes 15 to 60 minutes per repository. It prints a line every minute so you
  know it is still working.
- The AI part is billed to your Claude or Codex account. The build and tests cost nothing
  extra.

### If it stops

The last lines always say what happened and what to type next.

| What you see | What to do |
|---|---|
| Your account reached its usage limit | When the limit resets, run `hazina-review --resume hazina-review-out`. Finished repositories are kept. |
| Not signed in | Run `claude auth login` (or `codex login`), then `hazina-review --resume hazina-review-out`. |
| A `✗` in the check | Fix what that line says, then run the same command again. Nothing was billed. |
| The build or tests fail | Nothing to do. Whether it builds is part of the result. |
| Anything else | Email `hazina-review-out/hazina-review-log.txt` to [partners@hazinalabs.com](mailto:partners@hazinalabs.com). It holds versions, timings and errors, never your code or sign-in details. |

`--resume` only redoes what did not finish, with the same settings as before.

## 5. Send

At the end it prints the zip to send:

```text
Zip to send: /home/you/hazina-review-out.zip
```

Then email the zip to **[partners@hazinalabs.com](mailto:partners@hazinalabs.com)**.

## Options

| Option | What it does |
|---|---|
| `--all DIR` | Review every git repository directly inside `DIR`. |
| `--out DIR` | Where to put the results (default `./hazina-review-out`). |
| `--no-build` | Skip the install, build and tests. Faster, but those results are left empty. |
| `--provider codex` | Use Codex instead of Claude. |
| `--model ID` | Use a different model. Defaults: `claude-opus-5` for Claude, `gpt-6-sol` for Codex. |
| `--resume DIR` | Carry on with a run that stopped. |
| `--check` | Only check your computer. Add `--skip-model-check` to make it free. |

`hazina-review --help` lists every option.

## Privacy

- The AI reads a temporary copy of your committed files and history, which is deleted
  afterwards. Password-like files (`.env`, keys, credentials) are left out of the copy.
- The zip holds numbers and short sentences describing the kind of work, never your code.
  It also names each repository and the company it belongs to.
- With Claude, the AI can read only that copy. With Codex, its sandbox blocks writes but it
  can read other files your account can read.

Full details: [SECURITY.md](SECURITY.md).

---

**Hazina Labs** · [hazinalabs.com](https://hazinalabs.com) ·
[partners@hazinalabs.com](mailto:partners@hazinalabs.com)
