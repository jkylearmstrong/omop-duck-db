"""Regenerate the shared comorbidity fixtures (run from anywhere: ``python tests/fixtures/comorbidity_make_fixture.py``).

Writes, next to this file:

* ``comorbidity_conditions.csv``       condition_occurrence rows (one scenario label per row)
* ``comorbidity_cohort.csv``           cohort rows (cohort_definition_id, subject_id, cohort_start_date)
* ``comorbidity_concept_ancestor.csv`` a tiny synthetic concept_ancestor (self rows included)
* ``comorbidity_golden_elixhauser.csv`` / ``comorbidity_golden_charlson.csv``  the expected output of
  ``extract_elixhauser_comorbidities`` / ``extract_charlson_index`` for four configurations

Both test suites (tests/test_comorbidity.py and tests/testthat/test-comorbidity.R) load these files and assert the
SAME golden values, which is the cross-language parity check.

The golden values come from the plain-Python ``reference()`` below. It shares no code with ``omop_etl`` or the R
package and uses no SQL: it reads the curated code tables with the ``csv`` module and loops over the records. The
hand-built scenarios are small enough to verify by hand, and the tests re-derive several of them by hand (for example
person 40 below: Elixhauser 30 adjusted / 34 raw, Charlson 13 adjusted / 17 raw).

Scenarios (see the ``scenario`` column for the exact labels):

* 10-15   window boundaries around the index date 2021-06-01 (index-1 in, index day out, index-365 in, index-366 out,
          after the index out, and a 2015 record that only the all-history window sees)
* 20-24   normalisation: lower case, spaces and dots, no dot, NULL and empty source value
* 30-33   negative controls: unrelated codes, a code shorter than the prefix, a code that merely CONTAINS a prefix
* 40-45   hierarchy cases with hand-computed scores
* 50-51   several index dates for one person; a duplicated cohort row
* 60-62   a second cohort id
* 70-71   persons without a usable condition
* 80-88   the standard-concept side (child, grandchild, the ancestor itself, a concept under two ancestors, an
          unrelated concept, standard + source in different domains, concept 0 with a matching source value)
* 1000+   one probe person per Elixhauser domain and channel (ICD-10, ICD-9, standard descendant);
  2000+   the same for each Charlson category
"""

import csv
import re
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODES = HERE.parent.parent / "inst" / "extdata" / "comorbidity"
INDEX_DATE = date(2021, 6, 1)
CONFIGS = {
    # name: (lookback_days, source_and_standard, hierarchy_adjusted)
    "default": (365, True, True),
    "raw": (365, True, False),
    "source_only": (365, False, True),
    "all_history": (None, True, True),
}


