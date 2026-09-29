import re

from hazina_review import brief
from hazina_scan import history
from tests.conftest import _git, make_repo, work


def _everything_written(out):
    return "\n".join(p.read_text() for p in out.rglob("*") if p.is_file())


def _rows(out):
    """The overview's table as one dict a commit, under the column names it prints."""
    table = [
        [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip("|"))]
        for line in (out / brief.OVERVIEW_NAME).read_text().splitlines()
        if line.startswith("|")
    ]
    return [dict(zip(table[0], cells, strict=True)) for cells in table[2:]]


def _diffs(out):
    return sorted(p.name for p in (out / brief.DIFF_SUBDIR).iterdir())


def _numbers(out):
    """The sample positions the patch files are numbered with."""
    return [int(name[:4]) for name in _diffs(out)]


def test_the_layout_is_an_overview_and_a_directory_of_diffs(tmp_path, git_repo_with_many_commits):
    out = tmp_path / "brief"
    result = brief.write_brief(git_repo_with_many_commits, out, max_commits=5)
    assert sorted(p.name for p in out.iterdir()) == ["HISTORY-OVERVIEW.md", "commit-diffs"]
    shas = result["patched"]
    assert _diffs(out) == [f"{i:04d}-{sha[:12]}.diff" for i, sha in enumerate(shas)]


def test_a_patch_opens_with_the_hash_the_date_and_the_subject(tmp_path, repo_builder):
    repo = repo_builder({}, commits=[{"msg": "one", "files": work(1)}])
    out = tmp_path / "brief"
    (sha,) = brief.write_brief(repo, out)["patched"]
    text = (out / brief.DIFF_SUBDIR / f"0000-{sha[:12]}.diff").read_text()
    assert text.startswith(f"{sha}\n2024-01-15\none\n\n\ndiff --git ")


def test_credential_paths_are_recognised():
    for path in ("app/.env", ".env.production", "deploy/secrets.yml", "id_rsa"):
        assert brief.is_credential_path(path), path


def test_credential_paths_are_recognised_whatever_their_case():
    for path in (".ENV.Production", "keys/server.PEM"):
        assert brief.is_credential_path(path), path


def test_only_the_last_segment_of_a_path_is_judged():
    # A file under a credential-shaped directory is not withheld from the history; the
    # scratch copy of the tree still leaves it out.
    for path in (".env/prod", "svc/credentials/aws"):
        assert not brief.is_credential_path(path), path


def test_credential_shapes_beyond_environment_files_are_recognised():
    for path in (".netrc", ".npmrc", "ops/kubeconfig", "vault.kdbx", "id_rsa.pub"):
        assert brief.is_credential_path(path), path
    for path in ("signing/release.asc", "gcp/service-account-prod.json", "tls/server.key"):
        assert brief.is_credential_path(path), path


def test_a_renamed_path_is_judged_as_the_string_git_printed():
    assert brief.is_credential_path("settings.txt => .env")
    for path in ("config/{.env => settings.txt}", '"my dir/.env"', "src/{old => new}/engine.py"):
        assert not brief.is_credential_path(path), path


def test_ordinary_source_is_not_mistaken_for_a_credential():
    for path in ("src/token_bucket.py", "lib/secretsanta.go", "docs/environment.md"):
        assert not brief.is_credential_path(path), path


def test_committed_credential_never_reaches_the_brief(tmp_path, git_repo_with_env_file):
    out = tmp_path / "brief"
    result = brief.write_brief(git_repo_with_env_file, out)
    body = _everything_written(out)
    assert "SUPER_SECRET_VALUE" not in body
    assert result["credential_paths_excluded"] == 1
    assert "their contents are deliberately not included" in body


def test_credential_content_stays_out_wherever_its_own_name_is_credential_shaped(
    tmp_path, git_repo_with_credential_history
):
    out = tmp_path / "brief"
    result = brief.write_brief(git_repo_with_credential_history, out)
    body = _everything_written(out)
    # Only the two files inside credential-shaped directories are shown: their own names
    # are ordinary.
    assert sorted(set(re.findall(r"LEAK_\w+", body))) == ["LEAK_CRED_DIR", "LEAK_ENV_DIRECTORY"]
    # the ordinary files beside them are still there, so the patches are not simply empty
    assert "+e3_0 = 0" in body
    # three of the seven commits touch nothing but withheld paths, and git has nothing to say
    # about those: they get no patch, rather than an empty one
    assert result["commits_substantive"] == 7
    assert result["patches_written"] == 4 and result["patches_empty"] == 3
    assert _numbers(out) == [0, 3, 5, 6]
    assert result["credential_paths_excluded"] == 3


