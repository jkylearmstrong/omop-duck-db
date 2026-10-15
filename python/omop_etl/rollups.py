"""Extensible Hierarchical Rollups, Concept Pattern Matching, and Crosswalk Materialization.

Provides declarative, multi-domain hierarchical rollups across medications, conditions,
procedures, and laboratories with DuckDB-native execution, bidirectional hierarchy expansion,
dose form exclusions, and concept drift auditing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import duckdb
import pandas as pd

from omop_etl.integrity import (
    DEFAULT_ADMINISTERED_TYPE_CONCEPT_IDS,
    DEFAULT_DISPENSED_TYPE_CONCEPT_IDS,
    DEFAULT_PRESCRIBED_TYPE_CONCEPT_IDS,
)
from omop_etl.build_omop_cdm import _resource_path

__all__ = [
    "ConceptPattern",
    "HierarchicalRollup",
    "RollupEngine",
    "resolve_concept_set",
    "materialize_crosswalk",
    "audit_concept_drift",
    "compute_file_sha256",
]


def compute_file_sha256(path: str | Path) -> str:
    """Compute hex SHA-256 digest of a file on disk."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _find_column(df: pd.DataFrame, col_spec: str | int | None, required: bool = True) -> Any:
    """Find column in dataframe matching exact name, index, or case-insensitive name."""
    if col_spec is None:
        return None
    if isinstance(col_spec, int):
        if col_spec in df.columns:
            return col_spec
        if 0 <= col_spec < len(df.columns):
            return df.columns[col_spec]
        if required:
            raise KeyError(f"Column index {col_spec} out of bounds for table with {len(df.columns)} columns")
        return None
    if col_spec in df.columns:
        return col_spec
    col_norm = str(col_spec).strip().lower()
    for c in df.columns:
        if str(c).strip().lower() == col_norm:
            return c
    if str(col_spec).strip().isdigit():
        idx = int(str(col_spec).strip())
        if idx in df.columns:
            return idx
        if 0 <= idx < len(df.columns):
            return df.columns[idx]
    if required:
        raise KeyError(f"Column {col_spec!r} not found in columns: {list(df.columns)}")
    return None


def _clean_code_val(val: Any) -> str | None:
    """Normalize extracted concept code, stripping .0 artifact from numeric parsing."""
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _resolve_code_list_resource(name: str) -> Path:
    """Locate bundled clinical reference files in extdata/code_lists."""
    p_direct = Path(name)
    if p_direct.exists():
        return p_direct
    candidates = []
    base = name
    for ext in ("", ".csv", ".xlsx"):
        candidate_name = f"{base}{ext}" if not base.endswith(ext) or ext == "" else base
        candidates.append(_resource_path("extdata", "code_lists", candidate_name))
        candidates.append(Path(__file__).resolve().parent.parent.parent / "inst" / "extdata" / "code_lists" / candidate_name)
    for c in candidates:
        p = Path(c)
        if p.exists():
            return p
    raise FileNotFoundError(f"Bundled code list {name!r} not found. Searched under inst/extdata/code_lists/")


def _resolve_vocab_table(con: duckdb.DuckDBPyConnection | Any, name: str, vocab_db: str | None = None) -> str:
    """Resolve reachable vocabulary table (main, attached central_vocab, or custom schema)."""
    candidates = []
    if vocab_db:
        candidates.extend([f"{vocab_db}.{name}", f"{vocab_db}.main.{name}"])
    candidates.extend([name, f"central_vocab.main.{name}", f"central_vocab.{name}"])
    for ref in candidates:
        try:
            if con.execute(f"SELECT 1 FROM {ref} LIMIT 1").fetchone() is not None:
                return ref
        except (duckdb.CatalogException, duckdb.BinderException):
            continue
    raise ValueError(f"Vocabulary table {name!r} is not reachable on this connection.")


@dataclass
class ConceptPattern:
    """Pattern specification for concept code and name matching."""
    vocabulary_id: str | None = None
    code_pattern: str | None = None
    name_pattern: str | None = None
    domain_id: str | None = None
    regex: bool = False
    expand: str | None = None  # None | 'descendants' | 'ancestors'
    target_vocabulary: str | None = None
    include_retired: bool = False
    standard_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ConceptPattern:
        return cls(
            vocabulary_id=d.get("vocabulary_id"),
            code_pattern=d.get("code_pattern"),
            name_pattern=d.get("name_pattern"),
            domain_id=d.get("domain_id"),
            regex=bool(d.get("regex", False)),
            expand=d.get("expand"),
            target_vocabulary=d.get("target_vocabulary"),
            include_retired=bool(d.get("include_retired", False)),
            standard_only=bool(d.get("standard_only", False)),
        )


