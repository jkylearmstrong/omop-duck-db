# Bundled Clinical Reference Code Lists for Hierarchical Rollups

This directory contains standardized clinical medication and condition code lists curated and reviewed for multi-center clinical studies (such as eDERRI - electronic Diabetes Early Readmission Risk Index):

- `ederri_medications.csv`: Canonical clinical review table covering key antidiabetic medication classes (Biguanides, Sulfonylureas, Thiazolidinediones, DPP-4 Inhibitors, SGLT-2 Inhibitors, GLP-1 Receptor Agonists, and Insulins) with standard RxNorm CUIs, ATC codes, and descriptions.
- `diabetes_drug_categories.csv`: Domain-specific category file mapping clinical classes directly to RxNorm CUIs.
- `rxnorm_categories_CUI_map_brief.csv`: Headerless two-column mapping table (`CLASS`, `RXNORM_CUI`) representing legacy academic consortium code lists.
- `eDERRI_Clinical_Review_Package.xlsx`: Multi-sheet clinical review package containing `Medication_Classes` and `Diabetes_Medications` sheets.

These tables can be ingested directly into `HierarchicalRollup` and `RollupEngine` using:

```python
from omop_duck_db.rollups import HierarchicalRollup, RollupEngine

# Bundled engine loader
engine = RollupEngine.from_bundled("ederri_medications")

# Single-class loader from Excel sheet
rollup = HierarchicalRollup.from_excel(
    path="inst/extdata/code_lists/eDERRI_Clinical_Review_Package.xlsx",
    sheet_name="Diabetes_Medications",
    name="Biguanides",
    code_col="RXNORM_CUI",
    class_col="CLASS",
)
```
