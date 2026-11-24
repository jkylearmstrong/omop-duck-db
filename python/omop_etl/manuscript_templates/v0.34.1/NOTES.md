# Manuscript template v0.33.6

The prose follows the manuscript delivered on 2026-11-11, with every number replaced by a value read from the run
(`omop_etl.manuscript_text.manuscript_context`). Table and figure numbers come from `layout.json`, so a table that a run
does not produce is skipped and every later number and reference follows.

`scripts/check_manuscript_text.py` renders this template from a workbook and compares it, sentence by sentence, with a
delivered PDF. Rendered from the 2026-11-11 workbook, the sentences below are expected to differ from that PDF. Each is a
deliberate change, not a rendering error:

| Delivered text | This template | Why |
|---|---|---|
| Confidence intervals of the regression table are "Huber-White robust" | Wald intervals from the model-based standard errors | The package fits the table by Newton-Raphson and reports model-based standard errors; no robust variance is computed. |
| Methods 2.3 says L2 regularization stabilizes the odds-ratio estimates | The benchmark Logistic Regression is L2-regularized; the odds-ratio table is fit without a penalty | The delivered text said both. |
| Leave-one-site-out uses "the benchmark XGBoost (100 trees, learning rate 0.1, depth 6)" | The F1-tuned XGBoost configuration, read from Table 3 | The delivered Table 5 was computed with the F1-tuned configuration. |
| Lactate, arterial blood gases and troponin "were not extracted" | The laboratory list is read from the regression table | Since 0.30.0 these laboratories are extracted. |
| Laboratories "matched across health systems by LOINC code" | Matched through the OMOP vocabulary hierarchy (concept_ancestor) | Since 0.29.0 every laboratory is a hierarchy-based concept set. |
| "Evaluating the models across calendar subsets confirms ..." | Left out | No calendar-subset analysis is run by the package. |
| Hash-collision history, the hand-edited cohort query, `build_random_stay_cohort.py` | Left out | They describe the one-off pipeline behind that delivery, not the package. |
| Seeds and all-stays sensitivity analyses (ten seeds, 534,429 stays, AUC 0.74–0.75) | Shown only when the run records them (`sensitivity` in the context) | The study run does not produce them yet. |
| "(Table 6)" for the seed analysis | Table numbers from `layout.json` | The delivered "(Table 6)" pointed at the wrong table. Section numbers (2.1, 2.1.1, ...) and "(Section 2.1.1)" are kept as delivered. |
| Review-board statement, author list | From the study team's parameters file (`--manuscript-params`) | Institution names and people are not stored in the package. |
| Software section with Python 3.10 | The run's own versions, cited through grateful_py | The delivered section was edited by hand. |
| Table 1 values are "the latest plausible measurement" and differ from the model inputs (glucose 151 vs 161 mg/dL) | Table 1 shows the within-stay mean, the value the models use | The package's Table 1 is built from the model features; there is no separate latest-value table. |
| Accuracy and AUC-PR "computed in the benchmark output files but not tabulated" | Left out | The package does not write them to any output file. |
| AMA readmission rate compared with "11.0% overall" | Compared with the rate of the other discharges | The AMA group is part of the overall rate, so the contrast is with the other discharges. |
| "(seed 20261104)" for the random stay draw | "seeded" without the number | The package draws in the database (`define_study_cohort(sample_stay_rule="random")`), not with that script's seed. |
| Methods 2.4 hyperparameters (e.g. HistGBM and XGBoost at 300 iterations, learning rate 0.05) | The fixed benchmark configuration read from the estimators, plus the configuration F1 tuning selected | The delivered 2.4 and Appendix B disagreed; both now come from `default_model_grid`, so they cannot drift from the code. |
| Feature extraction and manuscript code "held by the study team, available on request" | The `define_study_cohort` call and the package version | The code is the open-source package; the call is printed from the run record's cohort parameters. |
| "Several terms show odds ratios slightly below 1" (any p) | Terms with an upper confidence limit below 1 and p < 0.05; "slightly" only when every such OR is at least 0.8 | The delivered list was chosen by hand. |
| "Global SHAP feature importance confirms that historical acute care utilization ... are the primary drivers" | Names the three top SHAP predictors of the run | The delivered sentence was written for one result. |

The check lays the rendered Markdown out as a PDF's extracted text reads (a sub-heading runs into its first paragraph, list items
run on from the paragraph above, figure captions are included), so a sentence counts as identical only when its words match.
Sentences the PDF splits across a page break, table text, and the review-board and author sections without a parameters file
still come back as absent.

## Changes in v0.33.0 (revisions requested on 2026-11-18)

