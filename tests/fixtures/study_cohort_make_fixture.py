"""Regenerates the synthetic CDM extract used by the define_study_cohort equivalence tests.

Run from the repository root:  python tests/fixtures/study_cohort_make_fixture.py

The output (tests/fixtures/study_cohort_cdm/*.csv) is committed and shared by the Python and R test suites, so
that both languages are checked against the same data and the same golden results
(tests/fixtures/study_cohort_golden.csv). Everything here is synthetic (seeded pseudo-random numbers); it contains
no real patients, sites, or paths.

The data is deliberately adversarial for cohort logic rather than realistic: stays that overlap, are adjacent, or
are same-day transfers; deaths inside / just after / long after / before a stay; discharge dispositions that mean
death, that are unknown, and that are look-alike codes; follow-up evidence that lands exactly on, one day before,
and one day after the horizon boundaries; observation periods that start exactly on / one day off the wash-in
boundary; people close to the 18th and 65th birthdays; negative-length stays; birth dates with a missing month and/or day.

CHANGING THIS FILE CHANGES THE DATA, so the golden file must be regenerated and re-verified in BOTH languages
(see study_cohort_make_golden.py). Do not edit the committed CSVs by hand.
"""

from __future__ import annotations

import csv
import datetime as dt
import random
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent / "study_cohort_cdm"
N_PERSONS = 260
SEED = 5304

D = dt.date


def iso(d):
    return "" if d is None else d.isoformat()


