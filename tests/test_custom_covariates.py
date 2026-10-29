"""Custom covariates written as measurement rows so FeatureExtraction can read them."""
import datetime

import duckdb
import pandas as pd
import pytest

from omop_etl.build_omop_cdm import build_schema
from omop_etl.custom_covariates import write_custom_covariates

CONCEPTS = {"frailty": (2000200001, "Frailty score"), "frailty_alt": (2000200002, "Frailty score, alternative definition")}


def _con():
    c = duckdb.connect()
    build_schema(c)
    return c


def _values():
    return pd.DataFrame({"person_id": [1, 1, 2, 3], "date": [datetime.date(2020, 1, 1)] * 4,
                         "feature": ["frailty", "frailty_alt", "frailty", "frailty"], "value": [2.0, 3.0, None, 5.0]})


def test_rows_units_and_concepts():
    c = _con()
    assert write_custom_covariates(c, _values(), CONCEPTS) == 3            # the missing value writes nothing
    rows = c.execute("SELECT person_id, measurement_concept_id, value_as_number, unit_concept_id, measurement_source_value "
                     "FROM measurement ORDER BY 1, 2").fetchall()
    assert rows == [(1, 2000200001, 2.0, 0, "omop_etl.custom_covariates"), (1, 2000200002, 3.0, 0, "omop_etl.custom_covariates"),
                    (3, 2000200001, 5.0, 0, "omop_etl.custom_covariates")]
    assert c.execute("SELECT count(*) FROM concept WHERE concept_id IN (2000200001, 2000200002) AND domain_id = 'Measurement'").fetchone()[0] == 2


def test_rerun_replaces_and_does_not_touch_other_measurements():
    c = _con()
    c.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id) "
              "VALUES (1, 1, 3004410, DATE '2020-01-01', 0)")
    write_custom_covariates(c, _values(), CONCEPTS)
    write_custom_covariates(c, _values(), CONCEPTS)
    assert c.execute("SELECT count(*) FROM measurement").fetchone()[0] == 4
    assert c.execute("SELECT count(*) FROM measurement WHERE measurement_concept_id = 3004410").fetchone()[0] == 1
    assert c.execute("SELECT count(DISTINCT measurement_id) FROM measurement").fetchone()[0] == 4


def test_rejects_vocabulary_range_ids_and_unknown_features():
    c = _con()
    with pytest.raises(ValueError, match="2,000,000,000|2000000000"):
        write_custom_covariates(c, _values(), {"frailty": (3004410, "x"), "frailty_alt": (2000200002, "y")})
    with pytest.raises(ValueError, match="no entry"):
        write_custom_covariates(c, _values().assign(feature="other"), CONCEPTS)
    with pytest.raises(ValueError, match="missing column"):
        write_custom_covariates(c, _values().drop(columns="date"), CONCEPTS)


def test_wide_features_get_stable_named_concepts_beside_the_standard_one():
    from omop_etl.custom_covariates import write_custom_features
    c = _con()
    wide = pd.DataFrame({"person_id": [1, 2], "date": [datetime.date(2020, 1, 1)] * 2, "charlson_site": [3.0, 1.0], "elix_site": [4.0, None]})
    ids = write_custom_features(c, wide, labels={"charlson_site": "Charlson index, site definition"})
    assert ids["charlson_site"] == (2001000000, "Charlson index, site definition") and ids["elix_site"][0] == 2001000001
    again = write_custom_features(c, wide)                              # re-run: same ids, rows replaced, not duplicated
    assert {k: v[0] for k, v in again.items()} == {k: v[0] for k, v in ids.items()}
    assert c.execute("SELECT count(*) FROM measurement").fetchone()[0] == 3
    assert c.execute("SELECT concept_name FROM concept WHERE concept_id = 2001000000").fetchone()[0] == "Charlson index, site definition"


def _concept(c, cid, code, vocab="omop-duck-db", name="x"):
    c.execute("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, "
              "valid_start_date, valid_end_date) VALUES (?, ?, 'Measurement', ?, 'Custom covariate', 'S', ?, DATE '1970-01-01', DATE '2099-12-31')",
              [cid, name, vocab, code])


def test_collision_with_another_vocabulary_concept_is_refused():
    from omop_etl.custom_covariates import write_custom_covariates
    c = _con()
    _concept(c, 2000200001, "other", vocab="SNOMED")
    with pytest.raises(ValueError, match="already used by other"):
        write_custom_covariates(c, _values(), CONCEPTS)
    assert c.execute("SELECT count(*) FROM measurement").fetchone()[0] == 0
    with pytest.raises(ValueError, match="share one concept id"):
        write_custom_covariates(_con(), _values(), {"frailty": (2000200001, "a"), "frailty_alt": (2000200001, "b")})


def test_registry_pins_ids_across_sites(tmp_path):
    from omop_etl.custom_covariates import (check_custom_concepts, export_custom_concept_registry, register_custom_concepts,
                                            write_custom_features)
    a, b = _con(), _con()
    feats = pd.DataFrame({"person_id": [1], "date": [datetime.date(2020, 1, 1)], "charlson_site": [3.0]})
    write_custom_features(a, feats)
    path = tmp_path / "registry.json"
    reg = export_custom_concept_registry(a, path)
    assert reg["charlson_site"]["concept_id"] == 2001000000
    # site b already numbered another feature first, so its own ids would differ from site a
    write_custom_features(b, feats.rename(columns={"charlson_site": "elixhauser"}))
    write_custom_features(b, feats)
    assert {p["kind"] for p in check_custom_concepts(b, path)} == {"name_different_id", "id_different_name"}
    # a fresh site that uses the shared registry gets the same id even though it registers other features first
    c = _con()
    got = register_custom_concepts(c, ["frailty", "charlson_site"], registry=path)
    assert got["charlson_site"][0] == 2001000000 and got["frailty"][0] > 2001000000
    assert check_custom_concepts(c, path) == []
    with pytest.raises(ValueError, match="custom concept conflicts"):
        check_custom_concepts(b, path, raise_on_conflict=True)


def test_registry_conflicts_with_local_ids_raise():
    from omop_etl.custom_covariates import register_custom_concepts
    c = _con()
    _concept(c, 2001000005, "frailty")
    with pytest.raises(ValueError, match="in this database but"):
        register_custom_concepts(c, ["frailty"], registry={"frailty": 2001000007})
    _concept(c, 2001000009, "other")
    with pytest.raises(ValueError, match="already used here by other"):
        register_custom_concepts(c, ["charlson"], registry={"charlson": 2001000009})
    with pytest.raises(ValueError, match="under 2000000000"):
        register_custom_concepts(c, ["charlson"], registry={"charlson": 1503297})
