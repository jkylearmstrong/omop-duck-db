"""Modular clinical domain profiles and ``Table1Generator``.

A *profile* bundles labs/vitals (LOINC), medication classes (ATC codes resolved through ``concept`` +
``concept_ancestor``) and comorbidities (ICD-10 code prefixes on ``condition_source_value``) for a clinical
domain. Profiles are plain dicts - copy and edit them, or load one from YAML (``load_profile``).

.. note:: The bundled LOINC / ATC / ICD-10 lists are a starting point for review by a clinician of that
   domain, not a validated specification. Profile labs with no data at a site simply come out 100% missing.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping, Sequence

import duckdb
import pandas as pd

from omop_etl.measurement_filter import filter_bounds, harmonize_measurement_units, resolve_bounds
from omop_etl.table1 import generate_table1

__all__ = ["DOMAIN_PROFILES", "load_profile", "Table1Generator"]

DOMAIN_PROFILES: dict[str, dict[str, Any]] = {
    "heart_failure": {
        "labs": {
            "troponin_i": ["10839-9", "42757-5", "89579-7"], "troponin_t": ["6598-7", "67151-1"],
            "nt_probnp": ["33762-6", "83107-3"], "bnp": ["30934-4", "42637-9"],
            "cholesterol_total": ["2093-3"], "ldl": ["13457-7", "2089-1"], "hdl": ["2085-9"],
            "triglycerides": ["2571-8"], "potassium": ["2823-3", "6298-4"], "lvef": ["10230-1"],
        },
        "bounds": {"potassium": [1.0, 10.0], "lvef": [5.0, 90.0], "troponin_i": [0.0, 50000.0],
                   "troponin_t": [0.0, 50000.0]},
        "medications": {
            "beta_blockers": {"atc_codes": ["C07"]}, "ace_inhibitors": {"atc_codes": ["C09A", "C09B"]},
            "arbs": {"atc_codes": ["C09C", "C09D"]}, "arni": {"atc_codes": ["C09DX04"]},
            "mras": {"atc_codes": ["C03DA"]}, "sglt2i": {"atc_codes": ["A10BK"]},
            "loop_diuretics": {"atc_codes": ["C03C"]}, "statins": {"atc_codes": ["C10AA"]},
        },
        "conditions": {"ischemic_heart_disease": ["I20", "I21", "I22", "I24", "I25"],
                       "atrial_fibrillation": ["I48"], "hypertension": ["I10", "I11", "I12", "I13", "I15"],
                       "heart_failure": ["I50"]},
    },
    "nephrology": {
        "labs": {
            "creatinine": ["2160-0", "38483-4"], "egfr": ["33914-3", "48642-3", "48643-1", "62238-1", "88293-6", "88294-4"],
            "bun": ["3094-0", "6299-2"], "phosphorus": ["2777-1"], "calcium": ["17861-6"],
            "uacr": ["9318-7", "14959-1"], "urine_protein_24h": ["21482-5"],
        },
        "bounds": {"creatinine": [0.1, 30.0], "egfr": [0.0, 250.0], "phosphorus": [0.5, 20.0]},
        "medications": {
            "raas_inhibitors": {"atc_codes": ["C09"]}, "phosphate_binders": {"atc_codes": ["V03AE"]},
            "esas": {"atc_codes": ["B03XA"]}, "vitamin_d_analogs": {"atc_codes": ["A11CC", "H05BX"]},
            "potassium_binders": {"atc_codes": ["V03AE01", "V03AE09"]},
        },
        "conditions": {"ckd_stage_1": ["N181"], "ckd_stage_2": ["N182"], "ckd_stage_3": ["N183"],
                       "ckd_stage_4": ["N184"], "ckd_stage_5": ["N185"], "esrd": ["N186"],
                       "dialysis_status": ["Z992", "Z491", "Z492"], "renal_osteodystrophy": ["N250"]},
    },
    "sepsis_icu": {
        "labs": {
            "ph": ["2744-1"], "po2": ["2703-7"], "pco2": ["2019-8"], "lactate": ["2524-7", "32693-4"],
            "platelets": ["777-3", "26515-7"], "bilirubin_total": ["1975-2"], "base_excess": ["11555-0"],
        },
        "bounds": {"ph": [6.5, 8.0], "lactate": [0.1, 30.0], "platelets": [1.0, 2000.0],
                   "bilirubin_total": [0.1, 60.0]},
        "medications": {
            "vasopressors": {"atc_codes": ["C01CA03", "C01CA24", "H01BA01"]},
            "inotropes": {"atc_codes": ["C01CA07"]}, "systemic_corticosteroids": {"atc_codes": ["H02AB"]},
            "broad_spectrum_antibiotics": {"atc_codes": ["J01DH", "J01CR05", "J01DE", "J01DD"]},
        },
        "conditions": {"sepsis": ["A40", "A41", "R652"], "septic_shock": ["R6521"],
                       "mechanical_ventilation": ["Z9911", "J9600", "J9601"]},
    },
    "oncology": {
        "labs": {
            "anc": ["751-8", "26499-4"], "platelets": ["777-3", "26515-7"], "hemoglobin": ["718-7", "59260-0"],
            "ldh": ["2532-0"], "cea": ["2039-6"], "psa": ["2857-1"], "ca_125": ["10334-1"], "ca_19_9": ["24108-3"],
        },
        "bounds": {"anc": [0.0, 200.0], "hemoglobin": [2.0, 25.0], "platelets": [1.0, 2000.0]},
        "medications": {
            "platinum_agents": {"atc_codes": ["L01XA"]}, "anthracyclines": {"atc_codes": ["L01DB"]},
            "taxanes": {"atc_codes": ["L01CD"]}, "checkpoint_inhibitors": {"atc_codes": ["L01FF"]},
            "gcsf": {"atc_codes": ["L03AA"]},
        },
        "conditions": {"solid_tumor": ["C0", "C1", "C2", "C3", "C4", "C5", "C6", "C7"],
                       "metastatic": ["C77", "C78", "C79", "C7B"],
                       "hematologic_malignancy": ["C81", "C82", "C83", "C84", "C85", "C88", "C90", "C91", "C92", "C93", "C94", "C95", "C96"],
                       "neutropenic_fever": ["D700", "D701", "D702", "D703", "D704", "D708", "D709"]},
    },
    "pulmonary": {
        "labs": {
            "fev1": ["20150-9"], "fvc": ["19870-5"], "fev1_fvc": ["19926-5"], "eosinophils": ["711-2", "26449-9"],
            "po2": ["2703-7"], "pco2": ["2019-8"], "spo2": ["59408-5", "2708-6"],
        },
        "bounds": {"spo2": [30.0, 100.0], "fev1_fvc": [0.0, 1.5]},
        "medications": {
            "ics": {"atc_codes": ["R03BA"]}, "laba": {"atc_codes": ["R03AC12", "R03AC13", "R03AC18"]},
            "lama": {"atc_codes": ["R03BB"]}, "saba": {"atc_codes": ["R03AC02", "R03AC03"]},
            "oral_corticosteroids": {"atc_codes": ["H02AB"]}, "biologics": {"atc_codes": ["R03DX"]},
        },
        "conditions": {"copd": ["J44"], "asthma": ["J45"], "supplemental_oxygen": ["Z9981"]},
    },
    "pharmacotherapy_safety": {
        "labs": {
            "vancomycin_trough": ["4092-3", "39797-6"], "digoxin": ["10535-3"], "lithium": ["14334-7"],
            "tacrolimus": ["11253-2"], "inr": ["6301-6", "34714-6"], "pt": ["5902-2"], "aptt": ["3173-2"],
        },
        "bounds": {"inr": [0.5, 20.0], "aptt": [10.0, 300.0]},
        "medications": {
            "doacs": {"atc_codes": ["B01AF", "B01AE07"]}, "heparins": {"atc_codes": ["B01AB"]},
            "warfarin": {"atc_codes": ["B01AA03"]}, "opioids": {"atc_codes": ["N02A"]},
            "sedatives": {"atc_codes": ["N05B", "N05C"]},
        },
        "conditions": {},
    },
}


def load_profile(source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """Load a profile from a dict or a YAML file (keys: ``labs``, ``bounds``, ``medications``, ``conditions``)."""
    if isinstance(source, Mapping):
        prof = copy.deepcopy(dict(source))
    else:
        import yaml
        prof = yaml.safe_load(Path(source).read_text(encoding="utf-8")) or {}
    unknown = set(prof) - {"labs", "bounds", "medications", "conditions"}
    if unknown:
        raise ValueError(f"Unknown profile keys {sorted(unknown)}.")
    return prof


class Table1Generator:
    """Build a cohort-level Table 1 from a DuckDB OMOP database, extended by domain profiles.

    >>> t1 = Table1Generator(db_path="omop_Temple.duckdb")
    >>> t1.add_domain_profile("heart_failure")
    >>> t1.add_custom_lab("troponin_t", loinc_codes=["6598-7", "42757-5"], bounds=[0.0, 50.0])
    >>> out = t1.generate(cohort_table="inpatient_hf_cohort", stratify_by="readmitted_30d")
    >>> out["table1"]

    The cohort table needs ``subject_id``, ``cohort_start_date``, ``cohort_end_date``. Labs use the last
    value in the stay by default; bounds (user ``bounds=``, then profile, then
    :data:`DEFAULT_PHYSIOLOGIC_BOUNDS`) are applied BEFORE aggregation. ``bounds=False`` disables all bounding.
    """

    def __init__(self, db_path: str | Path | None = None, *, con: duckdb.DuckDBPyConnection | None = None,
                 read_only: bool = True, bounds: Mapping[str, Sequence[float]] | bool | None = None,
                 population: str = "adult", harmonize_units: bool = True):
        if con is None and db_path is None:
            raise ValueError("Provide db_path or con.")
        self._owns = con is None
        self.con = con if con is not None else duckdb.connect(str(db_path), read_only=read_only)
        self.bounds = bounds
        self.population = population
        self.harmonize_units = harmonize_units
        self.labs: dict[str, list[str]] = {}
        self.lab_bounds: dict[str, list] = {}
        self.medications: dict[str, Any] = {}
        self.conditions: dict[str, list[str]] = {}
        self.profiles: list[str] = []

    def close(self) -> None:
        if self._owns:
            self.con.close()

    def add_domain_profile(self, profile: str | Mapping[str, Any] | Path) -> "Table1Generator":
        """Add a built-in profile by name (``DOMAIN_PROFILES``) or a custom dict / YAML path."""
        if isinstance(profile, str) and profile in DOMAIN_PROFILES:
            prof, name = load_profile(DOMAIN_PROFILES[profile]), profile
        elif isinstance(profile, str) and not Path(profile).exists():
            raise KeyError(f"Unknown profile {profile!r}; built-ins: {sorted(DOMAIN_PROFILES)}.")
        else:
            prof, name = load_profile(profile), "custom"
        self.labs.update({k: list(v) for k, v in prof.get("labs", {}).items()})
        self.lab_bounds.update({k: list(v) for k, v in prof.get("bounds", {}).items()})
        self.medications.update(prof.get("medications", {}))
        self.conditions.update({k: list(v) for k, v in prof.get("conditions", {}).items()})
        self.profiles.append(name)
        return self

    def add_custom_lab(self, name: str, loinc_codes: Sequence[str | int], bounds: Sequence[float] | None = None):
        self.labs[name] = list(loinc_codes)
        if bounds is not None:
            self.lab_bounds[name] = list(bounds)
        return self

    def add_custom_medication(self, name: str, spec: Mapping[str, Any] | Sequence[int]):
        self.medications[name] = spec
        return self

    def add_custom_condition(self, name: str, icd10_prefixes: Sequence[str]):
        self.conditions[name] = list(icd10_prefixes)
        return self

    # ------------------------------------------------------------------ extraction
    def _effective_bounds(self) -> dict[str, list]:
        if self.bounds is False:
            return {}
        eff = resolve_bounds(None, population=self.population)
        eff.update(self.lab_bounds)
        if isinstance(self.bounds, Mapping):
            eff = {**eff, **resolve_bounds(self.bounds, population=self.population)}
        return {k: v for k, v in eff.items() if k in self.labs}

    def extract(self, cohort_table: str, *, lab_strategy: str = "last_before_discharge",
                med_window: str = "pre_admission") -> pd.DataFrame:
        """Cohort rows with one column per profile lab, medication class and comorbidity."""
        from omop_etl.features import extract_measurements
        from omop_etl.medication_classes import extract_medication_classes

        con = self.con
        base = con.execute(f"SELECT * FROM {cohort_table}").df()
        key = ["subject_id", "cohort_start_date"]
        out = base
        self.unit_report = None
        if self.labs:
            table = "measurement"
            if self.harmonize_units:
                self.unit_report = harmonize_measurement_units(con, self.labs)
                table = "measurement_harmonized"
            labs = extract_measurements(con, cohort_table, loinc_map=self.labs, strategy=lab_strategy,
                                        outlier_bounds={k: tuple(v) for k, v in self._effective_bounds().items()},
                                        measurement_table=table)
            out = out.merge(labs, on=key, how="left")
        if self.medications:
            med = extract_medication_classes(con, cohort_table, self.medications, "standard", window=med_window)
            flags = med[list(self.medications)].reset_index(drop=True)
            out = out.reset_index(drop=True).join(flags.add_prefix("rx_"))
        if self.conditions:
            for name, prefixes in self.conditions.items():
                pats = " OR ".join("starts_with(REPLACE(UPPER(co.condition_source_value), '.', ''), "
                                   f"'{p.replace('.', '').upper()}')" for p in prefixes)
                hit = con.execute(
                    f"SELECT DISTINCT c.subject_id, c.cohort_start_date FROM {cohort_table} c "
                    "JOIN condition_occurrence co ON co.person_id = c.subject_id "
                    "AND co.condition_start_date <= c.cohort_end_date "
                    f"WHERE {pats}").df()
                hit[f"dx_{name}"] = 1
                out = out.merge(hit, on=key, how="left")
                out[f"dx_{name}"] = out[f"dx_{name}"].fillna(0).astype(int)
        return out

    def generate(self, cohort_table: str, stratify_by: str | None = None, *, lab_strategy: str = "last_before_discharge",
                 med_window: str = "pre_admission", extra_continuous: Sequence[str] = (),
                 extra_categorical: Sequence[str] = (), **table1_kwargs) -> dict[str, pd.DataFrame]:
        """Extract profile features and return ``generate_table1`` output (``table1``, ``table1b``).

        ``self.features`` and ``self.unit_report`` hold the analysed frame and the unit preflight report.
        """
        df = self.extract(cohort_table, lab_strategy=lab_strategy, med_window=med_window)
        self.features = df
        cont = list(self.labs) + list(extra_continuous)
        cat = [c for c in df.columns if c.startswith(("rx_", "dx_"))] + list(extra_categorical)
        return generate_table1(df, strata_col=stratify_by, continuous_vars=cont, categorical_vars=cat,
                               **table1_kwargs)