def main() -> None:
    rng = random.Random(SEED)
    persons, visits, deaths, obs, meas, cond, drug = [], [], [], [], [], [], []
    visit_id = meas_id = cond_id = drug_id = obs_id = 0

    for pid in range(1, N_PERSONS + 1):
        ref = D(2018, 6, 1) + dt.timedelta(days=rng.randint(0, 900))

        # ---- demographics: mixture that straddles the 18th / 65th birthday around the reference date
        r = rng.random()
        if r < 0.07:
            age_days = rng.randint(1, 17 * 365)
        elif r < 0.20:
            age_days = int(18 * 365.25) + rng.randint(-60, 60)
        elif r < 0.28:
            age_days = int(65 * 365.25) + rng.randint(-60, 60)
        else:
            age_days = rng.randint(int(18.5 * 365.25), int(95 * 365.25))
        dob = ref - dt.timedelta(days=age_days)
        yob, mob, dob_day = dob.year, dob.month, dob.day
        r = rng.random()
        if r < 0.08:
            mob = dob_day = None  # year only -> treated as 1 January
        elif r < 0.16:
            dob_day = None  # year + month only -> treated as the 1st of the month
        persons.append([pid, rng.choice([8507, 8532]), yob, mob, dob_day, 8527, 38003564])

        # ---- stays
        person_visits = []
        n_stays = rng.choice([1, 1, 2, 2, 3, 3, 4, 6])
        start = ref + dt.timedelta(days=rng.randint(-250, 250))
        for k in range(n_stays):
            concept = rng.choices([9201, 9202, 9203], weights=[62, 24, 14])[0]
            if concept == 9201:
                los = rng.choice([0] + [1] * 2 + [2, 3, 4, 5, 6] * 3 + [7, 9, 12, 20])
            else:
                los = rng.choice([0, 0, 0, 0, 1])
            end = start + dt.timedelta(days=los)
            if rng.random() < 0.012:  # data-entry error: end before start
                end = start - dt.timedelta(days=rng.randint(1, 3))
            disch = rng.choices(
                [8536, 38004284, 4216643, 4155309, 8546, None, 44814650, 581476],
                weights=[56, 8, 4, 4, 3, 17, 4, 4],
            )[0]
            if concept != 9201 and disch in (4216643, 4155309, 8546):
                disch = None
            visit_id += 1
            person_visits.append(
                dict(id=visit_id, pid=pid, concept=concept, start=start, end=end, disch=disch)
            )
            # next stay: overlapping, same-day transfer, adjacent, or a gap straddling common horizons
            gap = rng.choice([-2, -1, 0, 0, 1, 1, 2, 5, 12, 25, 29, 30, 31, 32, 45, 60, 61, 90, 91, 200, 400])
            start = max(start, end) + dt.timedelta(days=gap)

        # ---- death
        if rng.random() < 0.22:
            v = rng.choice(person_visits)
            mode = rng.choice(["in_stay", "end", "d+1", "short", "short", "boundary", "late", "pre"])
            if mode == "in_stay":
                span = max((v["end"] - v["start"]).days, 0)
                ddate = v["start"] + dt.timedelta(days=rng.randint(0, span))
            elif mode == "end":
                ddate = v["end"]
            elif mode == "d+1":
                ddate = v["end"] + dt.timedelta(days=1)
            elif mode == "short":
                ddate = v["end"] + dt.timedelta(days=rng.randint(2, 45))
            elif mode == "boundary":
                ddate = v["end"] + dt.timedelta(days=rng.choice([7, 8, 29, 30, 31, 89, 90, 91]))
            elif mode == "late":
                ddate = v["end"] + dt.timedelta(days=rng.randint(100, 600))
            else:  # death recorded before the admission (inconsistent source data)
                ddate = v["start"] - dt.timedelta(days=rng.randint(1, 60))
            if rng.random() < 0.9:
                deaths.append([pid, iso(ddate)])
            if mode in ("in_stay", "end") and rng.random() < 0.5 and v["concept"] == 9201:
                v["disch"] = 4216643  # disposition says died; death row may or may not exist
        elif rng.random() < 0.05:
            # disposition-only death with no death row, possibly followed by later stays (post-death activity)
            v = rng.choice(person_visits)
            if v["concept"] == 9201:
                v["disch"] = 4216643

        for v in person_visits:
            visits.append(
                [v["id"], v["pid"], v["concept"], iso(v["start"]), iso(v["end"]), 32827, v["disch"]]
            )

        # ---- observation periods
        first_start = min(v["start"] for v in person_visits)
        last_end = max(v["end"] for v in person_visits)
        r = rng.random()
        if r < 0.08:
            pass  # no observation period at all
        elif r < 0.20:
            for lo, hi in ((first_start - dt.timedelta(days=rng.randint(100, 700)), first_start + dt.timedelta(days=rng.randint(0, 200))),
                           (last_end - dt.timedelta(days=rng.randint(0, 50)), last_end + dt.timedelta(days=rng.randint(10, 800)))):
                obs_id += 1
                obs.append([obs_id, pid, iso(lo), iso(hi), 32817])
        else:
            lo = first_start - dt.timedelta(days=rng.choice([0, 30, 364, 365, 366, 400, 700, rng.randint(0, 900)]))
            hi = last_end + dt.timedelta(days=rng.choice([-40, 0, 29, 30, 31, 60, 90, 200, rng.randint(-30, 900)]))
            obs_id += 1
            obs.append([obs_id, pid, iso(lo), iso(hi), 32817])

        # ---- follow-up evidence in the three event tables (some exactly on horizon boundaries)
        def rand_date():
            return first_start - dt.timedelta(days=300) + dt.timedelta(
                days=rng.randint(0, (last_end - first_start).days + 700)
            )

        for _ in range(rng.choice([0, 0, 1, 2, 4, 8])):
            meas_id += 1
            meas.append([meas_id, pid, rng.choice([3004249, 3027018, 3016723]), iso(rand_date()), 32817])
        for _ in range(rng.choice([0, 0, 1, 2, 4, 8])):
            cond_id += 1
            cond.append([cond_id, pid, rng.choice([201826, 316139, 4329847]), iso(rand_date()), 32020])
        for _ in range(rng.choice([0, 0, 1, 2, 4, 8])):
            drug_id += 1
            d0 = rand_date()
            drug.append([drug_id, pid, rng.choice([1503297, 1308216, 1112807]), iso(d0), iso(d0), 32838])
        if rng.random() < 0.18:
            v = rng.choice(person_visits)
            off = rng.choice([-1, 0, 1, 6, 7, 8, 29, 30, 31, 59, 60, 61, 89, 90, 91])
            edate = iso(v["end"] + dt.timedelta(days=off))
            tbl = rng.choice(["meas", "cond", "drug"])
            if tbl == "meas":
                meas_id += 1
                meas.append([meas_id, pid, 3004249, edate, 32817])
            elif tbl == "cond":
                cond_id += 1
                cond.append([cond_id, pid, 201826, edate, 32020])
            else:
                drug_id += 1
                drug.append([drug_id, pid, 1503297, edate, edate, 32838])

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    def write(name, header, rows):
        with open(OUT_DIR / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(header)
            for row in rows:
                w.writerow(["" if x is None else x for x in row])

    write("person", ["person_id", "gender_concept_id", "year_of_birth", "month_of_birth", "day_of_birth",
                     "race_concept_id", "ethnicity_concept_id"], persons)
    write("visit_occurrence", ["visit_occurrence_id", "person_id", "visit_concept_id", "visit_start_date",
                               "visit_end_date", "visit_type_concept_id", "discharged_to_concept_id"], visits)
    write("death", ["person_id", "death_date"], deaths)
    write("observation_period", ["observation_period_id", "person_id", "observation_period_start_date",
                                 "observation_period_end_date", "period_type_concept_id"], obs)
    write("measurement", ["measurement_id", "person_id", "measurement_concept_id", "measurement_date",
                          "measurement_type_concept_id"], meas)
    write("condition_occurrence", ["condition_occurrence_id", "person_id", "condition_concept_id",
                                   "condition_start_date", "condition_type_concept_id"], cond)
    write("drug_exposure", ["drug_exposure_id", "person_id", "drug_concept_id", "drug_exposure_start_date",
                            "drug_exposure_end_date", "drug_type_concept_id"], drug)
    print(f"wrote {len(persons)} persons, {len(visits)} visits, {len(deaths)} deaths, {len(obs)} obs periods, "
          f"{len(meas)}/{len(cond)}/{len(drug)} measurement/condition/drug rows to {OUT_DIR}")


if __name__ == "__main__":
    main()