def test_the_overview_names_no_credential_shaped_file(tmp_path, git_repo_with_credential_history):
    out = tmp_path / "brief"
    brief.write_brief(git_repo_with_credential_history, out)
    rows = _rows(out)
    assert [row["subject"] for row in rows][:3] == ["directories", "alone", "rename"]
    assert rows[1]["changed paths"] == ""
    # the count beside them is of every file the commit changed, shown or not
    assert rows[-1]["files"] == "4" and rows[-1]["changed paths"] == "src/engine.py, src/rules.py"
    shown = " ".join(row["changed paths"] for row in rows[3:]).lower()
    for shape in (".env", "secrets", ".pem", "credentials"):
        assert shape not in shown, shape


# --- which commits the agent is shown ------------------------------------------------------------


def test_only_substantive_commits_are_listed_and_patched(tmp_path):
    lock = "".join(f"pin {i}\n" for i in range(30))
    commits = [
        {"msg": "two files and enough churn", "files": work(1)},
        {"msg": "one file however long", "files": {"src/engine.py": "x = 1\n" * 60}},
        {"msg": "two files and a line each", "files": {"src/a.py": "a\n", "src/b.py": "b\n"}},
        {"msg": "one file beside a lockfile", "files": {"src/a.py": "aa\n", "yarn.lock": lock}},
        {
            "msg": "a lockfile adds churn",
            "files": {"src/a.py": "A\n", "src/b.py": "B\n", "yarn.lock": lock * 2},
        },
        {"msg": "a tree arriving whole", "files": work(2, lines=6000)},
        {
            "msg": "tests are files too",
            "files": {"tests/test_a.py": "t = 1\n" * 15, "tests/test_b.py": "t = 2\n" * 15},
        },
    ]
    repo = make_repo(tmp_path, {}, commits)
    out = tmp_path / "brief"
    result = brief.write_brief(repo, out)
    assert [row["subject"] for row in _rows(out)] == [
        "tests are files too",
        "a lockfile adds churn",
        "two files and enough churn",
    ]
    assert result["commits_listed"] == 7 and result["commits_substantive"] == 3
    assert result["patches_written"] == 3
    assert _numbers(out) == [0, 1, 2]


def test_the_bar_is_the_one_step_one_counts_mineable_commits_with():
    assert (brief.MIN_IMPL_FILES, brief.MIN_CHURN, brief.MAX_CHURN) == (
        history.MIN_IMPL_FILES,
        history.MIN_CHURN,
        history.MAX_CHURN,
    )