| v0.32.0 | v0.33.0 | Why |
|---|---|---|
| Table 3 compared configurations selected by F1, by AUC-ROC and the fixed baseline | Not printed; the text cites the full grid (supplementary) instead, and Section 3.2.1 is folded into 3.2 | Requested: drop Table 3. The workbook keeps the sheet, so the text still names the F1-tuned configurations. |
| Cross-site table reported the uncalibrated Brier score | F1 at the 0.50 threshold, beside sensitivity and specificity | Requested: replace Brier with F1. |
| Regression table listed only the non-reference levels | Each multilevel feature (age, race, SDI, RUCA, discharge status, tobacco, BMI) prints its reference category (OR 1.00) | Requested. |
| Discharge status "Missing / Unknown" held every status outside home, home health, SNF/rehab and AMA | "Other" (hospice, assisted living, PCORnet OT, ...) and "Missing / Unknown" (no status recorded) are separate levels | The old label counted known statuses as unknown. |
| Figure 1 drew each exclusion as a dashed arrow into a box beside the previous step | Each exclusion branches off the middle of the down arrow into its step, with its text to the right (as in McCoy et al., JGIM 2017) | Requested. |

## Changes in v0.33.2 (reviewer check of run C4, 2026-11-20)

| v0.33.0 | v0.33.2 | Why |
|---|---|---|
| Abstract named the two significant risk terms with the largest standardized beta, "not measured" indicators included | Missing-value indicators are left out of that ranking | Run C4 led with "diastolic blood pressure: not measured", a data-capture term. |
| Systolic and diastolic "not measured" were two terms (merged only when identical) | Indicators that differ in fewer than 2% of the stays where either is missing are one term | The two were nearly collinear and split one effect into OR 0.41 and OR 1.92. |
| "Stays without a usable ZIP code are reported as Missing/Unknown rather than assigned a reference category" in every run | Follows `sdi_missing_term`: with it off, the text says missing SDI shares the reference with quintile 1 | The sentence contradicted the regression table when the term was off. |
| Labels in a sentence lower-cased their first letter ("aLT", "bMI", "diastolic Blood Pressure") | Ordinary words of the feature name are lower-cased; acronyms and the level after ": " stay as written | Casing errors. |
| The cohort call had no seed; the text said "drawn at random with a seed" | The call prints `random_seed` (define_study_cohort's default unless the run set one) | Readers could not see the seed. |
| Columns with no observed values listed each measure once per column ("lab potassium, lab potassium") | Each measure named once; the count is still the number of columns | Duplicate names. |

Not template changes but in the same release: the printed grid table (workbook Table S4, printed as S5) leaves out the unrounded `auc_raw` / `f1_raw` columns, which the workbook keeps.

## Changes in v0.33.3 (Kyle's review of run C5, 2026-11-21)

- Figure 1 always ends with the grant protocol window box and the analytic cohort, whatever `consort_final_steps` says, so the
  figure, the text and Table 1 give the same N. The paragraph after the eligibility list now points to Figure 1.
- The CONSORT table (workbook "Table S3 - CONSORT Attrition") is not printed; the workbook keeps it. Later supplementary tables move up.
- The length-of-stay table is printed with the sites and "All Sites Combined" as columns and one statistic per row (`layout.json`
  `"transpose"`); the workbook keeps one row per site, as delivered.
- "Total qualifying stays" is the number of eligible stays with an index admission in the window (from the run record). Before,
  with `consort_final_steps=False`, the text printed the first CONSORT step's patient count there.

## Changes in v0.33.4 (Kyle's review of run C6, 2026-11-22)

| v0.33.3 | v0.33.4 | Why |
|---|---|---|
| A paragraph ending in `{% endif %}` lost the blank line after it (`trim_blocks`), so the abstract's Results and Conclusions ran on inside Methods, and the Table 1 caption ran on from the sentence before it | Such lines end in `+%}`, which keeps the newline | Fourteen paragraphs were affected; `tests/test_manuscript_fixes_0334.py` checks that none is left. |
| The Section 2.4 model list rendered as one paragraph ("...standard errors.2. Random Forest...") | One numbered item per line | The loop's last tag ate each item's newline. |
| Section 2.2 named `prior_IP_OS_ED_count`, `readmission_count_twelve_months` and `n_secondary_dx_ccs` | Named in words | Column names do not belong in the prose. |
| Section 3.5 and Figure 2 listed column names (`elix_van_walraven_score`, `lab_bun_last`, ...) | Clinical labels (`manuscript_tables.feature_label`), the labels of Tables 1 and 4 where they exist | As in the GrantScoped draft. |
| Page 1 read "Study manuscript" when the parameters file had no title | `layout.json` `"title"` is the study title, used when neither an explicit title nor the parameters give one | The full text describes this one study. |
| Table 3 (leave-one-site-out) could split across pages | `layout.json` `"keep_together"`: Tables 2 and 3 stay on one page with their caption in the PDF | Word paginates on its own. |

## Changes in v0.33.5 (Kyle's review of run C7, 2026-11-22)

| v0.33.4 | v0.33.5 | Why |
|---|---|---|
| The abstract named the two clinical risk factors with the largest standardized beta (C7: prior readmissions, anion gap) | The same two "by standardized effect", plus the significant clinical risk factor with the largest odds ratio when it is not one of them (C7: discharge against medical advice, OR 1.88) | Per-unit and yes/no odds ratios are not comparable, so the ranking stays standardized; the largest OR is named alongside it, chosen by the run. |

## Changes in v0.33.6 (audit of run C8 before the draft for Dr. Rubin, 2026-11-22)

| v0.33.5 | v0.33.6 | Why |
|---|---|---|
| The author list ran on ("manuscript drafting.- Daniel J. Rubin") | One author per line | The item line ended in `{% endif %}`, which ate its newline. |
| "ranks elixhauser score (van walraven), ..." | "Elixhauser score (van Walraven)" | `lower_first` keeps proper names (`manuscript_text.PROPER_NOUNS`). |
| Section 2.2 named empty columns as "vital heart rate", "lab potassium", "with no observed values" | "heart rate", "potassium", "with no values in any site's source data" | Clinical labels, as elsewhere; the C8 check found these were never in the sites' extracts (PCORnet VITAL has no heart rate, respiratory rate, temperature or SpO2; the lab pulls followed the study list). |
| Section 2.2 listed Table 4's laboratories only | Every laboratory the models used (hemoglobin was a top SHAP predictor) | Read from `<prefix>_Missingness.csv` when it is next to the workbook. |
| "HistGBM and XGBoost AUC-ROC 0.777–0.777" | "0.777" when both ends print the same | `fmt_range`. |
| Section 2.1 repeated the same date range for each site | One sentence when every site spans the whole window | Repetition. |
| Section 2.3 gave the logistic regression's C as 1.0 | The F1-tuned C, with a pointer to Section 2.4 | Section 2.4 and Table 2 report the tuned model. |
| Appendix B gave the baseline settings without saying so | Says they are the fixed baselines and points to Section 2.4 | Same reason. |
| "strictly ran", "standalone ... standalone", "(stays with unverified follow-up status were excluded)" twice, "2017-01-01 to 2022-09-30 protocol period (spanning from January 1, 2017 to ...)" | Each said once | Repetition. |
| The bootstrap paragraph listed AUPRC | Lists the metrics that are reported | No AUPRC is reported. |
| No qualifying-stay count when the caller passed no run record | The run's own `<prefix>_run.json` next to the workbook fills what the caller leaves out | C8 printed no count. |

## v0.34.0 (Dr. Rubin's review of C9, 2026-11-23)

| Was | Now | Why |
|---|---|---|
| Model metrics in the text to 3 decimals (AUC-ROC 0.731, Brier 0.198) | 2 decimals, the values printed in Tables 2 and 3; the LOSO mean is the table's own "Mean" row | Dr. Rubin: Tables 2 and 3 and the text to 2 decimals, the same values throughout. |
| Section 3.5 described a beeswarm ("points ... red points") | Describes the violin plot of Figure 2 | Figure 2 is a violin plot again. |
| Section 2.1 did not say why Figure 1 excludes patients at its last step | Says the study scores one random draw made once and reused, and how many patients outside it are excluded; the Figure 1 caption says the same | External reviewers. |
| Section 2.2: "distinct CCS condition categories recorded during the index stay" | "distinct ICD-9-CM and ICD-10-CM codes recorded during the index stay, other than the principal diagnosis" | The column counted distinct standard concepts, not CCS categories, and included the principal diagnosis. Since 0.34.0 it is `n_secondary_dx`, counted as the text now says (Kyle: do what is correct). |
| Utilization list without outpatient visits; Table 1 "Prior Outpatient / Other Encounter Count" was all prior visits minus inpatient / observation / ED | Lists "prior outpatient and telehealth visits"; Table 1 prints `prior_outpatient_count` | The difference also counted visits of other or unknown type, which varied by site. |
| Section 2.1: patients whose stay in the reused draw no longer qualified were excluded at the last step | Says how many patients were given another of their qualifying stays at random with a fixed seed (`random_draw.redrawn`, from `random_draw_redrawn` in the run record) | Since 0.34.1 the draw samples qualifying stays only (Kyle, 2026-11-24): the discharge-status repair made some drawn stays transfers or in-hospital deaths. |
| C14: "1 patients", and "made once and reused" twice in Section 2.1 | Singular for a count of one (Section 2.1, Figure 1, caption); the redraw sentence starts "For N patients" after the exclusion sentence | Kyle's C14 reviewer flags. |
| Methods said BMI is the index-stay mean only | Abstract, Sections 2.1, 2.2 and 3.1 describe the BMI fill (`bmi_fill` from the run record; height and weight, then the 365 days before admission) | Kyle's PR 104 review: the 0.34.1 features fill a missing BMI. |
| Section 2.2 did not say eGFR and the anion gap fallback are computed | One sentence each, shown when the lab is listed | Kyle's PR 104 review. |
| Appendix B defined AUPRC, which is not reported (removed from the text in v0.33.6) | AUPRC subsection removed | Kyle's PR 104 review. |
| Days since prior inpatient / obs / ED encounter ignored observation stays | No text change; the feature now counts observation stays (581385) as its label says | Kyle's PR 104 review. |
| Tobacco: a stay with no status was "Never user" | Table 1 and Table 4 print "Tobacco: Missing / Unknown" when stays have no smoking status or tobacco code | Kyle's PR 104 review; the legacy Table 1b had 31.8% missing. |
