# Manuscript template v0.32.0

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