def resolve_concept_set(
    con: duckdb.DuckDBPyConnection | Any,
    patterns: Sequence[ConceptPattern | Mapping[str, Any] | str],
    vocab_db: str | None = None,
    standard_only: bool = False,
    expand: str | None = None,
    include_retired: bool = False,
) -> pd.DataFrame:
    """Resolve OMOP concept sets using code/name wildcard patterns, regexes, and hierarchy expansions.

    Args:
        con: Active DuckDB connection.
        patterns: Sequence of pattern objects, dicts, or strings (e.g. 'ATC:A10B%').
        vocab_db: Attached vocabulary schema/database name.
        standard_only: If True, filters output to standard OMOP concepts ('S').
        expand: Global hierarchy expansion ('descendants' | 'ancestors' | None).
        include_retired: If True, performs backward 'Maps to' lookup for deprecated concepts.

    Returns:
        pd.DataFrame: Resolved concepts table.
    """
    if not patterns:
        return pd.DataFrame(columns=[
            "concept_id", "concept_name", "domain_id", "vocabulary_id",
            "concept_class_id", "standard_concept", "concept_code",
            "valid_start_date", "valid_end_date", "invalid_reason", "matched_pattern"
        ])

    concept_table = _resolve_vocab_table(con, "concept", vocab_db)
    concept_ancestor = None
    concept_relationship = None
    try:
        concept_ancestor = _resolve_vocab_table(con, "concept_ancestor", vocab_db)
    except Exception:
        pass
    try:
        concept_relationship = _resolve_vocab_table(con, "concept_relationship", vocab_db)
    except Exception:
        pass

    normalized_patterns: list[ConceptPattern] = []
    for p in patterns:
        if isinstance(p, ConceptPattern):
            normalized_patterns.append(p)
        elif isinstance(p, Mapping):
            normalized_patterns.append(ConceptPattern.from_dict(p))
        elif isinstance(p, str):
            if ":" in p:
                vocab, pat = p.split(":", 1)
                normalized_patterns.append(ConceptPattern(vocabulary_id=vocab.strip(), code_pattern=pat.strip()))
            else:
                normalized_patterns.append(ConceptPattern(code_pattern=p.strip()))

    ctes = []
    select_unions = []

    for idx, pat in enumerate(normalized_patterns):
        conds = []
        if pat.vocabulary_id:
            conds.append(f"c.vocabulary_id = '{pat.vocabulary_id}'")
        if pat.domain_id:
            conds.append(f"c.domain_id = '{pat.domain_id}'")
        if pat.code_pattern:
            if pat.regex:
                conds.append(f"regexp_matches(c.concept_code, '{pat.code_pattern}')")
            elif "%" in pat.code_pattern or "_" in pat.code_pattern:
                conds.append(f"c.concept_code ILIKE '{pat.code_pattern}'")
            else:
                conds.append(f"c.concept_code = '{pat.code_pattern}'")
        if pat.name_pattern:
            if pat.regex:
                conds.append(f"regexp_matches(c.concept_name, '{pat.name_pattern}')")
            elif "%" in pat.name_pattern or "_" in pat.name_pattern:
                conds.append(f"c.concept_name ILIKE '{pat.name_pattern}'")
            else:
                conds.append(f"c.concept_name = '{pat.name_pattern}'")

        if pat.standard_only or standard_only:
            conds.append("c.standard_concept = 'S'")

        where_clause = " AND ".join(conds) if conds else "1=1"
        pat_label = pat.code_pattern or pat.name_pattern or f"pattern_{idx}"
        cte_name = f"seed_{idx}"

        ctes.append(f"""
            {cte_name} AS (
                SELECT c.*, '{pat_label}' AS matched_pattern
                FROM {concept_table} c
                WHERE {where_clause}
            )
        """)

        eff_expand = pat.expand or expand
        eff_retired = pat.include_retired or include_retired

        if eff_expand == "descendants" and concept_ancestor:
            target_filter = f"AND c_desc.vocabulary_id = '{pat.target_vocabulary}'" if pat.target_vocabulary else ""
            exp_cte = f"exp_{idx}"
            ctes.append(f"""
                {exp_cte} AS (
                    SELECT 
                        c_desc.concept_id, c_desc.concept_name, c_desc.domain_id, c_desc.vocabulary_id,
                        c_desc.concept_class_id, c_desc.standard_concept, c_desc.concept_code,
                        c_desc.valid_start_date, c_desc.valid_end_date, c_desc.invalid_reason,
                        s.matched_pattern
                    FROM {cte_name} s
                    JOIN {concept_ancestor} ca ON s.concept_id = ca.ancestor_concept_id
                    JOIN {concept_table} c_desc ON ca.descendant_concept_id = c_desc.concept_id
                    WHERE 1=1 {target_filter}
                )
            """)
            current_cte = exp_cte
        elif eff_expand == "ancestors" and concept_ancestor:
            target_filter = f"AND c_anc.vocabulary_id = '{pat.target_vocabulary}'" if pat.target_vocabulary else ""
            exp_cte = f"exp_{idx}"
            ctes.append(f"""
                {exp_cte} AS (
                    SELECT 
                        c_anc.concept_id, c_anc.concept_name, c_anc.domain_id, c_anc.vocabulary_id,
                        c_anc.concept_class_id, c_anc.standard_concept, c_anc.concept_code,
                        c_anc.valid_start_date, c_anc.valid_end_date, c_anc.invalid_reason,
                        s.matched_pattern
                    FROM {cte_name} s
                    JOIN {concept_ancestor} ca ON s.concept_id = ca.descendant_concept_id
                    JOIN {concept_table} c_anc ON ca.ancestor_concept_id = c_anc.concept_id
                    WHERE 1=1 {target_filter}
                )
            """)
            current_cte = exp_cte
        else:
            current_cte = cte_name

        if eff_retired and concept_relationship:
            ret_cte = f"ret_{idx}"
            ctes.append(f"""
                {ret_cte} AS (
                    SELECT * FROM {current_cte}
                    UNION ALL
                    SELECT 
                        c_src.concept_id, c_src.concept_name, c_src.domain_id, c_src.vocabulary_id,
                        c_src.concept_class_id, c_src.standard_concept, c_src.concept_code,
                        c_src.valid_start_date, c_src.valid_end_date, c_src.invalid_reason,
                        curr.matched_pattern
                    FROM {current_cte} curr
                    JOIN {concept_relationship} cr ON curr.concept_id = cr.concept_id_2 AND cr.relationship_id = 'Maps to'
                    JOIN {concept_table} c_src ON cr.concept_id_1 = c_src.concept_id
                    WHERE c_src.invalid_reason IS NOT NULL OR c_src.standard_concept IS NULL
                )
            """)
            current_cte = ret_cte

        select_unions.append(f"SELECT * FROM {current_cte}")

    full_sql = f"""
        WITH {", ".join(ctes)}
        SELECT DISTINCT 
            concept_id, concept_name, domain_id, vocabulary_id,
            concept_class_id, standard_concept, concept_code,
            valid_start_date, valid_end_date, invalid_reason, matched_pattern
        FROM (
            {" UNION ALL ".join(select_unions)}
        )
        ORDER BY vocabulary_id, concept_code
    """
    return con.execute(full_sql).df()


