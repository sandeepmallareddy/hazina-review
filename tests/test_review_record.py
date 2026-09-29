"""Each repository's files carry a record id: an HMAC over the four files as they were written,
so that a file changed after the run can be told apart from one the run wrote.

The vector in `tests/fixtures/record_vector.json` is shared with the receiving side, which
computes the same id independently; both must reproduce it byte for byte.
"""

import json
import re
import zipfile
from pathlib import Path

import pytest

from hazina_review import emit, record, run
from hazina_scan import orchestrator
from hazina_scan.schema import EmissionRefused
from tests.test_review_run import FOUR, _review, ready  # noqa: F401 -- the fixture is used

VECTOR = json.loads(
    (Path(__file__).parent / "fixtures" / "record_vector.json").read_text(encoding="utf-8")
)
KEY = VECTOR["key"]
LINE = re.compile(rb'\A\{\n  "record_id": "[0-9a-f]{32}",\n')


def _files(**changes) -> dict[str, bytes]:
    files = {name: text.encode() for name, text in VECTOR["files"].items()}
    return {**files, **{name.replace("__", "."): value for name, value in changes.items()}}


def _on_disk(folder: Path) -> dict[str, bytes]:
    return {name: (folder / name).read_bytes() for name in FOUR}


# --- the shared vector ---------------------------------------------------------------------


def test_the_shared_vector_gives_the_expected_record_id():
    assert record.compute(_files(), KEY) == VECTOR["record_id"]


def test_the_record_id_is_computed_with_its_own_value_blanked():
    other = VECTOR["files"]["codebase_repos.json"].replace(VECTOR["record_id"], "f" * 32)
    assert record.compute(_files(codebase_repos__json=other.encode()), KEY) == VECTOR["record_id"]


def test_the_shared_vector_verifies_and_strips():
    assert record.verify(_files(), [KEY]) is True
    assert record.verify(_files(), KEY) is True
    assert record.verify(_files(), ["another key", KEY]) is True
    assert record.strip(_files()["codebase_repos.json"]) == VECTOR[
        "stripped_codebase_repos_json"
    ].encode("utf-8")


def test_a_key_given_as_bytes_or_text_is_the_same_key():
    assert record.compute(_files(), KEY.encode()) == record.compute(_files(), KEY)


@pytest.mark.parametrize(
    "changes",
    [
        {"measurement__json": b'{\n  "measurer_version": "y"\n}\n'},
        {"codebase_repo_mining__json": b'{\n  "total_candidates": 4\n}\n'},
        {"codebase_repos__csv": b"a,b\n1,3\n"},
        {"codebase_repos__csv": b"a,b\n1,2\n\n"},
        {
            "codebase_repos__json": VECTOR["files"]["codebase_repos.json"]
            .replace("1234", "1235")
            .encode()
        },
        {
            "codebase_repos__json": VECTOR["files"]["codebase_repos.json"]
            .replace(VECTOR["record_id"], "0" * 32)
            .encode()
        },
    ],
)
def test_any_change_to_any_file_fails_verification(changes):
    assert record.verify(_files(**changes), [KEY]) is False


def test_the_wrong_key_fails_verification():
    assert record.verify(_files(), ["test-key-not-for-production!"]) is False
    assert record.verify(_files(), []) is False


def test_a_missing_or_extra_file_fails_verification_and_is_refused_by_compute():
    files = _files()
    missing = {name: data for name, data in files.items() if name != "measurement.json"}
    extra = {**files, "notes.txt": b"hello\n"}
    for wrong in (missing, extra):
        assert record.verify(wrong, [KEY]) is False
        with pytest.raises(ValueError):
            record.compute(wrong, KEY)


def test_a_row_without_its_record_id_line_fails_and_cannot_be_stripped():
    stripped = VECTOR["stripped_codebase_repos_json"].encode()
    assert record.verify(_files(codebase_repos__json=stripped), [KEY]) is False
    with pytest.raises(ValueError):
        record.strip(stripped)
    with pytest.raises(ValueError):
        record.compute(_files(codebase_repos__json=stripped), KEY)


def test_a_record_id_anywhere_but_the_first_line_is_not_the_record_id():
    moved = b'{\n  "loc": 1234,\n  "record_id": "' + VECTOR["record_id"].encode() + b'"\n}\n'
    assert record.verify(_files(codebase_repos__json=moved), [KEY]) is False


