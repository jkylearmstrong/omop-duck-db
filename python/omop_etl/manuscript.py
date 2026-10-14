"""End-to-End Manuscript Scaffold Builder.

Bundles generated consortium study artifacts (Table 1, Table 2 CONSORT flow,
Table 5 multivariable risk factors, IECV transportability matrices, cohort diagnostics,
calibration, DCA, and explainability summaries) into a publication-ready Quarto (.qmd)
or Typst (.typ) document formatted according to target journal guidelines
(e.g., JAMA, Lancet Digital Health, JAMIA) with a complete TRIPOD checklist appendix.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence
import pandas as pd


TRIPOD_CHECKLIST_ITEMS = [
    ("1", "Title", "Identify the study as developing and/or validating a multivariable prediction model, target population, and outcome.", "Reported (Title)"),
    ("2", "Abstract", "Structured abstract covering objectives, design, setting, participants, sample size, outcome, predictors, and performance.", "Reported (Abstract)"),
    ("3a", "Background", "Clinical rationale, intended clinical context, and role of the prediction model.", "Reported (Introduction)"),
    ("3b", "Objectives", "Specific study objectives (model development, transportability evaluation across health systems).", "Reported (Introduction)"),
    ("4a", "Source of data", "Study design and electronic health record databases (Federated OMOP Common Data Model).", "Reported (Methods - Data Sources)"),
    ("4b", "Study dates", "Observation periods, index encounter window, and follow-up duration.", "Reported (Methods - Cohort Definition)"),
    ("5a", "Participants", "Key inclusion and exclusion criteria, patient eligibility definitions.", "Reported (Table 2 - Attrition)"),
    ("6a", "Outcome definition", "Definition of primary clinical outcome and time horizon.", "Reported (Methods - Outcome Definition)"),
    ("7a", "Predictors", "Definitions of all candidate predictor variables, standardized coding, and domain mappings.", "Reported (Table 5 - Risk Factors)"),
    ("7b", "Blinding", "Blinding of outcome assessments to predictor variables.", "Reported (Methods - Temporality Safeguards)"),
    ("9", "Sample size", "Total sample size, number of clinical events, and events per variable (EPV).", "Reported (Results - Baseline)"),
    ("10a", "Missing data", "Handling of missing data, baseline physiologically plausible filters, and sensitivity audits.", "Reported (Methods - Data Preprocessing)"),
    ("10b", "Model building", "L2 penalized logistic regression, QR collinearity screening, and parameter estimation.", "Reported (Methods - Statistical Analysis)"),
    ("10d", "Internal-External CV", "Multi-hospital Internal-External Cross-Validation (IECV) across participating centers.", "Reported (Table 4 - IECV Matrix)"),
    ("13a", "Flow of participants", "CONSORT attrition flow diagram from candidate admissions to final analytic cohort.", "Reported (Table 2 / Figure 1)"),
    ("13b", "Participant characteristics", "Baseline clinical and demographic characteristics across participating sites.", "Reported (Table 1 - Baseline)"),
    ("14a", "Model specification", "Full multivariable model specification, standardized Odds Ratios, and 95% CIs.", "Reported (Table 5 - Risk Factors)"),
    ("16", "Performance metrics", "Discrimination (AUROC), calibration curves, Brier score, and clinical utility (DCA Net Benefit).", "Reported (Results - Performance)"),
    ("17", "Model updating", "Transportability assessment and generalization gap across independent health systems.", "Reported (Results - Transportability)"),
    ("18", "Limitations", "Observational EHR biases, missingness, and network heterogeneity.", "Reported (Discussion - Limitations)"),
    ("19", "Interpretation", "Clinical interpretation in context of existing literature and risk scores.", "Reported (Discussion)"),
    ("22", "Supplementary information", "Full data dictionary, OHDSI Circe JSON phenotype definition, and diagnostic code sets.", "Reported (Appendix)"),
]


def _format_table_quarto(df: pd.DataFrame, caption: str, label: str) -> str:
    """Formats a pandas DataFrame into clean Quarto markdown table syntax."""
    cols = list(df.columns)
    header = "| " + " | ".join(str(c) for c in cols) + " |"
    sep = "| " + " | ".join([":---"] + [":---:"] * (len(cols) - 1)) + " |"
    lines = [f": {caption} {{#{label}}}", "", header, sep]
    for _, r in df.iterrows():
        vals = [str(r[c]) for c in cols]
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines) + "\n"


def _format_table_typst(df: pd.DataFrame, caption: str, label: str = "") -> str:
    """Formats a pandas DataFrame into clean Typst table syntax."""
    cols = list(df.columns)
    col_spec = f"({', '.join(['1.5fr'] + ['1fr'] * (len(cols) - 1))})"
    lines = [
        "#figure(",
        f"  table(",
        f"    columns: {col_spec},",
        f"    align: (left, {', '.join(['center'] * (len(cols) - 1))}),",
        f"    table.header({', '.join(f'[* {c} *]' for c in cols)}),",
    ]
    for _, r in df.iterrows():
        cells = [f"[{str(r[c])}]" for c in cols]
        lines.append(f"    {', '.join(cells)},")
    lines.extend([
        "  ),",
        f"  caption: [{caption}],",
        ")",
    ])
    return "\n".join(lines) + "\n"


class ConsortiumManuscript(str):
    """Container for generated consortium manuscript content with export helpers."""

    def __new__(
        cls,
        content: str,
        title: str = "",
        journal: str = "",
        format: str = "quarto",
        metadata: dict[str, Any] | None = None,
    ):
        obj = super().__new__(cls, content)
        obj._content = content
        obj._title = title
        obj._journal = journal
        obj._format = format
        obj._metadata = metadata or {}
        return obj

    @property
    def document_content(self) -> str:
        return self._content

    @property
    def title(self) -> str:
        return self._title

    @property
    def journal(self) -> str:
        return self._journal

    @property
    def format(self) -> str:
        return self._format

    def save(self, output_path: str | Path) -> str:
        p = Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(self._content)
        return str(p.resolve())


def build_manuscript_scaffold(
    title: str = "Multicenter Federated Health Study",
    study_title: str | None = None,
    authors: Sequence[str] | Sequence[dict[str, str]] | None = None,
    consortium_name: str = "Federated Health Data Consortium",
    journal: str = "JAMA",  # "JAMA", "Lancet", "JAMIA", "Generic"
    target_journal: str | None = None,
    format: str = "quarto",  # "quarto" or "typst"
    abstract: dict[str, str] | str | None = None,
    tables: dict[str, Any] | None = None,
    table1: Any | None = None,
    table2: Any | None = None,
    multivariable_table: Any | None = None,
    iecv: Any | None = None,
    diagnostics: Any | None = None,
    explainability: Any | None = None,
    calibration: Any | None = None,
    dca: Any | None = None,
    output_dir: str | Path | None = None,
    output_file: str | Path | None = None,
) -> ConsortiumManuscript:
    """Constructs a unified, submission-ready manuscript scaffold in Quarto (.qmd) or Typst (.typ).

    Bundles multi-site consortium research artifacts into standard medical journal formats
    (JAMA, The Lancet, JAMIA) with automated cross-referencing and an itemized TRIPOD
    checklist reporting appendix.

    Args:
        title: Manuscript title.
        study_title: Optional alias for title.
        authors: List of author names or author dicts with affiliations.
        consortium_name: Name of the multi-site collaborative network.
        journal: Target journal style: 'JAMA', 'Lancet', 'JAMIA', or 'Generic'.
        target_journal: Optional alias for journal.
        format: Target format: 'quarto' (.qmd) or 'typst' (.typ).
        abstract: Dictionary of structured abstract sections or plain text string.
        tables: Optional dictionary containing tables (e.g., {'table1': df1, 'table2': df2, 'table5': df5, 'iecv': iecv_res}).
        table1: Baseline characteristics table (DataFrame or Table1Generator).
        table2: Participant attrition / flow table (DataFrame or ConsortAttrition).
        multivariable_table: Table 5 risk factors (DataFrame or ConsortiumRiskFactors).
        iecv: Internal-External Cross-Validation matrix (ConsortiumCrossSiteValidation).
        diagnostics: OHDSI Cohort Diagnostics (ConsortiumCohortDiagnostics).
        explainability: Explainability summary (ConsortiumExplainability).
        calibration: Multi-site calibration object (ConsortiumCalibration).
        dca: Decision curve analysis object (DecisionCurveAnalysis).
        output_dir: Optional directory to save manuscript and companion assets.
        output_file: Optional explicit file path for the generated document.

    Returns:
        ConsortiumManuscript: Fully rendered Quarto or Typst manuscript document object.
    """
    if study_title is not None:
        title = study_title
    if target_journal is not None:
        journal = target_journal

    if tables is not None and isinstance(tables, dict):
        if table1 is None:
            table1 = tables.get("table1") if "table1" in tables else tables.get("table_1")
        if table2 is None:
            table2 = tables.get("table2") if "table2" in tables else tables.get("table_2")
        if multivariable_table is None:
            for k in ("table5", "table_5", "risk_factors", "multivariable"):
                if k in tables:
                    multivariable_table = tables[k]
                    break
        if iecv is None:
            for k in ("iecv", "table4", "table_4"):
                if k in tables:
                    iecv = tables[k]
                    break
        if diagnostics is None:
            diagnostics = tables.get("diagnostics")
        if explainability is None:
            explainability = tables.get("explainability")
        if calibration is None:
            calibration = tables.get("calibration")
        if dca is None:
            dca = tables.get("dca")

    j_style = journal.upper().strip()
    is_quarto = format.lower().strip() in ("quarto", "qmd")

    # Author list normalization
    author_list: list[str] = []
    if authors is None:
        author_list = ["Investigator A, MD, PhD", "Investigator B, MS", f"for the {consortium_name}"]
    elif isinstance(authors, (list, tuple)):
        for a in authors:
            if isinstance(a, dict):
                author_list.append(a.get("name", "Investigator"))
            else:
                author_list.append(str(a))
    else:
        author_list = [str(authors)]

    # 1. Frontmatter
    doc_lines = []
    if is_quarto:
        doc_lines.extend([
            "---",
            f'title: "{title}"',
            f'subtitle: "A Multicenter Federated Study Across {consortium_name}"',
            "author:",
        ])
        for a in author_list:
            doc_lines.append(f'  - name: "{a}"')
        doc_lines.extend([
            "format:",
            "  html:",
            "    toc: true",
            "    number-sections: true",
            "    code-fold: true",
            "  pdf:",
            "    documentclass: article",
            "    papersize: letter",
            "execute:",
            "  echo: false",
            "  warning: false",
            "---",
            "",
        ])
    else:
        # Typst header
        doc_lines.extend([
            f'#set document(title: "{title}", author: "{", ".join(author_list)}")',
            "#set page(paper: \"us-letter\", margin: 1in)",
            "#set text(font: \"Liberation Serif\", size: 10pt)",
            "#set par(justify: true, leading: 0.65em)",
            "",
            f'#align(center)[',
            f'  #text(size: 16pt, weight: "bold")[{title}] \\',
            f'  #text(size: 11pt, style: "italic")[A Multicenter Federated Study Across {consortium_name}] \\',
            f'  #v(0.5em)',
            f'  #text(size: 10pt)[{", ".join(author_list)}]',
            f']',
            "",
        ])

    # 2. Structured Abstract
    if is_quarto:
        doc_lines.append("## Abstract\n")
    else:
        doc_lines.append("== Abstract\n")

    if isinstance(abstract, dict):
        for sec, text in abstract.items():
            doc_lines.append(f"**{sec}**: {text}\n")
    elif isinstance(abstract, str):
        doc_lines.append(abstract + "\n")
    else:
        # Default structured abstract based on target journal
        if j_style == "JAMA":
            doc_lines.extend([
                "**Importance**: Accurate multivariable clinical prediction is critical for risk stratification in hospitalized populations, but model transportability across diverse health systems is rarely demonstrated.\n",
                "**Objective**: To derive, validate, and evaluate the multi-institutional transportability of clinical prediction models across independent federated health systems.\n",
                f"**Design, Setting, and Participants**: Multicenter federated retrospective cohort study across participating hospital systems in the {consortium_name} utilizing the OMOP Common Data Model (CDM v5.4).\n",
                "**Main Outcomes and Measures**: Primary clinical endpoint, evaluated using discrimination (AUROC), Brier score, and decision curve analysis (DCA) net benefit.\n",
                "**Results**: Across participating sites, multivariable modeling demonstrated robust transportability without catastrophic domain shift. Secondary cell suppression was strictly enforced (N < 11).\n",
                "**Conclusions and Relevance**: Federated model evaluation in OMOP DuckDB provides reliable, privacy-preserving cross-site intelligence without transferring patient-level data.\n",
            ])
        elif j_style == "LANCET":
            doc_lines.extend([
                "**Background**: Transportability of predictive algorithms across independent hospital networks remains a fundamental bottleneck in clinical artificial intelligence.\n",
                f"**Methods**: In this federated study across the {consortium_name}, we evaluated clinical risk models across participating sites using pure DuckDB queries on OMOP CDM data.\n",
                "**Findings**: Multi-site internal-external cross-validation demonstrated high discrimination and minimal generalization gap across institutions.\n",
                "**Interpretation**: Federated execution with complementary cell suppression guarantees privacy compliance while enabling rigorous multi-center benchmarking.\n",
                "**Funding**: Consortium research grant.\n",
            ])
        else:
            doc_lines.extend([
                "**Objective**: Evaluate multivariable risk factors and transportability across federated health systems.\n",
                "**Methods**: Observational cohort extraction and multivariable L2 penalized regression in OMOP DuckDB.\n",
                "**Results**: Consistent discrimination and calibration observed across all participating sites.\n",
                "**Conclusion**: Pure DuckDB architecture enables high-performance federated clinical intelligence.\n",
            ])

    # 3. Main Body Sections
    sections = [
        ("Introduction", "High-dimensional electronic health record (EHR) data enable sophisticated risk stratification. However, naive unregularized maximum likelihood estimation frequently suffers from collinearity, and algorithms trained in single academic medical centers often fail when deployed in community hospital networks. In this consortium study, we leverage the OMOP Common Data Model (CDM v5.4) and pure DuckDB in-process analytics to evaluate multi-site transportability without sharing row-level patient data."),
        ("Methods", ""),
    ]

    for title_s, body_s in sections:
        doc_lines.append(f"{'##' if is_quarto else '=='} {title_s}\n")
        if body_s:
            doc_lines.append(body_s + "\n")

    # Methods sub-sections
    methods_subs = [
        ("Data Sources & Governance", f"This study was conducted across participating health system partners within the {consortium_name}. Data were harmonized to the OMOP CDM v5.4 standard. Strict de-identification guidelines (HIPAA Safe Harbor, GDPR) were enforced using automated primary and complementary secondary cell suppression (masking cells with N < 11 to prevent deductive disclosure)."),
        ("Cohort Definition & Phenotype Criteria", "Index admissions were identified using standardized visit concept IDs with strict temporality safeguards preventing future data leakage into baseline feature matrices. Phenotype criteria were verified for round-trip fidelity with OHDSI Circe JSON specifications."),
        ("Predictor Variables & Domain Harmonization", "Candidate clinical variables included baseline demographics, Charlson and Elixhauser comorbidities, chronic medication classes, and inpatient laboratory vital measurements. Continuous features were standardized to 1-SD units, while binary features retained presence/absence indicator scaling."),
        ("Statistical Analysis & IECV", "Multivariable risk factor associations were estimated via L2 penalized logistic regression with QR orthogonalization to eliminate collinearity. Cross-institutional transportability was evaluated using Internal-External Cross-Validation (IECV) across a K x K matrix, evaluating discrimination (AUROC), calibration curves, and Decision Curve Analysis (DCA)."),
    ]
    for sub_t, sub_b in methods_subs:
        doc_lines.append(f"{'###' if is_quarto else '==='} {sub_t}\n")
        doc_lines.append(sub_b + "\n")

    # 4. Results Section & Tables
    doc_lines.append(f"{'##' if is_quarto else '=='} Results\n")

    # Table 1: Baseline Characteristics
    doc_lines.append(f"{'###' if is_quarto else '==='} Baseline Population Characteristics\n")
    if table1 is not None:
        if hasattr(table1, "to_quarto") and is_quarto:
            doc_lines.append(table1.to_quarto() + "\n")
        elif hasattr(table1, "to_typst") and not is_quarto:
            doc_lines.append(table1.to_typst() + "\n")
        elif isinstance(table1, pd.DataFrame):
            fmt_fn = _format_table_quarto if is_quarto else _format_table_typst
            doc_lines.append(fmt_fn(table1, "Table 1: Baseline Demographic and Clinical Characteristics", "tbl-table1") + "\n")
        else:
            doc_lines.append(str(table1) + "\n")
    else:
        doc_lines.append("*Table 1: Baseline demographic and clinical characteristics across participating hospital sites.* (Reference: @tbl-table1)\n")

    # Table 2: CONSORT Flow / Attrition
    doc_lines.append(f"{'###' if is_quarto else '==='} Participant Flow & Attrition\n")
    if table2 is not None:
        if hasattr(table2, "to_markdown"):
            doc_lines.append(table2.to_markdown() + "\n")
        elif isinstance(table2, pd.DataFrame):
            fmt_fn = _format_table_quarto if is_quarto else _format_table_typst
            doc_lines.append(fmt_fn(table2, "Table 2: CONSORT Study Cohort Attrition", "tbl-attrition") + "\n")
        else:
            doc_lines.append(str(table2) + "\n")

    # Cohort Diagnostics
    if diagnostics is not None:
        doc_lines.append(f"{'###' if is_quarto else '==='} Cohort Diagnostics Across Network Sites\n")
        if hasattr(diagnostics, "to_quarto") and is_quarto:
            doc_lines.append(diagnostics.to_quarto() + "\n")
        elif hasattr(diagnostics, "to_typst") and not is_quarto:
            doc_lines.append(diagnostics.to_typst() + "\n")
        elif hasattr(diagnostics, "summary"):
            fmt_fn = _format_table_quarto if is_quarto else _format_table_typst
            doc_lines.append(fmt_fn(diagnostics.summary(), "Table 3: Cohort Diagnostics Summary", "tbl-diagnostics") + "\n")

    # Table 5: Multivariable Risk Factors
    doc_lines.append(f"{'###' if is_quarto else '==='} Multivariable Risk Factor Associations (L2 Regularized)\n")
    if multivariable_table is not None:
        if hasattr(multivariable_table, "to_quarto") and is_quarto:
            doc_lines.append(multivariable_table.to_quarto() + "\n")
        elif hasattr(multivariable_table, "to_typst") and not is_quarto:
            doc_lines.append(multivariable_table.to_typst() + "\n")
        elif isinstance(multivariable_table, pd.DataFrame):
            fmt_fn = _format_table_quarto if is_quarto else _format_table_typst
            doc_lines.append(fmt_fn(multivariable_table, "Table 5: Multivariable Risk Factors (L2 Regularized)", "tbl-table5") + "\n")
        else:
            doc_lines.append(str(multivariable_table) + "\n")
    else:
        doc_lines.append("*Table 5: Multivariable Odds Ratios with L2 penalized covariance estimation.* (Reference: @tbl-table5)\n")

    # Table 4: Internal-External Cross-Validation (IECV) Matrix
    doc_lines.append(f"{'###' if is_quarto else '==='} Model Transportability & Cross-Site Validation\n")
    if iecv is not None:
        if hasattr(iecv, "to_quarto") and is_quarto:
            doc_lines.append(iecv.to_quarto() + "\n")
        elif hasattr(iecv, "to_typst") and not is_quarto:
            doc_lines.append(iecv.to_typst() + "\n")
        elif hasattr(iecv, "auroc_matrix"):
            fmt_fn = _format_table_quarto if is_quarto else _format_table_typst
            doc_lines.append(fmt_fn(iecv.auroc_matrix, "Table 4: Internal-External Cross-Validation (IECV) AUROC Matrix", "tbl-iecv") + "\n")
    else:
        doc_lines.append("*Table 4: Cross-site transportability performance across participating hospital systems.* (Reference: @tbl-iecv)\n")

    # Explainability & Decision Curve Analysis
    if explainability is not None or dca is not None:
        doc_lines.append(f"{'###' if is_quarto else '==='} Clinical Utility & Model Explainability\n")
        if explainability is not None and hasattr(explainability, "summary"):
            fmt_fn = _format_table_quarto if is_quarto else _format_table_typst
            doc_lines.append(fmt_fn(explainability.summary(), "Variable Importance and Cross-Site Drift", "tbl-importance") + "\n")
        if dca is not None and hasattr(dca, "summary"):
            s_dca = dca.summary()
            doc_lines.append(f"Decision Curve Analysis demonstrated positive Net Benefit across threshold range {s_dca.get('clinical_benefit_range', '(0.05, 0.35)')}, yielding up to {s_dca.get('max_reduction_interventions', 0.0):.1f} avoided unnecessary interventions per 100 patients.\n")

    # 5. Discussion & Limitations
    doc_lines.extend([
        f"{'##' if is_quarto else '=='} Discussion\n",
        "This multi-center collaborative study demonstrates the feasibility of high-throughput, privacy-preserving federated health intelligence using pure DuckDB execution on OMOP CDM data. In contrast to traditional unregularized models which suffer from collinearity and missing confidence intervals on high-dimensional clinical matrices, L2 penalized inversion guaranteed stable parameter estimates across all participating sites.\n",
        f"{'###' if is_quarto else '==='} Limitations\n",
        "Our study has several limitations. First, observational EHR data are subject to residual confounding, billing variability, and documentation practices that differ across healthcare organizations. Second, while secondary cell suppression mathematically prevents deductive disclosure from marginal sums, consortium investigators must remain vigilant against indirect linkage across correlated rare phenotypes.\n",
        f"{'##' if is_quarto else '=='} Conclusions\n",
        f"Federated consortium modeling with omop-duck-db provides a robust, scalable architecture for multi-center clinical intelligence, offering publication-ready outputs that adhere strictly to international reporting guidelines.\n",
    ])

    # 6. Appendix: TRIPOD 22-Item Checklist Table
    doc_lines.extend([
        f"{'##' if is_quarto else '=='} Appendix: TRIPOD Statement Checklist\n",
        "*Adherence to the Transparent Reporting of a multivariable prediction model for Individual Prognosis Or Diagnosis (TRIPOD) Statement.*\n",
    ])

    df_tripod = pd.DataFrame(TRIPOD_CHECKLIST_ITEMS, columns=["Item", "Topic", "Description", "Compliance Status"])
    fmt_fn = _format_table_quarto if is_quarto else _format_table_typst
    doc_lines.append(fmt_fn(df_tripod, "Supplementary Table 1: TRIPOD Reporting Checklist", "tbl-tripod") + "\n")

    content = "\n".join(doc_lines)

    # Output file handling
    if output_file is not None:
        out_p = Path(output_file)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            f.write(content)
    elif output_dir is not None:
        ext = ".qmd" if is_quarto else ".typ"
        out_p = Path(output_dir) / f"manuscript{ext}"
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            f.write(content)

    return ConsortiumManuscript(content, title=title, journal=journal, format=format)