class HierarchicalRollup:
    """Declarative, multi-domain hierarchical rollup definition."""

    def __init__(
        self,
        name: str,
        domain: str = "Drug",
        root_vocabulary: str | None = None,
        roots: Sequence[str | int] | None = None,
        target_vocabulary: str | None = None,
        expansion_direction: str = "descendants",
        include_retired_maps_to: bool = True,
        exclude_dose_forms: Sequence[str] | None = None,
        combination_policy: str = "attribute_all",
        rules: Sequence[ConceptPattern | Mapping[str, Any] | str] | None = None,
        source_file: str | None = None,
        source_file_hash: str | None = None,
    ):
        self.name = name
        self.domain = domain
        self.root_vocabulary = root_vocabulary
        self.roots = list(roots or [])
        self.target_vocabulary = target_vocabulary or ("RxNorm" if domain == "Drug" else None)
        self.expansion_direction = expansion_direction
        self.include_retired_maps_to = include_retired_maps_to
        self.exclude_dose_forms = list(exclude_dose_forms or [])
        self.combination_policy = combination_policy
        self.source_file = source_file
        self.source_file_hash = source_file_hash
        self.rules: list[ConceptPattern] = []

        if rules:
            for r in rules:
                if isinstance(r, ConceptPattern):
                    self.rules.append(r)
                elif isinstance(r, Mapping):
                    self.rules.append(ConceptPattern.from_dict(r))
                elif isinstance(r, str):
                    if ":" in r:
                        vocab, pat = r.split(":", 1)
                        self.rules.append(ConceptPattern(vocabulary_id=vocab.strip(), code_pattern=pat.strip()))
                    else:
                        self.rules.append(ConceptPattern(code_pattern=r.strip()))
        elif self.roots:
            for root in self.roots:
                pat = str(root)
                self.rules.append(ConceptPattern(
                    vocabulary_id=self.root_vocabulary,
                    code_pattern=pat,
                    domain_id=self.domain,
                    expand=self.expansion_direction,
                    target_vocabulary=self.target_vocabulary,
                    include_retired=self.include_retired_maps_to,
                ))

    def to_dict(self) -> dict[str, Any]:
        d = {
            "name": self.name,
            "domain": self.domain,
            "root_vocabulary": self.root_vocabulary,
            "roots": self.roots,
            "target_vocabulary": self.target_vocabulary,
            "expansion_direction": self.expansion_direction,
            "include_retired_maps_to": self.include_retired_maps_to,
            "exclude_dose_forms": self.exclude_dose_forms,
            "combination_policy": self.combination_policy,
            "rules": [r.to_dict() for r in self.rules],
        }
        if self.source_file is not None:
            d["source_file"] = self.source_file
        if self.source_file_hash is not None:
            d["source_file_hash"] = self.source_file_hash
        return d

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), **kwargs)

    def spec_hash(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> HierarchicalRollup:
        rules = [ConceptPattern.from_dict(r) for r in d.get("rules", [])]
        return cls(
            name=d["name"],
            domain=d.get("domain", "Drug"),
            root_vocabulary=d.get("root_vocabulary"),
            roots=d.get("roots"),
            target_vocabulary=d.get("target_vocabulary"),
            expansion_direction=d.get("expansion_direction", "descendants"),
            include_retired_maps_to=bool(d.get("include_retired_maps_to", True)),
            exclude_dose_forms=d.get("exclude_dose_forms"),
            combination_policy=d.get("combination_policy", "attribute_all"),
            rules=rules,
            source_file=d.get("source_file"),
            source_file_hash=d.get("source_file_hash"),
        )

    @classmethod
    def from_json(cls, s: str) -> HierarchicalRollup:
        return cls.from_dict(json.loads(s))

    @classmethod
    def from_excel(
        cls,
        path: str | Path,
        sheet_name: str | int | None = 0,
        name: str | None = None,
        code_col: str | int = "RXNORM_CUI",
        class_col: str | int | None = None,
        vocabulary_col: str | int | None = None,
        vocabulary_id: str | None = None,
        root_vocabulary: str | None = None,
        domain: str = "Drug",
        target_vocabulary: str | None = None,
        expand: str | None = "descendants",
        expansion_direction: str | None = None,
        include_retired_maps_to: bool = True,
        exclude_dose_forms: Sequence[str] | None = None,
        combination_policy: str = "attribute_all",
        header: bool | int | None = True,
        **kwargs,
    ) -> HierarchicalRollup:
        """Instantiate a HierarchicalRollup directly from an Excel worksheet."""
        p = Path(path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Excel file not found at {path}")
        fhash = compute_file_sha256(p)
        hdr = 0 if header is True else (None if header is False else header)
        df = pd.read_excel(p, sheet_name=sheet_name, header=hdr, **kwargs)

        c_code = _find_column(df, code_col, required=True)
        c_class = _find_column(df, class_col, required=False) if class_col is not None else None
        c_vocab = _find_column(df, vocabulary_col, required=False) if vocabulary_col is not None else None

        if c_class is not None and name is not None:
            match_mask = df[c_class].astype(str).str.strip().str.lower() == str(name).strip().lower()
            if not match_mask.any():
                norm_target = re.sub(r"[- _]+", "", str(name).lower())
                match_mask = df[c_class].astype(str).apply(lambda s: re.sub(r"[- _]+", "", str(s).lower())) == norm_target
            df = df[match_mask]

        eff_name = name or (str(sheet_name) if sheet_name is not None and sheet_name != 0 else p.stem)
        eff_expand = expansion_direction if expansion_direction is not None else expand
        eff_vocab = vocabulary_id or root_vocabulary or ("RxNorm" if domain == "Drug" else None)

        rules = []
        roots = []
        for _, row in df.iterrows():
            c_val = _clean_code_val(row[c_code])
            if not c_val:
                continue
            roots.append(c_val)
            row_vocab = str(row[c_vocab]).strip() if c_vocab is not None and not pd.isna(row[c_vocab]) else eff_vocab
            rules.append(ConceptPattern(
                vocabulary_id=row_vocab,
                code_pattern=c_val,
                domain_id=domain,
                expand=eff_expand,
                target_vocabulary=target_vocabulary or ("RxNorm" if domain == "Drug" else None),
                include_retired=include_retired_maps_to,
            ))

        return cls(
            name=eff_name,
            domain=domain,
            root_vocabulary=eff_vocab,
            roots=roots,
            target_vocabulary=target_vocabulary,
            expansion_direction=eff_expand or "descendants",
            include_retired_maps_to=include_retired_maps_to,
            exclude_dose_forms=exclude_dose_forms,
            combination_policy=combination_policy,
            rules=rules,
            source_file=str(p),
            source_file_hash=fhash,
        )

    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        name: str | None = None,
        code_col: str | int = "RXNORM_CUI",
        class_col: str | int | None = None,
        vocabulary_col: str | int | None = None,
        vocabulary_id: str | None = None,
        root_vocabulary: str | None = None,
        domain: str = "Drug",
        target_vocabulary: str | None = None,
        expand: str | None = "descendants",
        expansion_direction: str | None = None,
        include_retired_maps_to: bool = True,
        exclude_dose_forms: Sequence[str] | None = None,
        combination_policy: str = "attribute_all",
        header: bool | int | None = True,
        **kwargs,
    ) -> HierarchicalRollup:
        """Instantiate a HierarchicalRollup directly from a CSV file."""
        p = Path(path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"CSV file not found at {path}")
        fhash = compute_file_sha256(p)
        hdr = 0 if header is True else (None if header is False else header)
        df = pd.read_csv(p, header=hdr, **kwargs)

        c_code = _find_column(df, code_col, required=False)
        if c_code is None and "CODE" in df.columns:
            c_code = "CODE"
        if c_code is None and len(df.columns) > 1 and class_col is not None and isinstance(class_col, int) and class_col == 0:
            c_code = df.columns[1]
        if c_code is None:
            c_code = _find_column(df, code_col, required=True)

        c_class = _find_column(df, class_col, required=False) if class_col is not None else None
        c_vocab = _find_column(df, vocabulary_col, required=False) if vocabulary_col is not None else None

        if c_class is not None and name is not None:
            match_mask = df[c_class].astype(str).str.strip().str.lower() == str(name).strip().lower()
            if not match_mask.any():
                norm_target = re.sub(r"[- _]+", "", str(name).lower())
                match_mask = df[c_class].astype(str).apply(lambda s: re.sub(r"[- _]+", "", str(s).lower())) == norm_target
            df = df[match_mask]

        eff_name = name or p.stem
        eff_expand = expansion_direction if expansion_direction is not None else expand
        eff_vocab = vocabulary_id or root_vocabulary or ("RxNorm" if domain == "Drug" else None)

        rules = []
        roots = []
        for _, row in df.iterrows():
            c_val = _clean_code_val(row[c_code])
            if not c_val:
                continue
            roots.append(c_val)
            row_vocab = str(row[c_vocab]).strip() if c_vocab is not None and not pd.isna(row[c_vocab]) else eff_vocab
            rules.append(ConceptPattern(
                vocabulary_id=row_vocab,
                code_pattern=c_val,
                domain_id=domain,
                expand=eff_expand,
                target_vocabulary=target_vocabulary or ("RxNorm" if domain == "Drug" else None),
                include_retired=include_retired_maps_to,
            ))

        return cls(
            name=eff_name,
            domain=domain,
            root_vocabulary=eff_vocab,
            roots=roots,
            target_vocabulary=target_vocabulary,
            expansion_direction=eff_expand or "descendants",
            include_retired_maps_to=include_retired_maps_to,
            exclude_dose_forms=exclude_dose_forms,
            combination_policy=combination_policy,
            rules=rules,
            source_file=str(p),
            source_file_hash=fhash,
        )

    @classmethod
    def from_bundled(
        cls,
        name: str | None = None,
        bundle_name: str | None = None,
        resource_name: str | None = None,
        class_col: str | int | None = "CLASS",
        code_col: str | int | None = None,
        vocabulary_col: str | int | None = "VOCABULARY",
        vocabulary_id: str | None = None,
        domain: str = "Drug",
        expand: str | None = "descendants",
        include_retired_maps_to: bool = True,
        exclude_dose_forms: Sequence[str] | None = None,
        **kwargs,
    ) -> HierarchicalRollup:
        """Instantiate a HierarchicalRollup from bundled clinical code lists."""
        res_target = bundle_name or resource_name or "ederri_medications"
        kwargs.pop("bundle_name", None)
        kwargs.pop("resource_name", None)
        eff_name = name or "Biguanides"
        res_path = _resolve_code_list_resource(res_target)
        if res_path.suffix.lower() in (".xlsx", ".xls"):
            return cls.from_excel(
                path=res_path,
                name=eff_name,
                class_col=class_col,
                code_col=code_col or "CODE",
                vocabulary_col=vocabulary_col,
                vocabulary_id=vocabulary_id,
                domain=domain,
                expand=expand,
                include_retired_maps_to=include_retired_maps_to,
                exclude_dose_forms=exclude_dose_forms,
                **kwargs,
            )
        else:
            if code_col is None:
                df_prev = pd.read_csv(res_path, nrows=2)
                code_col = "RXNORM_CUI" if "RXNORM_CUI" in df_prev.columns else ("CODE" if "CODE" in df_prev.columns else 0)
            return cls.from_csv(
                path=res_path,
                name=name,
                class_col=class_col,
                code_col=code_col,
                vocabulary_col=vocabulary_col,
                vocabulary_id=vocabulary_id,
                domain=domain,
                expand=expand,
                include_retired_maps_to=include_retired_maps_to,
                exclude_dose_forms=exclude_dose_forms,
                **kwargs,
            )

    def materialize(
        self,
        con: duckdb.DuckDBPyConnection | Any,
        vocab_db: str | None = None,
        cache_path: str | Path | None = None,
    ) -> pd.DataFrame:
        """Materialize resolved crosswalk concepts in DuckDB and optionally cache to Parquet."""
        if cache_path:
            p = Path(cache_path)
            if p.exists():
                try:
                    cached_df = pd.read_parquet(p)
                    if (
                        not cached_df.empty
                        and "spec_hash" in cached_df.columns
                        and cached_df["spec_hash"].iloc[0] == self.spec_hash()
                    ):
                        if self.source_file and os.path.exists(self.source_file):
                            if compute_file_sha256(self.source_file) == self.source_file_hash:
                                return cached_df
                        else:
                            return cached_df
                except Exception:
                    pass

        df = resolve_concept_set(
            con,
            patterns=self.rules,
            vocab_db=vocab_db,
            expand=self.expansion_direction,
            include_retired=self.include_retired_maps_to,
        )

        if self.exclude_dose_forms and not df.empty:
            df = _filter_dose_forms(con, df, self.exclude_dose_forms, vocab_db=vocab_db)

        df["rollup_name"] = self.name
        df["combination_policy"] = self.combination_policy
        df["spec_hash"] = self.spec_hash()

        if cache_path:
            p = Path(cache_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(p, index=False)

        return df

    def extract_exposures(
        self,
        con: duckdb.DuckDBPyConnection | Any,
        site_db: str | None = None,
        cohort_table: str = "cohort",
        prescribed_types: Sequence[int] | None = None,
        include_combination_overlap: bool = True,
        vocab_db: str | None = None,
        **kwargs,
    ) -> pd.DataFrame:
        """Extract exposure records from site CDM table matched against materialized crosswalk."""
        crosswalk_df = self.materialize(con, vocab_db=vocab_db)
        if crosswalk_df.empty:
            return pd.DataFrame()

        de_prefix = f"{site_db}." if site_db else ""
        de_table = f"{de_prefix}drug_exposure"

        types = list(prescribed_types) if prescribed_types is not None else list(DEFAULT_PRESCRIBED_TYPE_CONCEPT_IDS)
        type_str = ", ".join(str(int(t)) for t in types)

        cids = sorted(crosswalk_df["concept_id"].dropna().astype(int).unique().tolist())
        con.execute("CREATE TEMPORARY TABLE IF NOT EXISTS _tmp_xwalk_ids (concept_id BIGINT)")
        con.execute("DELETE FROM _tmp_xwalk_ids")
        con.executemany("INSERT INTO _tmp_xwalk_ids VALUES (?)", [(c,) for c in cids])

        query = f"""
            SELECT 
                de.drug_exposure_id,
                de.person_id,
                de.drug_concept_id,
                de.drug_exposure_start_date,
                de.drug_exposure_end_date,
                de.drug_type_concept_id,
                '{self.name}' AS rollup_name
            FROM {de_table} de
            JOIN _tmp_xwalk_ids x ON de.drug_concept_id = x.concept_id
            WHERE de.drug_type_concept_id IN ({type_str})
        """
        return con.execute(query).df()


def _filter_dose_forms(
    con: duckdb.DuckDBPyConnection | Any,
    df: pd.DataFrame,
    exclude_dose_forms: Sequence[str],
    vocab_db: str | None = None,
) -> pd.DataFrame:
    """Exclude non-systemic / unwanted formulations using concept names and RxNorm dose form relationships."""
    if df.empty or not exclude_dose_forms:
        return df

    norm_forms = [f.lower().strip() for f in exclude_dose_forms]
    mask = pd.Series(True, index=df.index)

    for form in norm_forms:
        mask = mask & (~df["concept_name"].str.lower().str.contains(form, na=False))

    filtered_df = df[mask].copy()

    try:
        cr_table = _resolve_vocab_table(con, "concept_relationship", vocab_db)
        c_table = _resolve_vocab_table(con, "concept", vocab_db)
        cids = filtered_df["concept_id"].dropna().astype(int).tolist()
        if cids:
            con.execute("CREATE TEMPORARY TABLE IF NOT EXISTS _tmp_cand_concepts (concept_id BIGINT)")
            con.execute("DELETE FROM _tmp_cand_concepts")
            con.executemany("INSERT INTO _tmp_cand_concepts VALUES (?)", [(c,) for c in cids])
            form_clauses = " OR ".join(f"lower(c_form.concept_name) LIKE '%{f}%'" for f in norm_forms)
            excluded_cids_query = f"""
                SELECT DISTINCT cand.concept_id
                FROM _tmp_cand_concepts cand
                JOIN {cr_table} cr ON cand.concept_id = cr.concept_id_1 AND cr.relationship_id = 'RxNorm has dose form'
                JOIN {c_table} c_form ON cr.concept_id_2 = c_form.concept_id
                WHERE {form_clauses}
            """
            bad_cids = {r[0] for r in con.execute(excluded_cids_query).fetchall()}
            filtered_df = filtered_df[~filtered_df["concept_id"].isin(bad_cids)].copy()
    except Exception:
        pass

    return filtered_df


class RollupEngine:
    """Multi-rollup and multi-domain hierarchy execution engine."""

    def __init__(
        self,
        name: str = "custom_rollup_engine",
        rules: Sequence[ConceptPattern | Mapping[str, Any] | str] | None = None,
        exclude_forms: Sequence[str] | None = None,
        exclude_dose_forms: Sequence[str] | None = None,
        domain: str = "Drug",
        combination_policy: str = "attribute_all",
        rollups: Sequence[HierarchicalRollup] | Mapping[str, HierarchicalRollup] | None = None,
        source_files: Sequence[str] | None = None,
        source_file_hashes: Mapping[str, str] | None = None,
    ):
        self.name = name
        self.domain = domain
        self.exclude_dose_forms = list(exclude_forms or exclude_dose_forms or [])
        self.combination_policy = combination_policy
        self.source_files = list(source_files or [])
        self.source_file_hashes = dict(source_file_hashes or {})

        self.rollups: dict[str, HierarchicalRollup] = {}
        if rollups:
            if isinstance(rollups, Mapping):
                self.rollups = dict(rollups)
            else:
                for r in rollups:
                    self.rollups[r.name] = r
            self.rollup = next(iter(self.rollups.values())) if self.rollups else None
        else:
            self.rollup = HierarchicalRollup(
                name=name,
                domain=domain,
                rules=rules,
                exclude_dose_forms=self.exclude_dose_forms,
                combination_policy=combination_policy,
            )
            self.rollups[name] = self.rollup

    def _find_rollup_key(self, key: str) -> str | None:
        if key in self.rollups:
            return key
        norm_key = re.sub(r"[- _]+", "", str(key).lower())
        for k in self.rollups:
            if re.sub(r"[- _]+", "", str(k).lower()) == norm_key:
                return k
        return None

    def __getitem__(self, key: str) -> HierarchicalRollup:
        rk = self._find_rollup_key(key)
        if rk is not None:
            return self.rollups[rk]
        return self.rollups[key]

    def get(self, key: str, default: Any = None) -> HierarchicalRollup | Any:
        rk = self._find_rollup_key(key)
        if rk is not None:
            return self.rollups[rk]
        return default

    def __len__(self) -> int:
        return len(self.rollups)

    def __iter__(self):
        return iter(self.rollups.values())

    def __contains__(self, key: str) -> bool:
        return self._find_rollup_key(key) is not None

    @property
    def rollup_names(self) -> list[str]:
        return list(self.rollups.keys())

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": self.domain,
            "exclude_dose_forms": self.exclude_dose_forms,
            "combination_policy": self.combination_policy,
            "rollups": {k: v.to_dict() for k, v in self.rollups.items()},
            "source_files": self.source_files,
            "source_file_hashes": self.source_file_hashes,
        }

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), **kwargs)

    def spec_hash(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> RollupEngine:
        rollups = {k: HierarchicalRollup.from_dict(v) for k, v in d.get("rollups", {}).items()}
        return cls(
            name=d.get("name", "custom_rollup_engine"),
            domain=d.get("domain", "Drug"),
            exclude_dose_forms=d.get("exclude_dose_forms"),
            combination_policy=d.get("combination_policy", "attribute_all"),
            rollups=rollups,
            source_files=d.get("source_files"),
            source_file_hashes=d.get("source_file_hashes"),
        )

    @classmethod
    def from_json(cls, s: str) -> RollupEngine:
        return cls.from_dict(json.loads(s))

    @classmethod
    def from_excel(
        cls,
        path: str | Path,
        sheet_name: str | int | Sequence[str | int] | None = 0,
        class_col: str | int | None = "CLASS",
        code_col: str | int = "CODE",
        vocabulary_col: str | int | None = "VOCABULARY",
        vocabulary_id: str | None = None,
        root_vocabulary: str | None = None,
        domain: str = "Drug",
        target_vocabulary: str | None = None,
        expand: str | None = "descendants",
        expansion_direction: str | None = None,
        include_retired_maps_to: bool = True,
        exclude_dose_forms: Sequence[str] | None = None,
        combination_policy: str = "attribute_all",
        name: str | None = None,
        header: bool | int | None = True,
        **kwargs,
    ) -> RollupEngine:
        """Instantiate a RollupEngine from a multi-class or multi-sheet Excel workbook."""
        p = Path(path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Excel file not found at {path}")
        fhash = compute_file_sha256(p)
        hdr = 0 if header is True else (None if header is False else header)
        eff_expand = expansion_direction if expansion_direction is not None else expand
        eff_vocab = vocabulary_id or root_vocabulary or ("RxNorm" if domain == "Drug" else None)

        rollups: list[HierarchicalRollup] = []

        if sheet_name is None:
            sheets_dict = pd.read_excel(p, sheet_name=None, header=hdr, **kwargs)
            for sname, sdf in sheets_dict.items():
                c_class = _find_column(sdf, class_col, required=False) if class_col is not None else None
                c_code = _find_column(sdf, code_col, required=False)
                if c_code is None and "RXNORM_CUI" in sdf.columns:
                    c_code = "RXNORM_CUI"
                if c_code is None and len(sdf.columns) > 0:
                    c_code = sdf.columns[0]
                c_vocab = _find_column(sdf, vocabulary_col, required=False) if vocabulary_col is not None else None

                if c_class is not None:
                    for cls_val in sdf[c_class].dropna().unique():
                        cls_name = str(cls_val).strip()
                        if not cls_name:
                            continue
                        sub = sdf[sdf[c_class] == cls_val]
                        roots, rules = [], []
                        for _, row in sub.iterrows():
                            c_val = _clean_code_val(row[c_code])
                            if not c_val:
                                continue
                            roots.append(c_val)
                            row_vocab = str(row[c_vocab]).strip() if c_vocab is not None and not pd.isna(row[c_vocab]) else eff_vocab
                            rules.append(ConceptPattern(
                                vocabulary_id=row_vocab,
                                code_pattern=c_val,
                                domain_id=domain,
                                expand=eff_expand,
                                target_vocabulary=target_vocabulary or ("RxNorm" if domain == "Drug" else None),
                                include_retired=include_retired_maps_to,
                            ))
                        rollups.append(HierarchicalRollup(
                            name=cls_name,
                            domain=domain,
                            root_vocabulary=eff_vocab,
                            roots=roots,
                            target_vocabulary=target_vocabulary,
                            expansion_direction=eff_expand or "descendants",
                            include_retired_maps_to=include_retired_maps_to,
                            exclude_dose_forms=exclude_dose_forms,
                            combination_policy=combination_policy,
                            rules=rules,
                            source_file=str(p),
                            source_file_hash=fhash,
                        ))
                else:
                    roots, rules = [], []
                    for _, row in sdf.iterrows():
                        c_val = _clean_code_val(row[c_code])
                        if not c_val:
                            continue
                        roots.append(c_val)
                        row_vocab = str(row[c_vocab]).strip() if c_vocab is not None and not pd.isna(row[c_vocab]) else eff_vocab
                        rules.append(ConceptPattern(
                            vocabulary_id=row_vocab,
                            code_pattern=c_val,
                            domain_id=domain,
                            expand=eff_expand,
                            target_vocabulary=target_vocabulary or ("RxNorm" if domain == "Drug" else None),
                            include_retired=include_retired_maps_to,
                        ))
                    rollups.append(HierarchicalRollup(
                        name=str(sname).strip(),
                        domain=domain,
                        root_vocabulary=eff_vocab,
                        roots=roots,
                        target_vocabulary=target_vocabulary,
                        expansion_direction=eff_expand or "descendants",
                        include_retired_maps_to=include_retired_maps_to,
                        exclude_dose_forms=exclude_dose_forms,
                        combination_policy=combination_policy,
                        rules=rules,
                        source_file=str(p),
                        source_file_hash=fhash,
                    ))
        else:
            df = pd.read_excel(p, sheet_name=sheet_name, header=hdr, **kwargs)
            c_code = _find_column(df, code_col, required=False)
            if c_code is None and "RXNORM_CUI" in df.columns:
                c_code = "RXNORM_CUI"
            if c_code is None and "CODE" in df.columns:
                c_code = "CODE"
            if c_code is None:
                c_code = _find_column(df, code_col, required=True)
            c_class = _find_column(df, class_col, required=False) if class_col is not None else None
            c_vocab = _find_column(df, vocabulary_col, required=False) if vocabulary_col is not None else None

            if c_class is not None:
                for cls_val in df[c_class].dropna().unique():
                    cls_name = str(cls_val).strip()
                    if not cls_name:
                        continue
                    sub = df[df[c_class] == cls_val]
                    roots, rules = [], []
                    for _, row in sub.iterrows():
                        c_val = _clean_code_val(row[c_code])
                        if not c_val:
                            continue
                        roots.append(c_val)
                        row_vocab = str(row[c_vocab]).strip() if c_vocab is not None and not pd.isna(row[c_vocab]) else eff_vocab
                        rules.append(ConceptPattern(
                            vocabulary_id=row_vocab,
                            code_pattern=c_val,
                            domain_id=domain,
                            expand=eff_expand,
                            target_vocabulary=target_vocabulary or ("RxNorm" if domain == "Drug" else None),
                            include_retired=include_retired_maps_to,
                        ))
                    rollups.append(HierarchicalRollup(
                        name=cls_name,
                        domain=domain,
                        root_vocabulary=eff_vocab,
                        roots=roots,
                        target_vocabulary=target_vocabulary,
                        expansion_direction=eff_expand or "descendants",
                        include_retired_maps_to=include_retired_maps_to,
                        exclude_dose_forms=exclude_dose_forms,
                        combination_policy=combination_policy,
                        rules=rules,
                        source_file=str(p),
                        source_file_hash=fhash,
                    ))
            else:
                roots, rules = [], []
                for _, row in df.iterrows():
                    c_val = _clean_code_val(row[c_code])
                    if not c_val:
                        continue
                    roots.append(c_val)
                    row_vocab = str(row[c_vocab]).strip() if c_vocab is not None and not pd.isna(row[c_vocab]) else eff_vocab
                    rules.append(ConceptPattern(
                        vocabulary_id=row_vocab,
                        code_pattern=c_val,
                        domain_id=domain,
                        expand=eff_expand,
                        target_vocabulary=target_vocabulary or ("RxNorm" if domain == "Drug" else None),
                        include_retired=include_retired_maps_to,
                    ))
                rollups.append(HierarchicalRollup(
                    name=name or str(sheet_name or p.stem),
                    domain=domain,
                    root_vocabulary=eff_vocab,
                    roots=roots,
                    target_vocabulary=target_vocabulary,
                    expansion_direction=eff_expand or "descendants",
                    include_retired_maps_to=include_retired_maps_to,
                    exclude_dose_forms=exclude_dose_forms,
                    combination_policy=combination_policy,
                    rules=rules,
                    source_file=str(p),
                    source_file_hash=fhash,
                ))

        engine_name = name or (str(sheet_name) if sheet_name is not None and sheet_name != 0 else p.stem)
        return cls(
            name=engine_name,
            domain=domain,
            rollups=rollups,
            exclude_dose_forms=exclude_dose_forms,
            combination_policy=combination_policy,
            source_files=[str(p)],
            source_file_hashes={str(p): fhash},
        )

    @classmethod
    def from_csvs(
        cls,
        paths: Sequence[str | Path],
        class_col: str | int | None = "CLASS",
        code_col: str | int = "CODE",
        vocabulary_col: str | int | None = None,
        vocabulary_id: str | None = None,
        root_vocabulary: str | None = None,
        domain: str = "Drug",
        target_vocabulary: str | None = None,
        expand: str | None = "descendants",
        expansion_direction: str | None = None,
        include_retired_maps_to: bool = True,
        exclude_dose_forms: Sequence[str] | None = None,
        combination_policy: str = "attribute_all",
        name: str | None = None,
        header: bool | int | None = True,
        **kwargs,
    ) -> RollupEngine:
        """Instantiate a RollupEngine from a sequence of CSV files."""
        source_files = []
        source_file_hashes = {}
        class_dict: dict[str, dict[str, Any]] = {}
        eff_expand = expansion_direction if expansion_direction is not None else expand
        eff_vocab = vocabulary_id or root_vocabulary or ("RxNorm" if domain == "Drug" else None)

        for p_in in paths:
            p = Path(p_in).resolve()
            if not p.exists():
                raise FileNotFoundError(f"CSV file not found at {p_in}")
            p_str = str(p)
            source_files.append(p_str)
            source_file_hashes[p_str] = compute_file_sha256(p)

            hdr = 0 if header is True else (None if header is False else header)
            df = pd.read_csv(p, header=hdr, **kwargs)

            c_code = _find_column(df, code_col, required=False)
            if c_code is None and "RXNORM_CUI" in df.columns:
                c_code = "RXNORM_CUI"
            if c_code is None and "CODE" in df.columns:
                c_code = "CODE"
            if c_code is None:
                if len(df.columns) > 1 and class_col is not None and isinstance(class_col, int) and class_col == 0:
                    c_code = df.columns[1]
                else:
                    c_code = _find_column(df, code_col, required=True)

            c_class = _find_column(df, class_col, required=False) if class_col is not None else None
            c_vocab = _find_column(df, vocabulary_col, required=False) if vocabulary_col is not None else None

            if c_class is not None:
                for cls_val in df[c_class].dropna().unique():
                    cls_name = str(cls_val).strip()
                    if not cls_name:
                        continue
                    if cls_name not in class_dict:
                        class_dict[cls_name] = {"roots": [], "rules": []}
                    sub = df[df[c_class] == cls_val]
                    for _, row in sub.iterrows():
                        c_val = _clean_code_val(row[c_code])
                        if not c_val:
                            continue
                        class_dict[cls_name]["roots"].append(c_val)
                        row_vocab = str(row[c_vocab]).strip() if c_vocab is not None and not pd.isna(row[c_vocab]) else eff_vocab
                        class_dict[cls_name]["rules"].append(ConceptPattern(
                            vocabulary_id=row_vocab,
                            code_pattern=c_val,
                            domain_id=domain,
                            expand=eff_expand,
                            target_vocabulary=target_vocabulary or ("RxNorm" if domain == "Drug" else None),
                            include_retired=include_retired_maps_to,
                        ))
            else:
                cls_name = p.stem
                if cls_name not in class_dict:
                    class_dict[cls_name] = {"roots": [], "rules": []}
                for _, row in df.iterrows():
                    c_val = _clean_code_val(row[c_code])
                    if not c_val:
                        continue
                    class_dict[cls_name]["roots"].append(c_val)
                    row_vocab = str(row[c_vocab]).strip() if c_vocab is not None and not pd.isna(row[c_vocab]) else eff_vocab
                    class_dict[cls_name]["rules"].append(ConceptPattern(
                        vocabulary_id=row_vocab,
                        code_pattern=c_val,
                        domain_id=domain,
                        expand=eff_expand,
                        target_vocabulary=target_vocabulary or ("RxNorm" if domain == "Drug" else None),
                        include_retired=include_retired_maps_to,
                    ))

        rollups = []
        for cls_name, data in class_dict.items():
            r = HierarchicalRollup(
                name=cls_name,
                domain=domain,
                root_vocabulary=eff_vocab,
                roots=data["roots"],
                target_vocabulary=target_vocabulary,
                expansion_direction=eff_expand or "descendants",
                include_retired_maps_to=include_retired_maps_to,
                exclude_dose_forms=exclude_dose_forms,
                combination_policy=combination_policy,
                rules=data["rules"],
                source_file=", ".join(source_files),
                source_file_hash=hashlib.sha256("".join(source_file_hashes.values()).encode("utf-8")).hexdigest(),
            )
            rollups.append(r)

        return cls(
            name=name or (Path(paths[0]).stem if len(paths) == 1 else "multi_csv_rollup_engine"),
            domain=domain,
            rollups=rollups,
            exclude_dose_forms=exclude_dose_forms,
            combination_policy=combination_policy,
            source_files=source_files,
            source_file_hashes=source_file_hashes,
        )

    @classmethod
    def from_folder(
        cls,
        folder_path: str | Path,
        glob: str = "*.csv",
        class_col: str | int | None = "CLASS",
        code_col: str | int = "CODE",
        vocabulary_col: str | int | None = None,
        vocabulary_id: str | None = None,
        root_vocabulary: str | None = None,
        domain: str = "Drug",
        target_vocabulary: str | None = None,
        expand: str | None = "descendants",
        expansion_direction: str | None = None,
        include_retired_maps_to: bool = True,
        exclude_dose_forms: Sequence[str] | None = None,
        combination_policy: str = "attribute_all",
        name: str | None = None,
        header: bool | int | None = True,
        **kwargs,
    ) -> RollupEngine:
        """Instantiate a RollupEngine by discovering CSV code lists in a folder."""
        p = Path(folder_path).resolve()
        if not p.is_dir():
            raise NotADirectoryError(f"Folder path does not exist or is not a directory: {folder_path}")
        files = sorted(p.glob(glob))
        if not files:
            raise FileNotFoundError(f"No files matching glob {glob!r} found in {folder_path}")
        return cls.from_csvs(
            paths=files,
            class_col=class_col,
            code_col=code_col,
            vocabulary_col=vocabulary_col,
            vocabulary_id=vocabulary_id,
            root_vocabulary=root_vocabulary,
            domain=domain,
            target_vocabulary=target_vocabulary,
            expand=expand,
            expansion_direction=expansion_direction,
            include_retired_maps_to=include_retired_maps_to,
            exclude_dose_forms=exclude_dose_forms,
            combination_policy=combination_policy,
            name=name or p.name,
            header=header,
            **kwargs,
        )

    @classmethod
    def from_bundled(
        cls,
        name: str = "ederri_medications",
        bundle_name: str | None = None,
        resource_name: str | None = None,
        class_col: str | int | None = "CLASS",
        code_col: str | int | None = None,
        vocabulary_col: str | int | None = "VOCABULARY",
        vocabulary_id: str | None = None,
        domain: str = "Drug",
        expand: str | None = "descendants",
        include_retired_maps_to: bool = True,
        exclude_dose_forms: Sequence[str] | None = None,
        **kwargs,
    ) -> RollupEngine:
        """Instantiate a RollupEngine from bundled clinical code lists."""
        res_target = bundle_name or resource_name or name
        kwargs.pop("bundle_name", None)
        kwargs.pop("resource_name", None)
        res_path = _resolve_code_list_resource(res_target)
        if res_path.suffix.lower() in (".xlsx", ".xls"):
            return cls.from_excel(
                path=res_path,
                class_col=class_col,
                code_col=code_col or "CODE",
                vocabulary_col=vocabulary_col,
                vocabulary_id=vocabulary_id,
                domain=domain,
                expand=expand,
                include_retired_maps_to=include_retired_maps_to,
                exclude_dose_forms=exclude_dose_forms,
                **kwargs,
            )
        else:
            if code_col is None:
                df_prev = pd.read_csv(res_path, nrows=2)
                code_col = "RXNORM_CUI" if "RXNORM_CUI" in df_prev.columns else ("CODE" if "CODE" in df_prev.columns else 0)
            return cls.from_csvs(
                paths=[res_path],
                class_col=class_col,
                code_col=code_col,
                vocabulary_col=vocabulary_col,
                vocabulary_id=vocabulary_id,
                domain=domain,
                expand=expand,
                include_retired_maps_to=include_retired_maps_to,
                exclude_dose_forms=exclude_dose_forms,
                name=name,
                **kwargs,
            )

    def materialize(
        self,
        con: duckdb.DuckDBPyConnection | Any,
        vocab_db: str | None = None,
        cache_path: str | Path | None = None,
    ) -> pd.DataFrame:
        """Materialize resolved crosswalk concepts across all rollups and optionally cache to Parquet."""
        if cache_path:
            p = Path(cache_path)
            if p.exists():
                try:
                    cached_df = pd.read_parquet(p)
                    if (
                        not cached_df.empty
                        and "spec_hash" in cached_df.columns
                        and cached_df["spec_hash"].iloc[0] == self.spec_hash()
                    ):
                        valid = True
                        for fpath, fhash in self.source_file_hashes.items():
                            if os.path.exists(fpath):
                                if compute_file_sha256(fpath) != fhash:
                                    valid = False
                                    break
                        if valid:
                            return cached_df
                except Exception:
                    pass

        if len(self.rollups) == 1:
            df = self.rollup.materialize(con, vocab_db=vocab_db)
            df["engine_name"] = self.name
            df["spec_hash"] = self.spec_hash()
            if cache_path:
                p = Path(cache_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(p, index=False)
            return df

        dfs = []
        for r in self.rollups.values():
            df_r = r.materialize(con, vocab_db=vocab_db)
            if not df_r.empty:
                dfs.append(df_r)

        if not dfs:
            combined = pd.DataFrame(columns=[
                "concept_id", "concept_name", "domain_id", "vocabulary_id",
                "concept_class_id", "standard_concept", "concept_code",
                "valid_start_date", "valid_end_date", "invalid_reason", "matched_pattern",
                "rollup_name", "combination_policy", "spec_hash", "engine_name"
            ])
        else:
            combined = pd.concat(dfs, ignore_index=True)

        combined["engine_name"] = self.name
        combined["spec_hash"] = self.spec_hash()

        if cache_path:
            p = Path(cache_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            combined.to_parquet(p, index=False)

        return combined

    def extract_exposures(
        self,
        con: duckdb.DuckDBPyConnection | Any,
        site_db: str | None = None,
        cohort_table: str = "cohort",
        prescribed_types: Sequence[int] | None = None,
        include_combination_overlap: bool = True,
        vocab_db: str | None = None,
    ) -> pd.DataFrame:
        """Extract exposure records from site CDM table matched against materialized crosswalk."""
        crosswalk_df = self.materialize(con, vocab_db=vocab_db)
        if crosswalk_df.empty:
            return pd.DataFrame()

        de_prefix = f"{site_db}." if site_db else ""
        de_table = f"{de_prefix}drug_exposure"
        c_table = f"{de_prefix}{cohort_table}" if site_db and "." not in cohort_table else cohort_table

        types = list(prescribed_types) if prescribed_types is not None else list(DEFAULT_PRESCRIBED_TYPE_CONCEPT_IDS)
        type_str = ", ".join(str(int(t)) for t in types)

        cids_and_names = (
            crosswalk_df[["concept_id", "rollup_name"]]
            .dropna(subset=["concept_id"])
            .drop_duplicates()
        )
        cids_and_names["concept_id"] = cids_and_names["concept_id"].astype(int)

        con.execute("CREATE TEMPORARY TABLE IF NOT EXISTS _tmp_xwalk_ids (concept_id BIGINT, rollup_name VARCHAR)")
        con.execute("DELETE FROM _tmp_xwalk_ids")
        con.register("_tmp_xwalk_reg", cids_and_names)
        con.execute("INSERT INTO _tmp_xwalk_ids SELECT concept_id, rollup_name FROM _tmp_xwalk_reg")
        con.unregister("_tmp_xwalk_reg")

        query = f"""
            SELECT 
                de.drug_exposure_id,
                de.person_id,
                de.drug_concept_id,
                de.drug_exposure_start_date,
                de.drug_exposure_end_date,
                de.drug_type_concept_id,
                x.rollup_name
            FROM {de_table} de
            JOIN _tmp_xwalk_ids x ON de.drug_concept_id = x.concept_id
            WHERE de.drug_type_concept_id IN ({type_str})
        """
        return con.execute(query).df()


def materialize_crosswalk(
    rollup: HierarchicalRollup | RollupEngine,
    con: duckdb.DuckDBPyConnection | Any,
    output_path: str | Path | None = None,
    vocab_db: str | None = None,
) -> pd.DataFrame:
    """Materialize crosswalk to Parquet or DuckDB with cryptographic spec hash."""
    if isinstance(rollup, RollupEngine):
        return rollup.materialize(con, vocab_db=vocab_db, cache_path=output_path)
    return rollup.materialize(con, vocab_db=vocab_db, cache_path=output_path)


def audit_concept_drift(
    con_old: duckdb.DuckDBPyConnection | Any,
    con_new: duckdb.DuckDBPyConnection | Any,
    rollup: HierarchicalRollup | RollupEngine,
    vocab_db_old: str | None = None,
    vocab_db_new: str | None = None,
) -> dict[str, Any]:
    """Audit concept additions, removals, and status changes across vocabulary versions."""
    df_old = rollup.materialize(con_old, vocab_db=vocab_db_old) if hasattr(rollup, "materialize") else rollup
    df_new = rollup.materialize(con_new, vocab_db=vocab_db_new) if hasattr(rollup, "materialize") else rollup

    old_ids = set(df_old["concept_id"].tolist())
    new_ids = set(df_new["concept_id"].tolist())

    added = sorted(list(new_ids - old_ids))
    removed = sorted(list(old_ids - new_ids))
    shared = new_ids.intersection(old_ids)

    status_changed = []
    if shared:
        old_indexed = df_old.set_index("concept_id")
        new_indexed = df_new.set_index("concept_id")
        for cid in shared:
            old_row = old_indexed.loc[cid]
            new_row = new_indexed.loc[cid]
            old_stat = getattr(old_row, "standard_concept", None) if not isinstance(old_row, pd.DataFrame) else old_row["standard_concept"].iloc[0]
            new_stat = getattr(new_row, "standard_concept", None) if not isinstance(new_row, pd.DataFrame) else new_row["standard_concept"].iloc[0]
            old_inv = getattr(old_row, "invalid_reason", None) if not isinstance(old_row, pd.DataFrame) else old_row["invalid_reason"].iloc[0]
            new_inv = getattr(new_row, "invalid_reason", None) if not isinstance(new_row, pd.DataFrame) else new_row["invalid_reason"].iloc[0]
            if old_stat != new_stat or old_inv != new_inv:
                status_changed.append({
                    "concept_id": cid,
                    "concept_name": getattr(new_row, "concept_name", None) if not isinstance(new_row, pd.DataFrame) else new_row["concept_name"].iloc[0],
                    "old_standard": old_stat,
                    "new_standard": new_stat,
                    "old_invalid_reason": old_inv,
                    "new_invalid_reason": new_inv,
                })

    return {
        "rollup_name": rollup.name if hasattr(rollup, "name") else "custom_rollup",
        "old_total_concepts": len(old_ids),
        "new_total_concepts": len(new_ids),
        "added_count": len(added),
        "removed_count": len(removed),
        "status_changed_count": len(status_changed),
        "added_concept_ids": added,
        "removed_concept_ids": removed,
        "status_changed": status_changed,
    }