# --- the embedded key ----------------------------------------------------------------------


def test_the_embedded_key_decodes_to_32_bytes():
    key = record.production_key()
    assert isinstance(key, bytes) and len(key) == 32


def test_the_module_does_not_hold_the_key_in_plain_hex():
    source = Path(record.__file__).read_text(encoding="utf-8")
    assert not re.search(r"[0-9a-fA-F]{64}", source)
    held = Path.home() / ".hazina-record-key"
    if not held.is_file():
        pytest.skip("no key file on this machine to compare against")
    hex_key = held.read_text(encoding="ascii").strip().lower()
    assert record.production_key() == bytes.fromhex(hex_key)
    for spelling in (hex_key, hex_key.upper()):
        assert spelling not in source
        assert spelling[:16] not in source and spelling[-16:] not in source


# --- the emission boundary -----------------------------------------------------------------


def test_the_emission_boundary_accepts_a_record_id_and_nothing_else_in_its_place():
    assert emit.record_id(VECTOR["record_id"]) == VECTOR["record_id"]
    for wrong in (None, "", "0" * 31, "A" * 32, "g" * 32, "0" * 33, 12):
        with pytest.raises(EmissionRefused):
            emit.record_id(wrong)


# --- a run ---------------------------------------------------------------------------------


def test_a_runs_row_starts_with_its_record_id_and_all_four_files_verify(
    ready,  # noqa: F811
    tmp_path,
    py_repo,
):
    out = tmp_path / "o"
    _review(py_repo, out)
    folder = out / py_repo.name
    files = _on_disk(folder)
    head = LINE.match(files["codebase_repos.json"])
    assert head is not None
    written = json.loads(files["codebase_repos.json"])
    assert next(iter(written)) == "record_id" and written["record_id"] != "0" * 32
    assert record.verify(files, [record.production_key()]) is True
    assert record.compute(files, record.production_key()) == written["record_id"]
    # the id is the row's alone: neither the spreadsheet row nor the measurement carries it
    assert b"record_id" not in files["codebase_repos.csv"]
    assert b"record_id" not in files["measurement.json"]


def test_stripping_the_line_gives_exactly_what_is_written_without_a_record_id(
    ready,  # noqa: F811
    tmp_path,
    py_repo,
):
    out = tmp_path / "o"
    _review(py_repo, out)
    folder = out / py_repo.name
    row = json.loads((folder / "codebase_repos.json").read_bytes())
    row.pop("record_id")
    plain = tmp_path / "plain"
    orchestrator.write_outputs(plain / "x", row, {}, out_root=plain)
    stripped = record.strip((folder / "codebase_repos.json").read_bytes())
    assert stripped == (plain / "x" / "codebase_repos.json").read_bytes()
    assert (folder / "codebase_repos.csv").read_bytes() == (
        plain / "x" / "codebase_repos.csv"
    ).read_bytes()


def test_one_number_changed_after_the_run_fails_verification(
    ready,  # noqa: F811
    tmp_path,
    py_repo,
):
    out = tmp_path / "o"
    _review(py_repo, out)
    files = _on_disk(out / py_repo.name)
    measurement = files["measurement.json"]
    changed = re.sub(
        rb'("n_complex_logic": )(\d+)', lambda m: m[1] + b"9" + m[2], measurement, count=1
    )
    assert changed != measurement
    assert record.verify({**files, "measurement.json": changed}, [record.production_key()]) is (
        False
    )


def test_what_is_zipped_verifies(ready, tmp_path, py_repo):  # noqa: F811
    out = tmp_path / "o"
    _review(py_repo, out)
    with zipfile.ZipFile(out.parent / run.ZIP_NAME) as archive:
        files = {name: archive.read(f"{py_repo.name}/{name}") for name in FOUR}
    assert record.verify(files, [record.production_key()]) is True


def test_a_run_with_a_failed_lane_still_carries_a_record_id(
    ready,  # noqa: F811
    monkeypatch,
    tmp_path,
    py_repo,
):
    from hazina_review.lanes import census
    from hazina_review.providers.ask import Turn

    monkeypatch.setattr(census, "ask", lambda *a, **k: Turn("timeout"))
    out = tmp_path / "o"
    _review(py_repo, out)
    assert record.verify(_on_disk(out / py_repo.name), [record.production_key()]) is True