def read_codes(index):
    """-> ([(domain, column, weight)] in CSV order, {domain: ICD-10-CM prefixes}, {domain: ICD-9-CM prefixes},
    {domain: SNOMED ancestor ids})."""
    domains, icd10, icd9, anc = {}, {}, {}, {}
    with open(CODES / f"{index}_codes.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            domains.setdefault(r["domain"], (r["column"], int(r["weight"])))
            if r["code_system"] == "SNOMED_ANCESTOR":
                anc.setdefault(r["domain"], []).append(int(r["code"]))
            elif r["code_system"] == "ICD10CM":
                icd10.setdefault(r["domain"], []).append(r["code"])
            else:
                icd9.setdefault(r["domain"], []).append(r["code"])
    return [(d, c, w) for d, (c, w) in domains.items()], icd10, icd9, anc


HIERARCHY = {
    "elixhauser": (("dm_comp", "dm_uncomp"), ("mets", "solid_tumor"), ("htn_comp", "htn_uncomp")),
    "charlson": (("mod_severe_liver", "mild_liver"), ("dm_comp", "dm_uncomp"), ("mets", "malignancy")),
}
SUMMARY = {
    "elixhauser": ("elix_van_walraven_score", "elix_total_conditions"),
    "charlson": ("charlson_index", "cci_total_conditions"),
}


def icd_probe(prefix, lower):
    """A realistic dotted code that starts with ``prefix`` (e.g. I099 -> I09.9, I50 -> I50.9)."""
    code = prefix[:3] + "." + prefix[3:] if len(prefix) > 3 else prefix + ".9"
    return code.lower() if lower else code


# ---------------------------------------------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------------------------------------------
def build():
    conds, cohort, ancestors = [], [], set()  # conds: (person, concept, date, source, scenario)

    def person(pid, label, rows, index_dates=(INDEX_DATE,), cohort_ids=(1,)):
        for cid in cohort_ids:
            for d in index_dates:
                cohort.append((cid, pid, d, label))
        for concept, d, source in rows:
            conds.append((pid, concept, d, source, label))

    def tree(*pairs):
        ancestors.update(pairs)

    d = INDEX_DATE
    # window boundaries (chf code I50.9)
    person(10, "window: index-1 in", [(0, d - timedelta(1), "I50.9")])
    person(11, "window: index day out", [(0, d, "I50.9")])
    person(12, "window: index-365 in", [(0, d - timedelta(365), "I50.9")])
    person(13, "window: index-366 out", [(0, d - timedelta(366), "I50.9")])
    person(14, "window: after index out", [(0, d + timedelta(1), "I50.9")])
    person(15, "window: 2015 only in all-history", [(0, date(2015, 1, 1), "I50.9")])
    # normalisation
    person(20, "normalise: lower case", [(0, date(2021, 5, 1), "i50.9")])
    person(21, "normalise: spaces and dots", [(0, date(2021, 5, 1), " I 50 . 9 ")])
    person(22, "normalise: ICD-9 with and without dot", [(0, date(2021, 5, 1), "428.0"), (0, date(2021, 5, 2), "4280")])
    person(23, "normalise: NULL source value", [(0, date(2021, 5, 1), None)])
    person(24, "normalise: empty source value", [(0, date(2021, 5, 1), "")])
    # negative controls
    person(30, "negative: unrelated codes", [(0, date(2021, 5, 1), "Z00.00"), (0, date(2021, 5, 2), "J01.90")])
    person(31, "negative: shorter than the prefix", [(0, date(2021, 5, 1), "I5")])
    person(32, "negative: contains, does not start with", [(0, date(2021, 5, 1), "XI50.9")])
    person(33, "negative: neighbouring codes", [(0, date(2021, 5, 1), "I51.9"), (0, date(2021, 5, 2), "429.9")])
    # hierarchy
    person(40, "hierarchy: everything", [
        (0, date(2021, 5, 1), "I50.9"), (0, date(2021, 5, 2), "E11.22"), (0, date(2021, 5, 3), "E11.9"),
        (0, date(2021, 5, 4), "C78.0"), (0, date(2021, 5, 5), "C50.9"), (0, date(2021, 5, 6), "I12.9"),
        (0, date(2021, 5, 7), "I10"), (0, date(2021, 5, 8), "K72.9"), (0, date(2021, 5, 9), "K73.9"),
        (0, date(2021, 5, 10), "I21.9")])
    person(41, "hierarchy: diabetes", [(0, date(2021, 5, 1), "E11.22"), (0, date(2021, 5, 2), "E11.9")])
    person(42, "hierarchy: negative weights", [
        (0, date(2021, 5, 1), "E66.9"), (0, date(2021, 5, 2), "F11.20"), (0, date(2021, 5, 3), "F32.9"),
        (0, date(2021, 5, 4), "E03.9")])
    person(43, "hierarchy: mets and solid tumour", [(0, date(2021, 5, 1), "C78.0"), (0, date(2021, 5, 2), "C50.9")])
    person(44, "hierarchy: liver", [(0, date(2021, 5, 1), "K73.9"), (0, date(2021, 5, 2), "K72.9")])
    person(45, "hierarchy: hypertension", [(0, date(2021, 5, 1), "I10"), (0, date(2021, 5, 2), "I12.9")])
    # several index dates; duplicated cohort row
    person(50, "multi index: chf then copd", [(0, date(2021, 1, 10), "I50.9"), (0, date(2021, 8, 15), "J44.9")],
           index_dates=(date(2021, 2, 1), date(2021, 9, 1), date(2022, 9, 20)))
    person(51, "duplicate cohort row", [(0, date(2021, 5, 1), "I50.9")], index_dates=(d, d))
    # cohort id 2
    person(60, "cohort 2 only: chf", [(0, date(2021, 5, 1), "I50.9")], cohort_ids=(2,))
    person(61, "cohort 2 only: no conditions", [], cohort_ids=(2,))
    person(62, "cohort 1 and 2: copd", [(0, date(2021, 5, 1), "J44.9")], cohort_ids=(1, 2))
    # no usable condition
    person(70, "none: no condition rows", [])
    person(71, "none: only after the index", [(0, date(2021, 7, 1), "I50.9")])
    # standard side: 316139 heart failure (chf), 255573 chronic lung disease (copd) are ancestors in both indices
    tree((316139, 316139), (316139, 9000001), (316139, 9000002), (9000001, 9000002), (9000001, 9000001),
         (9000002, 9000002), (255573, 255573), (255573, 9000003), (316139, 9000003), (9000003, 9000003))
    person(80, "standard: child", [(9000001, date(2021, 5, 1), None)])
    person(81, "standard: grandchild", [(9000002, date(2021, 5, 1), "ZZZ")])
    person(82, "standard: the ancestor itself", [(316139, date(2021, 5, 1), None)])
    person(83, "standard: under two ancestors", [(9000003, date(2021, 5, 1), None)])
    person(84, "standard: unrelated concept", [(9000099, date(2021, 5, 1), "ZZZ")])
    person(85, "standard and source, same domain", [(9000001, date(2021, 5, 1), "I50.9")])
    person(86, "standard chf and source copd on one row", [(9000001, date(2021, 5, 1), "J44.9")])
    person(87, "standard: on the index day", [(9000001, d, None)])
    person(88, "standard: concept 0 with a source value", [(0, date(2021, 5, 1), "J44.9")])

    # probes: one person per (index, domain, channel)
    base = {"elixhauser": 1000, "charlson": 2000}
    next_concept = 8000000
    for index in ("elixhauser", "charlson"):
        domains, icd10, icd9, anc = read_codes(index)
        for i, (domain, _, _) in enumerate(domains):
            pid = base[index] + 3 * i
            lower = i % 2 == 1
            person(pid, f"probe {index} {domain} icd10", [(0, date(2021, 5, 1), icd_probe(icd10[domain][0], lower))])
            person(pid + 1, f"probe {index} {domain} icd9", [(0, date(2021, 5, 1), icd_probe(icd9[domain][0], lower))])
            if domain in anc:
                child, next_concept = next_concept + 1, next_concept + 1
                tree((anc[domain][0], anc[domain][0]), (anc[domain][0], child), (child, child))
                person(pid + 2, f"probe {index} {domain} standard",
                       [(child, date(2021, 5, 1), None if i % 2 == 0 else "ZZZ")])
    return conds, cohort, sorted(ancestors)


# ---------------------------------------------------------------------------------------------------------------
# Independent reference model (plain Python)
# ---------------------------------------------------------------------------------------------------------------
def reference(index, conds, cohort, ancestors, lookback, standard, adjusted):
    """Expected rows ``[(person, index_date, flags..., score, total)]`` for one configuration."""
    domains, icd10, icd9, anc = read_codes(index)
    below = {}
    for a, x in ancestors:
        below.setdefault(a, set()).add(x)
    standard_ids = {}
    for domain, _, _ in domains:
        ids = set()
        for a in anc.get(domain, []):
            ids |= {a} | below.get(a, set())
        standard_ids[domain] = ids
    suppressed = {s: dom for dom, s in HIERARCHY[index]} if adjusted else {}

    out = []
    for person, idx_date in sorted({(p, d) for _, p, d, _ in cohort}):
        flags = {}
        for domain, _, _ in domains:
            hit = False
            for pid, concept, cdate, source, _ in conds:
                if pid != person or not (cdate < idx_date):
                    continue
                if lookback is not None and cdate < idx_date - timedelta(lookback):
                    continue
                norm = re.sub(r"\s+", "", source or "").replace(".", "").upper()
                if any(norm.startswith(p) for p in icd10[domain] + icd9[domain]) or (standard and concept in standard_ids[domain]):
                    hit = True
                    break
            flags[domain] = int(hit)
        score = total = 0
        for domain, _, weight in domains:
            counted = 0 if (domain in suppressed and flags[suppressed[domain]]) else flags[domain]
            score += counted * weight
            total += counted
        out.append((person, idx_date, *[flags[dom] for dom, _, _ in domains], score, total))
    return out


# ---------------------------------------------------------------------------------------------------------------
def main():
    conds, cohort, ancestors = build()
    with open(HERE / "comorbidity_conditions.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["condition_occurrence_id", "person_id", "condition_concept_id", "condition_start_date",
                    "condition_source_value", "scenario"])
        for n, (pid, concept, d, source, label) in enumerate(conds, start=1):
            w.writerow([n, pid, concept, d.isoformat(), "" if source is None else source, label])
    with open(HERE / "comorbidity_cohort.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["cohort_definition_id", "subject_id", "cohort_start_date", "scenario"])
        for cid, pid, d, label in cohort:
            w.writerow([cid, pid, d.isoformat(), label])
    with open(HERE / "comorbidity_concept_ancestor.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["ancestor_concept_id", "descendant_concept_id"])
        w.writerows(ancestors)
    for index in ("elixhauser", "charlson"):
        domains = read_codes(index)[0]
        header = ["config", "subject_id", "cohort_start_date", *[c for _, c, _ in domains], *SUMMARY[index]]
        with open(HERE / f"comorbidity_golden_{index}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(header)
            for name, (lookback, standard, adjusted) in CONFIGS.items():
                for row in reference(index, conds, cohort, ancestors, lookback, standard, adjusted):
                    w.writerow([name, row[0], row[1].isoformat(), *row[2:]])
    print(f"{len(conds)} condition rows, {len(cohort)} cohort rows, {len(ancestors)} ancestor rows")


if __name__ == "__main__":
    main()