def test_every_ref_is_read_and_a_merge_is_not(tmp_path):
    repo = make_repo(tmp_path, {}, [{"msg": "base", "files": work(0)}])
    _git(repo, "checkout", "-q", "-b", "side")
    for rel, text in {"side/one.py": "s = 1\n" * 15, "side/two.py": "s = 2\n" * 15}.items():
        (repo / rel).parent.mkdir(exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "on the side")
    _git(repo, "checkout", "-q", "main")
    for rel, text in work(1).items():
        (repo / rel).write_text(text)
    _git(repo, "commit", "-q", "-a", "-m", "on main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "joined", "side")
    _git(repo, "checkout", "-q", "-b", "unmerged")
    for rel, text in work(2).items():
        (repo / rel).write_text(text)
    _git(repo, "commit", "-q", "-a", "-m", "never merged")
    _git(repo, "checkout", "-q", "main")
    out = tmp_path / "brief"
    result = brief.write_brief(repo, out)
    subjects = {row["subject"] for row in _rows(out)}
    assert subjects == {"base", "on the side", "on main", "never merged"}
    assert result["commits_listed"] == 4


def test_a_row_carries_the_date_the_files_the_churn_and_the_first_paths(tmp_path):
    many = {f"pkg/m{i:02d}.py": "v = 1\n" * 3 for i in range(11)}
    commits = [{"msg": "wide | change", "files": many, "date": "2023-03-04T10:00:00+00:00"}]
    repo = make_repo(tmp_path, {}, commits)
    out = tmp_path / "brief"
    brief.write_brief(repo, out)
    (row,) = _rows(out)
    (name,) = _diffs(out)
    assert row["commit"] == f"`{name[5:17]}`" and row["date"] == "2023-03-04"
    assert row["files"] == "11" and row["churn"] == "33" and row["patch"] == f"`{name}`"
    shown = ", ".join(f"pkg/m{i:02d}.py" for i in range(8))
    assert row["changed paths"] == f"{shown} +3 more"
    assert row["subject"] == "wide \\| change"


def test_the_overview_counts_what_it_lists(tmp_path, git_repo_with_many_commits):
    out = tmp_path / "brief"
    brief.write_brief(git_repo_with_many_commits, out, max_commits=5)
    text = (out / brief.OVERVIEW_NAME).read_text()
    assert "- refs in the repository: 1\n" in text
    assert "- commits reachable from all refs: 40\n" in text
    assert "- commits that look like real multi-file development: 40\n" in text
    assert "- of those, full patches written to `commit-diffs/`: 5\n" in text
    assert "sampled at even intervals across the whole history" in text
    assert "withheld" not in text


def test_nobody_who_wrote_a_commit_is_named(tmp_path):
    commits = [{"msg": "work", "files": work(1), "name": "Ada Example", "email": "ada@example.com"}]
    repo = make_repo(tmp_path, {}, commits)
    out = tmp_path / "brief"
    brief.write_brief(repo, out)
    body = _everything_written(out)
    assert "Ada" not in body and "example.com" not in body


def test_a_history_of_small_commits_is_read_and_holds_nothing(tmp_path):
    commits = [{"msg": f"fix {n}", "files": {"a.py": f"x = {n}\n"}} for n in range(5)]
    repo = make_repo(tmp_path, {}, commits)
    out = tmp_path / "brief"
    result = brief.write_brief(repo, out)
    assert result["commits_listed"] == 5 and result["commits_substantive"] == 0
    assert result["patches_written"] == 0 and result["patches_empty"] == 0
    assert _rows(out) == [] and _diffs(out) == []
    assert (
        "- commits that look like real multi-file development: 0\n"
        in (out / brief.OVERVIEW_NAME).read_text()
    )


def test_commits_are_sampled_at_even_intervals_from_the_newest(
    tmp_path, git_repo_with_many_commits
):
    out = tmp_path / "brief"
    result = brief.write_brief(git_repo_with_many_commits, out, max_commits=5)
    assert result["patches_written"] == 5
    # forty commits and room for five: every eighth, counted from the newest
    rows = _rows(out)
    assert [n for n, row in enumerate(rows) if row["patch"]] == [0, 8, 16, 24, 32]
    assert _numbers(out) == [0, 1, 2, 3, 4]
    assert result["patched"] == result["chosen"] and len(result["chosen"]) == 5


def test_a_sample_is_cut_the_same_way_whatever_the_lengths():
    picked = brief._sample(list(range(7)), 3)
    assert picked == [0, 2, 4]
    assert brief._sample(list(range(3)), 3) == [0, 1, 2]
    assert brief._sample(list(range(1000)), 160)[:4] == [0, 6, 12, 18]
    assert brief._sample(list(range(9)), 0) == list(range(9))
    assert brief._sample(list(range(9)), -1) == list(range(9))


def test_every_substantive_commit_keeps_its_row_sampled_or_not(
    tmp_path, git_repo_with_many_commits
):
    out = tmp_path / "brief"
    brief.write_brief(git_repo_with_many_commits, out, max_commits=5)
    rows = _rows(out)
    assert len(rows) == 40
    assert rows[0]["subject"] == "step 39" and rows[-1]["subject"] == "step 0"
    names = iter(_diffs(out))
    for row in rows:
        if row["patch"]:
            name = next(names)
            assert row["patch"] == f"`{name}`" and row["commit"] == f"`{name[5:17]}`"
    assert sum(1 for row in rows if row["patch"]) == 5


def test_an_oversized_patch_is_cut_at_the_ceiling(tmp_path, repo_builder):
    repo = repo_builder({"big.txt": "line\n" * 2500, "large.txt": "line\n" * 2500})
    out = tmp_path / "brief"
    brief.write_brief(repo, out, max_diff_bytes=1000)
    (name,) = _diffs(out)
    text = (out / brief.DIFF_SUBDIR / name).read_text()
    assert text.endswith("\n[patch truncated at the size limit]\n")
    assert len(text) == 1000 + len("\n[patch truncated at the size limit]\n")


def test_the_ceilings_count_characters_not_bytes(tmp_path, repo_builder):
    wide = ("漢" * 100 + "\n") * 30
    repo = repo_builder({}, commits=[{"msg": "wide", "files": {"a.py": wide, "b.py": wide}}])
    out = tmp_path / "brief"
    brief.write_brief(repo, out, max_diff_bytes=1000)
    (name,) = _diffs(out)
    text = (out / brief.DIFF_SUBDIR / name).read_text(encoding="utf-8")
    assert len(text) == 1000 + len("\n[patch truncated at the size limit]\n")
    assert len(text.encode("utf-8")) > 2000


def test_the_total_ceiling_is_checked_before_each_patch(tmp_path, git_repo_with_many_commits):
    out = tmp_path / "brief"
    result = brief.write_brief(git_repo_with_many_commits, out, max_commits=5, max_total_bytes=1)
    # the first patch is written in full, and the total it leaves stops the rest
    assert result["patches_written"] == 1 and result["bytes"] > 1


def test_a_directory_that_is_not_a_repository_yields_no_patches(tmp_path):
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    out = tmp_path / "brief"
    result = brief.write_brief(empty, out)
    assert result["commits_listed"] == 0 and result["commits_substantive"] == 0
    assert result["patches_written"] == 0 and result["ok"] is False
    assert result["credential_paths_excluded"] == 0
    assert _diffs(out) == []
    text = (out / brief.OVERVIEW_NAME).read_text()
    assert "- refs in the repository: 0\n" in text
    assert "- commits reachable from all refs: unknown\n" in text


# --- a blank answer from git gives no patch, and nothing else stops the brief ------------------


def test_a_pathspec_git_rejects_leaves_every_patch_blank(
    tmp_path, monkeypatch, git_repo_with_many_commits
):
    rejected = (*brief._EXCLUDE_PATHSPECS, ":(exclude,glob,icase,nonsense)**/x")
    monkeypatch.setattr(brief, "_EXCLUDE_PATHSPECS", rejected)
    out = tmp_path / "brief"
    result = brief.write_brief(git_repo_with_many_commits, out, max_commits=5)
    assert result["commits_substantive"] == 40 and result["patches_written"] == 0
    assert result["patches_empty"] == 5
    assert _diffs(out) == []
    assert len(_rows(out)) == 40


def test_a_commit_of_withheld_paths_only_gets_no_patch(tmp_path, git_repo_with_credential_history):
    result = brief.write_brief(git_repo_with_credential_history, tmp_path / "brief")
    assert result["patches_empty"] == 3


def test_git_metadata_without_a_diff_is_written_as_it_came(
    tmp_path, monkeypatch, git_repo_with_credential_history
):
    # Only a blank answer is passed over.
    real = brief.run_git

    def with_header_on_filtered_commits(repo, *args, **kwargs):
        answer = real(repo, *args, **kwargs)
        if args[0] == "show" and not answer:
            return "2026-09-26\nwithheld files\n\n"
        return answer

    monkeypatch.setattr(brief, "run_git", with_header_on_filtered_commits)
    out = tmp_path / "brief"
    result = brief.write_brief(git_repo_with_credential_history, out)
    assert result["patches_written"] == 7 and result["patches_empty"] == 0
    diffs = [out / brief.DIFF_SUBDIR / name for name in _diffs(out)]
    headers = [path for path in diffs if "diff --git" not in path.read_text()]
    assert len(headers) == 3
    expected = "2026-09-26\nwithheld files\n\n" + brief._WITHHELD_NOTE
    assert all(path.read_text() == expected for path in headers)


def test_a_whitespace_answer_is_blank(tmp_path, monkeypatch, git_repo_with_many_commits):
    real = brief.run_git

    def _blank_show(repo, *args, **kwargs):
        return " \n\t\n" if args[0] == "show" else real(repo, *args, **kwargs)

    monkeypatch.setattr(brief, "run_git", _blank_show)
    result = brief.write_brief(git_repo_with_many_commits, tmp_path / "brief", max_commits=5)
    assert result["patches_written"] == 0 and result["patches_empty"] == 5


def test_a_commit_that_changes_no_file_is_not_a_substantive_one(tmp_path, repo_builder):
    repo = repo_builder({}, commits=[{"msg": "one", "files": work(1)}, {"msg": "nothing changed"}])
    result = brief.write_brief(repo, tmp_path / "brief")
    assert result["commits_listed"] == 2 and result["commits_substantive"] == 1
    assert result["patches_written"] == 1 and result["patches_empty"] == 0


def test_a_withheld_file_with_an_awkward_name_is_still_empty_by_design(tmp_path, repo_builder):
    text = "K = 1\n" * 12
    keys = {"msg": "keys", "files": {"clés privées/.env": text, "clés privées/b/.env": text}}
    repo = repo_builder({}, commits=[{"msg": "one", "files": work(1)}, keys])
    result = brief.write_brief(repo, tmp_path / "brief")
    assert result["patches_empty"] == 1


# --- every git call has the same long ceiling, whatever the run has left ------------------------


def test_every_git_call_in_the_brief_is_given_the_same_ceiling(
    tmp_path, monkeypatch, git_repo_with_many_commits
):
    allowed = []
    real = brief.run_git

    def _watched(repo, *args, **kwargs):
        allowed.append(kwargs.get("timeout"))
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(brief, "run_git", _watched)
    result = brief.write_brief(git_repo_with_many_commits, tmp_path / "brief", max_commits=5)
    assert len(allowed) == 3 + 5 and set(allowed) == {brief.GIT_TIMEOUT} == {900}
    assert result["patches_written"] == 5 and "stopped_early" not in result


def test_the_brief_takes_no_allowance():
    import inspect

    assert "remaining" not in inspect.signature(brief.write_brief).parameters
