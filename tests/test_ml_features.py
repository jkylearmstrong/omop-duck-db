"""Tests for the set-based ML feature extractors (sparse concept matrix, SARD visit tensors).

The fixture is small enough to verify by hand. Each scenario targets a specific failure mode:
temporal leakage, concept 0 colliding with padding, row misalignment with the parquet, patients with
several index rows, vocabulary reuse, and visit grouping/truncation.
"""

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

pytest.importorskip("scipy")

from omop_etl import (  # noqa: E402
    as_omop_learn_batch,
    extract_sard_visit_tensors,
    extract_sparse_concept_matrix,
)
from omop_etl.ml_features import PAD_TOKEN_IDX, UNIX_REFERENCE_DATE  # noqa: E402


def _cols(res):
    """concept token -> column index"""
    return dict(zip(res["concepts"]["token"], res["concepts"]["column_index"]))


def _dense(res):
    return res["X"].toarray()


# ---------------------------------------------------------------- sparse matrix
def test_sparse_counts_window_and_alignment(cdm_and_cohort):
    con, path = cdm_and_cohort
    res = extract_sparse_concept_matrix(con, path)
    X, cols = _dense(res), _cols(res)

    assert X.shape == (4, len(cols))
    # Row alignment follows parquet order [person2, person1, person3, person1(later)]
    assert list(res["cohort"]["person_id"]) == [2, 1, 3, 1]
    assert list(res["y"]) == [1, 0, 0, 1]

    t2d = "201 - condition - Type 2 diabetes"
    met = "301 - drug - Metformin"
    # person 1, 2021-06-01: 201 twice (5/20, 5/25); metformin once. 202 is ON index -> out; 203 is after -> out;
    # 204 is >365d old -> out; concept 0 dropped; procedures not requested by default.
    assert X[1, cols[t2d]] == 2
    assert X[1, cols[met]] == 1
    # 202/203 are in the vocabulary only because person 1's *later* index row (9/1) legitimately sees them;
    # for the 6/1 row the index-day (202) and post-index (203) events must be absent.
    assert X[1, cols["202 - condition"]] == 0, "index-date event leaked into the default window"
    assert X[1, cols["203 - condition"]] == 0, "event after the index date leaked into the window"
    assert "204 - condition" not in cols, ">365d-old event should not be a feature for anyone"
    assert X[1].sum() == 3
    # person 2: 201 once, metformin once, 205 once (visit-less event still counts)
    assert X[0, cols[t2d]] == 1 and X[0, cols[met]] == 1 and X[0, cols["205 - condition"]] == 1
    # person 3 has no events: kept as an all-zero row (alignment preserved)
    assert X[2].sum() == 0
    # person 1's later index row sees 203 (6/10) plus the earlier history, still excluding >365d
    assert X[3, cols["203 - condition"]] == 1 and X[3, cols[t2d]] == 2 and X[3, cols["202 - condition"]] == 1


def test_concept_zero_never_a_feature(cdm_and_cohort):
    con, path = cdm_and_cohort
    res = extract_sparse_concept_matrix(con, path)
    assert 0 not in set(res["concepts"]["concept_id"])
    sard = extract_sard_visit_tensors(con, path)
    assert 0 not in set(sard["concepts"]["concept_id"])


def test_include_index_date_and_lookback_bounds(cdm_and_cohort):
    con, path = cdm_and_cohort
    res = extract_sparse_concept_matrix(con, path, include_index_date=True)
    cols = _cols(res)
    assert _dense(res)[1, cols["202 - condition"]] == 1, "index-date event should be included on request"

    short = extract_sparse_concept_matrix(con, path, lookback_days=7)  # 5/25 .. 5/31 for person 1
    cols = _cols(short)
    row1 = _dense(short)[1]
    assert row1[cols["201 - condition - Type 2 diabetes"]] == 1  # only the 5/25 record
    assert "205 - condition" not in cols  # person 2's 5/10 event is outside 7 days

    unbounded = extract_sparse_concept_matrix(con, path, lookback_days=None)
    assert "204 - condition" in _cols(unbounded)  # 2020-01-01 is only visible with no lower bound


