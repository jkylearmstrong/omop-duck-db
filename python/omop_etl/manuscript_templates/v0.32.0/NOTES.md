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
| Numbered cross references written by hand ("(Table 6)" for the seed analysis, "Section 2.1.1") | Numbers from `layout.json`, references by name | The delivered "(Table 6)" pointed at the wrong table. |
| Review-board statement, author list | From the study team's parameters file (`--manuscript-params`) | Institution names and people are not stored in the package. |
| Software section with Python 3.10 | The run's own versions, cited through grateful_py | The delivered section was edited by hand. |