def test_binary_and_domains(cdm_and_cohort):
    con, path = cdm_and_cohort
    res = extract_sparse_concept_matrix(con, path, value="binary", domains=("condition", "drug", "procedure"))
    cols = _cols(res)
    assert _dense(res)[1, cols["201 - condition - Type 2 diabetes"]] == 1  # binary, not 2
    assert _dense(res)[1, cols["401 - procedure"]] == 1
    only_drugs = extract_sparse_concept_matrix(con, path, domains=("drug",))
    assert set(only_drugs["concepts"]["domain"]) == {"drug"}


def test_min_patient_freq_counts_patients_not_rows(cdm_and_cohort):
    con, path = cdm_and_cohort
    # 201 and 301 occur for persons 1 and 2 (2 patients); person 1 has two index rows but counts once.
    res = extract_sparse_concept_matrix(con, path, min_patient_freq=2)
    assert set(res["concepts"]["concept_id"]) == {201, 301}
    assert set(res["concepts"]["n_patients"]) == {2}
    assert extract_sparse_concept_matrix(con, path, min_patient_freq=3)["X"].shape[1] == 0


def test_tokenizer_reuse_fixes_columns(cdm_and_cohort, tmp_path):
    con, path = cdm_and_cohort
    train = extract_sparse_concept_matrix(con, path, min_patient_freq=2)
    # "test" cohort: person 1 only
    test_pq = tmp_path / "test.parquet"
    duckdb.connect().register("t", pd.DataFrame({"subject_id": [1], "cohort_start_date": pd.to_datetime(["2021-06-01"])})
                              ).execute(f"COPY (SELECT * FROM t) TO '{test_pq.as_posix()}' (FORMAT PARQUET)")
    test = extract_sparse_concept_matrix(con, test_pq, tokenizer=train["tokenizer"])
    assert test["X"].shape[1] == train["X"].shape[1]
    assert list(test["concepts"]["token"]) == list(train["concepts"]["token"])
    assert test["y"] is None  # no outcome column in this parquet
    # concepts outside the train vocabulary (e.g. 203, 205) are dropped, not appended
    assert _dense(test).sum() == 3


def test_explicit_columns_and_errors(cdm_and_cohort, tmp_path):
    con, path = cdm_and_cohort
    res = extract_sparse_concept_matrix(con, path, person_col="subject_id", index_date_col="cohort_start_date",
                                        outcome_col="outcome_flag")
    assert list(res["y"]) == [1, 0, 0, 1]
    with pytest.raises(ValueError, match="Available columns"):
        extract_sparse_concept_matrix(con, path, person_col="nope")
    with pytest.raises(ValueError, match="domains"):
        extract_sparse_concept_matrix(con, path, domains=("measurement",))
    bad = tmp_path / "bad.parquet"
    duckdb.connect().register("b", pd.DataFrame({"subject_id": [1, None], "cohort_start_date": pd.to_datetime(["2021-06-01"] * 2)})
                              ).execute(f"COPY (SELECT * FROM b) TO '{bad.as_posix()}' (FORMAT PARQUET)")
    with pytest.raises(ValueError, match="missing"):
        extract_sparse_concept_matrix(con, bad)


def test_works_on_read_only_connection_with_central_vocab(tmp_path, cdm_and_cohort):
    """Mirrors the documented usage: read-only site DB + separately attached central_vocabulary."""
    con, path = cdm_and_cohort
    site, vocab = tmp_path / "site.duckdb", tmp_path / "central_vocabulary.duckdb"
    con.execute(f"ATTACH '{site.as_posix()}' AS s"); con.execute(f"ATTACH '{vocab.as_posix()}' AS v")
    for t in ["person", "visit_occurrence", "condition_occurrence", "drug_exposure", "procedure_occurrence"]:
        con.execute(f"CREATE TABLE s.{t} AS SELECT * FROM {t}")
    con.execute("CREATE TABLE v.concept AS SELECT * FROM concept")
    con.execute("DETACH s"); con.execute("DETACH v")

    ro = duckdb.connect(str(site), read_only=True)
    ro.execute(f"ATTACH '{vocab.as_posix()}' AS central_vocab (READ_ONLY)")  # no `concept` in site DB
    res = extract_sparse_concept_matrix(ro, path, min_patient_freq=2)
    assert "201 - condition - Type 2 diabetes" in set(res["concepts"]["token"])  # name came from central_vocab
    assert res["concepts"].set_index("concept_id").loc[201, "vocabulary_id"] == "SNOMED"


# ---------------------------------------------------------------- SARD tensors
def test_sard_tensor_layout_and_visit_grouping(cdm_and_cohort):
    con, path = cdm_and_cohort
    res = extract_sard_visit_tensors(con, path)
    T, tok = res["concept_tensor"], res["tokenizer"]
    assert tok.pad_token_idx == PAD_TOKEN_IDX == 3
    N, V, L = T.shape
    assert N == 4 and list(res["n_visits"]) == [2, 2, 0, 3]  # person2: visit 21 + date-grouped pseudo-visit

    # Row 1 (person 1, 2021-06-01): visits 11 {201} (5/20), 12 {201, 301} (5/25) -- oldest first.
    # (concept 0, procedure 401 [not requested], 204 [too old], 202 [index day] never appear.)
    ids = lambda r, v: [tok.concept_list[i] for i in T[r, v] if i != PAD_TOKEN_IDX]
    assert ids(1, 0) == ["201 - condition - Type 2 diabetes"]
    assert sorted(ids(1, 1)) == sorted(["201 - condition - Type 2 diabetes", "301 - drug - Metformin"])
    assert list(res["visit_lengths"][1]) [:2] == [1, 2]
    # Row 0 (person 2): pseudo-visit (5/10, no visit id) precedes visit 21 (5/30)
    assert ids(0, 0) == ["205 - condition"] and len(ids(0, 1)) == 2
    # Padding: unused slots hold PAD; all-empty row is all PAD with n_visits 0
    assert (T[2] == PAD_TOKEN_IDX).all() and (res["times"][2] == -1).all()
    assert (T[1, 2:] == PAD_TOKEN_IDX).all()

    # times: days since 1900-01-01; days_before_index measured to the row's own index date
    d = lambda s: (pd.Timestamp(s) - UNIX_REFERENCE_DATE).days
    assert list(res["times"][1][:2]) == [d("2021-05-20"), d("2021-05-25")]
    assert list(res["visit_days_before_index"][1][:2]) == [12, 7]
    assert (res["times"][1][2:] == -1).all()
    # Row 3 sees person 1's later visit 13 (6/10) as its most recent visit
    assert res["times"][3][res["n_visits"][3] - 1] == d("2021-06-10")


def test_sard_truncation_keeps_most_recent_visits(cdm_and_cohort):
    con, path = cdm_and_cohort
    res = extract_sard_visit_tensors(con, path, max_nvisits=1, max_visit_len=1)
    assert res["concept_tensor"].shape == (4, 1, 1)
    d = lambda s: (pd.Timestamp(s) - UNIX_REFERENCE_DATE).days
    # person 1 / row 1: most recent visit is 5/25 (visit 12), not 5/20
    assert res["times"][1, 0] == d("2021-05-25")
    # row 3: most recent visit is 6/10
    assert res["times"][3, 0] == d("2021-06-10")
    assert list(res["n_visits"]) == [1, 1, 0, 1]
    # explicit caps larger than the data give an exactly fixed shape (train/test consistency)
    big = extract_sard_visit_tensors(con, path, max_nvisits=7, max_visit_len=5)
    assert big["concept_tensor"].shape == (4, 7, 5)


def test_sard_matches_sparse_vocabulary_and_counts(cdm_and_cohort):
    """Both extractors must agree on vocabulary and on which concepts fall in each window."""
    con, path = cdm_and_cohort
    sp = extract_sparse_concept_matrix(con, path, domains=("condition", "drug", "procedure"))
    sd = extract_sard_visit_tensors(con, path, domains=("condition", "drug", "procedure"))
    assert list(sp["concepts"]["token"]) == list(sd["concepts"]["token"])
    # distinct (row, concept) pairs seen in the tensor == nonzero cells of the sparse matrix
    T = sd["concept_tensor"]
    for r in range(T.shape[0]):
        in_tensor = {int(t) - 5 for t in T[r].ravel() if t != PAD_TOKEN_IDX}
        assert in_tensor == set(sp["X"][r].nonzero()[1].tolist())


def test_sard_min_freq_and_omop_learn_batch(cdm_and_cohort):
    con, path = cdm_and_cohort
    res = extract_sard_visit_tensors(con, path, min_patient_freq=2)
    assert set(res["concepts"]["concept_id"]) == {201, 301}
    batch = as_omop_learn_batch(res)
    assert set(batch) == {"visits", "times", "lengths", "y"}
    assert batch["visits"].shape[0] == batch["lengths"].shape[0] == 4
    # an all-filtered cohort degrades to well-formed, minimal padding rather than crashing
    empty = extract_sard_visit_tensors(con, path, min_patient_freq=99)
    assert empty["concept_tensor"].shape == (4, 1, 1) and (empty["concept_tensor"] == PAD_TOKEN_IDX).all()


# ---------------------------------------------------------------- R <-> Python parity
def _rscript_cmd():
    """Rscript invocation that can load DBI/duckdb/Matrix, or None. Plain first (respects renv); falls back to
    --no-init-file (global library) for checkouts whose renv library has not been restored."""
    import os
    import shutil
    import subprocess

    exe = shutil.which("Rscript")
    if exe is None:
        return None
    for extra in ([], ["--no-init-file"]):
        probe = subprocess.run([exe, *extra, "-e", "library(DBI);library(duckdb);library(Matrix)"],
                               capture_output=True, text=True, shell=(os.name == "nt"))
        if probe.returncode == 0:
            return [exe, *extra]
    return None


def test_r_and_python_sparse_matrices_are_identical(cdm_and_cohort, tmp_path):
    """Same CDM + cohort through Python extract_sparse_concept_matrix and the standalone R file
    (R/ml_features.R, sourced without installing the package): identical vocabulary, column order, cells."""
    import os
    import subprocess

    rscript = _rscript_cmd()
    if rscript is None:
        pytest.skip("Rscript with DBI, duckdb and Matrix not available")
    con, parquet = cdm_and_cohort
    repo_root = Path(__file__).resolve().parent.parent

    db = tmp_path / "cdm.duckdb"
    con.execute(f"ATTACH '{db.as_posix()}' AS persisted")
    for t in ["person", "visit_occurrence", "condition_occurrence", "drug_exposure", "procedure_occurrence", "concept"]:
        con.execute(f"CREATE TABLE persisted.{t} AS SELECT * FROM main.{t}")
    con.execute("DETACH persisted")

    out = tmp_path / "r_out"
    out.mkdir()
    script = tmp_path / "r_side.R"
    script.write_text("""
args <- commandArgs(trailingOnly = TRUE)
suppressMessages({library(DBI); library(duckdb); library(Matrix)})
source(file.path(args[1], "R", "ml_features.R"))
con <- dbConnect(duckdb::duckdb(), args[2], read_only = TRUE)
res <- extract_sparse_concept_matrix(con, args[3], domains = c("condition", "drug", "procedure"))
trip <- Matrix::summary(res$X)
write.csv(data.frame(i = trip$i - 1L, j = trip$j - 1L, x = trip$x), file.path(args[4], "triplets.csv"), row.names = FALSE)
writeLines(res$concepts$token, file.path(args[4], "tokens.txt"), useBytes = TRUE)
write.csv(data.frame(shape = dim(res$X)), file.path(args[4], "shape.csv"), row.names = FALSE)
dbDisconnect(con, shutdown = TRUE)
""")
    proc = subprocess.run([*rscript, str(script), str(repo_root), str(db), str(parquet), str(out)],
                          capture_output=True, text=True, shell=(os.name == "nt"))
    assert proc.returncode == 0, proc.stderr

    ro = duckdb.connect(str(db), read_only=True)
    py = extract_sparse_concept_matrix(ro, parquet, domains=("condition", "drug", "procedure"))
    ro.close()

    r_tokens = (out / "tokens.txt").read_text(encoding="utf-8").splitlines()
    assert r_tokens == list(py["concepts"]["token"])                      # same vocabulary, same order

    r_shape = pd.read_csv(out / "shape.csv")["shape"].tolist()
    assert r_shape == list(py["X"].shape)

    r_trip = pd.read_csv(out / "triplets.csv")
    coo = py["X"].tocoo()
    py_cells = sorted(zip(coo.row.tolist(), coo.col.tolist(), coo.data.astype(float).tolist()))
    r_cells = sorted(zip(r_trip["i"].tolist(), r_trip["j"].tolist(), r_trip["x"].astype(float).tolist()))
    assert r_cells == py_cells                                            # identical cells
