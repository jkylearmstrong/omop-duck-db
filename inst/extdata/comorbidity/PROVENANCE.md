# Comorbidity code tables: provenance

Sources, weights, hierarchy rules, known deviations and per-domain confidence for each code table in this directory. Each index has its own section.

<!-- BEGIN charlson -->
## Charlson index (`charlson_codes.csv`)

Seventeen Charlson categories following the Quan et al. 2005 coding algorithms
(ICD-10 and enhanced ICD-9-CM), plus a curated set of standard SNOMED
Condition ancestors per category for the standard-concept side.

### Sources actually consulted

- Quan H, Sundararajan V, Halfon P, et al. Coding algorithms for defining
  comorbidities in ICD-9-CM and ICD-10 administrative data. Med Care
  2005;43(11):1130-9. An earlier curation pass of this table recorded reading
  **Table 1** from a public PDF reproduction ("Charlson Comorbidities - Coding
  Algorithms for ICD-9-CM and ICD-10") and reading one typo (`E10.l`) as
  `E10.1`. That reproduction, the Quan article itself and a second public PDF
  of Charlson/Quan code lists could **not** be opened during the later
  re-verification (the fetch tool returned undecodable, protected PDF binary),
  so the original table typesetting was not re-checked. The narrow Quan
  malignancy ranges used here (ICD-10 C00-C26, C30-C34, C37-C41, C43, C45-C58,
  C60-C76, C81-C85, C88, C90-C97; ICD-9-CM 140-172, 174-195, 200-208, 238.6)
  rest on that earlier reading and are not independently re-confirmed.
- Cross-check against an open-source implementation, re-done in the
  re-verification pass: the `comorbidity` R package (CRAN, Gasparini),
  `data-raw/make-mapping.R` (`charlson_icd10_quan`, `charlson_icd9_quan`), and
  `R/score.R` for the hierarchy (`assign0`). Both files were retrieved through
  a page-summarising fetch tool, so the lists are second-hand. The lists were
  transcribed into a script and compared with this CSV by prefix containment
  (both directions). Result: for all 16 non-malignancy categories, every
  package prefix is either covered by this table or matches no code at all in
  ICD-10-CM / ICD-9-CM (the removals listed under (a)), and this table has no
  prefix that the package lacks. Malignancy differs on purpose: the package
  uses the broad ranges C30-C39 and C45-C97 and ICD-9 140-195, which also
  cover C77-C80 (metastasis, a separate category here), C86, C7A/C7B and ICD-9
  173; this table follows Quan's narrower ranges and therefore omits those
  (prefix-level differences: ICD-10 C77-C80 and C86, ICD-9 173).
- Weights: original Charlson et al. 1987 (J Chronic Dis 40:373) weights as
  carried by Quan 2005. The 1987 paper was **not** read. The weights were
  confirmed only from secondary sources (web-search summaries of the 1987
  weights and the `comorbidity` package, which offers `charlson` = 1987 and
  `quan` = 2011 weight sets): diabetes with chronic complication 2,
  hemiplegia 2, renal disease 2, any malignancy 2, moderate or severe liver
  disease 3, metastatic solid tumor 6, AIDS 6, and 1 for the first ten
  categories. The updated Quan 2011 weights are **not** used.
- OMOP standardized vocabularies (Athena download, vocabulary version
  `v5.0 27-FEB-26`) for every automated check below.

### Weights (exactly as in the CSV)

mi 1, chf 1, pvd 1, cevd 1, dementia 1, copd 1, rheum 1, pud 1, mild_liver 1,
dm_uncomp 1, dm_comp 2, plegia 2, renal 2, malignancy 2, mod_severe_liver 3,
mets 6, hiv 6 (the 17 categories in this order).

### Hierarchy rules (to be applied when scoring, not when flagging)

Raw `cci_*` flags are never altered. When computing the hierarchy-adjusted
score:

- `mod_severe_liver` supersedes `mild_liver` (mild contributes 0 if severe = 1).
- `dm_comp` supersedes `dm_uncomp` (uncomplicated contributes 0 if complicated = 1).
- `mets` supersedes `malignancy` (any-malignancy contributes 0 if metastatic = 1).

The standard-concept side makes these rules necessary: `dm_uncomp` uses the
generic `Diabetes mellitus` ancestor, whose descendants include every
complicated form, so its set overlaps `dm_comp` by construction; likewise
alcoholic hepatic failure and decompensated cirrhosis sit under both liver
sets.

### Row conventions

- ICD rows are prefixes (dots removed, upper case) taken from Quan Table 1 and
  expanded to 3-4 character prefixes. Source-value matching on prefixes alone
  cannot tell ICD-9-CM from ICD-10-CM for codes starting with `V`: the ICD-9-CM
  prefixes V434, V420, V427, V451 and V56 collide with ICD-10-CM transport
  accident codes (V4x, V5x). Apply `ICD9CM` rows only to ICD-9-CM data where the
  coding system is known, or accept the rare collision.
- `SNOMED_ANCESTOR` rows hold OMOP concept_ids; the `note` column carries the
  concept name for review. Descendants (including the ancestor) are taken from
  `concept_ancestor`.
- `mets` has **no** `SNOMED_ANCESTOR` rows (see below).

### Validation against the vocabulary (all run, results)

(a) Every ICD prefix matches at least one real concept: 457 prefix rows, 0
match nothing, 0 match only deprecated concepts. The initial expansion of
Quan Table 1 had 59 prefixes that match nothing in ICD-10-CM/ICD-9-CM. None is
a typo: all 37 ICD-10 ones exist in the WHO ICD-10 vocabulary but not in
ICD-10-CM, and the 22 ICD-9 ones are code numbers that do not exist in
ICD-9-CM (artifacts of expanding numeric ranges). They were **removed**:

- ICD-10 (WHO-only): pvd I792; cevd I64; dementia F00 F051; copd J46;
  dm_uncomp E100 E120 E121 E126 E128 E129 E140 E141 E146 E148 E149;
  dm_comp E107 E117 E122 E123 E124 E125 E127 E137 E142 E143 E144 E145 E147;
  renal Z491 Z492; malignancy C97; mod_severe_liver I859 I982; hiv B21 B22 B24.
- ICD-9-CM (nonexistent): chf 4256; pvd 4433-4437; copd 497 498 499;
  renal 5833 5835; malignancy 166 167 168 169 177 178; mod_severe_liver
  5725 5726 5727; hiv 043 044.

Sites that store **WHO ICD-10** (not ICD-10-CM) source values lose these
prefixes (notably I64 stroke NOS, E12/E14, F00, J46, B21-B24). Re-add them
under a separate code system if WHO ICD-10 support is wanted.

(b) All 112 `SNOMED_ANCESTOR` concept_ids exist, are `SNOMED`, `standard_concept
= 'S'`, `domain_id = 'Condition'`, not deprecated, and each name was reviewed
for clinical fit (names are in the CSV `note`).

(c) Descendant counts (standard Condition concepts including the ancestor):
mi 132, chf 225, pvd 403, cevd 582, dementia 222, copd 292, rheum 256,
pud 308, mild_liver 147, dm_uncomp 145, dm_comp 332, plegia 272, renal 160,
malignancy 48,797, mod_severe_liver 77, hiv 200, mets 0. (Unfiltered
`concept_ancestor` descendants are a few larger because some ancestors have
Observation-domain descendants, e.g. drug-induced asthma and an ESRD dialysis
service concept: rheum 259, copd 293, renal 161, malignancy 48,801; consumers
should intersect with Condition-domain concepts, which condition_occurrence
does by construction.) The malignancy set is
large because it is the union of organ-system level malignancy ancestors; no
whole-body root is used. Pairwise overlap between domains (shared
descendants): cevd-plegia 58 (stroke with hemiplegia), dm_comp-renal 40
(diabetic kidney disease), cevd-dementia 10 (multi-infarct dementia),
dm_comp-pvd 9 (diabetic angiopathy), hiv-malignancy 7 and dementia-hiv 5
(HIV-associated lymphoma and dementia), dm_comp-dm_uncomp 6,
mild_liver-mod_severe_liver 4 (alcoholic hepatic failure, decompensated
cirrhosis), dm_uncomp-pvd 2, mild_liver-rheum 2, and single shared
concepts in chf-mild_liver, chf-copd, copd-rheum and hiv-mild_liver. Each is a concept that is genuinely a member of both conditions
(comorbid or overlapping), except the dm_uncomp/dm_comp and mild/severe liver
overlaps, which the hierarchy rules resolve.

(d) ICD vs standard-concept consistency (via `Maps to`). A = share of valid
ICD concepts in the domain's prefixes that map to a standard Condition and land
inside the ancestor descendant set; B = share of all valid ICD concepts that map
into the descendant set and lie inside the domain's prefixes (the rest are
"leaks"); P = share of prefixes where at least half of their mappable codes land
in the descendant set (prefixes with no mappable condition code are not counted).
Figures were recomputed from this CSV after the pvd change below.

| domain | ICD-10-CM A | ICD-9-CM A | B | P | confidence |
|---|---|---|---|---|---|
| mi | 100% | 100% | 100% | 100% | high |
| chf | 88% | 91% | 65% | 67% | medium-high |
| pvd | 97% | 73% | 88% | 57% | medium-high |
| cevd | 91% | 77% | 95% | 73% | medium |
| dementia | 100% | 100% | 82% | 100% | high |
| copd | 79% | 90% | 90% | 71% | medium-high |
| rheum | 97% | 69% | 99% | 85% | high |
| pud | 100% | 100% | 96% | 100% | high |
| mild_liver | 80% | 63% | 84% | 53% | medium |
| dm_uncomp | 97% | 100% | 17% | 85% | medium (structural overlap) |
| dm_comp | 100% | 100% | 56% | 88% | high |
| plegia | 89% | 79% | 37% | 65% | medium |
| renal | 52% | 44% | 58% | 25% | ICD side high, SNOMED side low-medium |
| malignancy | 93% | 93% | 93% | 91% | medium-high |
| mod_severe_liver | 84% | 89% | 42% | 81% | medium |
| mets | n/a | n/a | n/a | n/a | ICD only |
| hiv | 100% | 100% | 14% | 100% | high (ICD), see caveat |

Low B values are mostly explained by overlap with a sibling domain (dm_comp
codes inside the `Diabetes mellitus` descendants, I69 stroke-sequela codes
inside the hemiplegia concepts, acute hepatic failure under `Hepatic failure`)
or by ICD codes outside Quan's list that are clinically the same condition
(E08/E09 secondary diabetes, O24 pregnancy-related codes, O98 HIV in
pregnancy).

### Per-domain notes and known deviations

- **mi**: `Myocardial infarction` covers all 67 mappable ICD codes with no leaks.
- **chf**: `Heart failure` plus dilated, restrictive and toxic cardiomyopathy
  (Quan I42.0, I42.5-I42.7). Not covered on the standard side: generic
  cardiomyopathy (I42.8, I42.9, I43, 425.4/425.8/425.9), ischemic
  cardiomyopathy (I25.5) and rheumatic heart disease (I09.9); they are caught
  only through the ICD path. `Heart failure` includes right ventricular
  failure and therefore cor pulmonale, so ICD codes I26.0x and I27.x (not in
  Quan) map into the set; this is an intrinsic SNOMED hierarchy choice.
- **pvd**: Z95.8/Z95.9, ICD-9 V43.4 and most other status codes map to
  Observation-domain concepts and cannot be matched on the standard side.
  Atheroembolism (I75), arterial embolism (I74) and diabetic angiopathy fall
  inside the peripheral vascular disease descendants. The generic
  `Atherosclerosis of artery` concept (40479625) is deliberately **not** used:
  its descendants include coronary atherosclerosis (7 concepts, e.g.
  `Coronary atherosclerosis`), carotid and cerebral atherosclerosis, which
  Charlson does not count as peripheral vascular disease. Specific
  non-coronary atherosclerosis concepts (aorta, extremities, lower and upper
  limb, bypass graft, abdominal visceral, iliac, generalized, occlusive,
  aortoiliac, brachiocephalic) are listed instead. The cost is that records
  coded with the unspecified `Atherosclerosis of artery` itself (ICD-9 440,
  440.8, 440.9 map there) are caught only by the ICD path.
- **cevd**: the root `Cerebrovascular disease` is deliberately not used: it
  contains traumatic vessel injury (S06, S15 codes) and congenital anomalies.
  Instead specific stroke, TIA, occlusion/stenosis, sequela and aneurysm
  concepts are listed. Unspecified cerebrovascular disease (I67.9, I68.8,
  G46.x) and most I69 sequela codes are caught only by the ICD path. Perinatal
  and migraine-related infarction codes fall inside the infarction concept.
- **dementia**: Quan ICD-9-CM (`290`, `2941`, `3312`) omits 331.0 (Alzheimer
  disease) while the ICD-10 list includes G30; the standard-side
  `Alzheimer's disease` ancestor includes both. ICD-9-CM 294.2x (dementia
  unspecified, added after Quan 2005) is not in the list. Substance-induced
  dementia, frontotemporal dementia and Huntington disease fall inside the
  `Dementia` descendants.
- **copd**: prefix `I278` also matches ICD-10-CM I27.82 (chronic pulmonary
  embolism), which is not chronic pulmonary disease; this follows Quan's
  prefix and is kept. Asthma is included as in Quan. Generic and
  occupational-agent codes (J63.1, J63.3, J66.8, J68.4, J70.1, J70.3,
  J44.8x) are ICD-path only.
- **rheum**: ICD-9-CM 710.2 (sicca syndrome) is in Quan's ICD-9 list but has no
  ICD-10 counterpart; no standard ancestor was added for it. `Giant cell
  arteritis` also admits M31.6 (not in Quan, one code). Juvenile arthritis
  codes can enter through `Rheumatoid arthritis` (one code observed).
- **pud**: `Peptic ulcer` also admits esophageal ulcer and neonatal peptic
  ulcer (7 of 159 ICD codes).
- **mild_liver / mod_severe_liver**: Quan counts cirrhosis (K74.6, K70.3) as
  mild and hepatic failure, portal hypertension, esophageal/gastric varices
  and hepatorenal syndrome as moderate or severe. Known artifacts: ICD-10-CM
  K76.82 (hepatic encephalopathy, added after Quan) matches the mild prefix
  K768 while ICD-9 572.2 maps to severe; the standard-side `Hepatic
  encephalopathy` and `Hepatic coma` are under severe. The severe set admits
  acute hepatic failure (K72.0), postprocedural hepatic failure, hepatic coma
  in viral hepatitis and secondary esophageal varices (not in Quan). Toxic
  liver disease with necrosis (K71.1) and veno-occlusive disease (K76.5) are
  ICD-path only. NASH (K75.81) falls inside `Steatotic liver disease`.
- **dm_uncomp / dm_comp**: Quan counts ketoacidosis, coma, other specified
  and unspecified complications (E10.0/.1/.6/.8, ICD-9 250.1-250.3 and 250.8,
  250.9) as **uncomplicated** and only renal, ophthalmic, neurological and
  peripheral-circulatory complications (plus E10.7 multiple) as chronic
  complications. `dm_comp` therefore uses exactly the four matching standard
  concepts. `dm_uncomp` uses the generic `Diabetes mellitus` concept (the
  standard concept that E10.9, E11.9, E13.9 and ICD-9 250.0x map to); gestational
  diabetes and secondary/drug-induced diabetes are descendants (a known leak).
  E08/E09 are ICD-10-CM codes not in Quan and are not listed.
- **plegia**: standard-side set uses hemiplegia, paraplegia, diplegia,
  monoplegia, tetraplegia/quadriplegia, hereditary spastic paraplegia and the
  one-sided paralytic syndrome. Cerebral palsy codes (G80.1, G80.2, ICD-9 343.x),
  cauda equina syndrome (G83.4, 344.6) and unspecified paralysis (G83.9, 344.9)
  are ICD-path only. Stroke sequela concepts with hemiplegia also count in
  `cevd` (intended: both conditions are present).
- **renal**: lowest standard-side confidence. Chronic kidney disease, ESRD,
  chronic renal failure and renal osteodystrophy are used. Hypertensive CKD
  (I12.0, I13.1, 403.x/404.x) and the glomerular codes with morphology
  (N03.2-N03.7, N05.2-N05.7, 582-583) map to concepts outside these
  descendants and are ICD-path only; dialysis and transplant status
  (Z49, Z94.0, Z99.2, V42.0, V45.1, V56) map to Observation/Procedure
  concepts. Hypertensive CKD complicating childbirth (O10.2, O10.3) enters
  through the CKD descendants.
- **malignancy**: union of organ-system malignancy ancestors (digestive,
  respiratory, thorax incl. breast, genitourinary, nervous, skeletal,
  connective tissue, melanoma, endocrine, ENT, eye, lip/oral cavity/pharynx),
  the lymphoid/haematopoietic ancestor, multiple myeloma and GIST. The
  non-melanoma skin cancer exclusion (C44, ICD-9 173) cannot be expressed with
  ancestors: only basal/squamous skin cancers of specific sites that are also
  children of a listed site concept (about 28 ICD codes, e.g. skin of breast,
  lip) leak in. Other leaks: carcinoid tumors (C7A, ICD-9 209; clinically
  malignant, added after Quan), myelodysplastic and myeloproliferative
  neoplasms (D45-D47, ICD-9 238.x) and C86. Kaposi sarcoma (C46), connective
  and soft tissue (C49 partly, 171) and ill-defined sites (C76, 195, 159) are
  ICD-path only. ICD-10-CM C7A/C7B are not in Quan and are not listed.
- **mets**: Quan's C77-C80 and ICD-9 196-199 map in the current vocabulary to
  **Measurement**-domain standard concepts ("Metastasis to ...", "Spread to
  lymph node") and only 19 of 99 valid ICD concepts map to a Condition at all.
  No Condition ancestor captures the domain: `Metastatic malignant neoplasm`
  (432851, the concept the earlier draft used) has descendants such as
  leukemic infiltration of skin and lymphoma of thyroid and matches none of the
  ICD codes, so it is deliberately **not** used. Metastatic solid tumor is
  therefore detected from ICD source values only.
- **hiv**: `Human immunodeficiency virus infection` is the standard concept for
  B20 / 042 but its descendants include asymptomatic HIV infection (Z21, V08),
  HIV in pregnancy (O98.7) and HIV-2; asymptomatic infection is therefore
  also flagged on the standard side (Quan's list excludes Z21).

### Corrections to the earlier draft

The unreviewed draft's SNOMED ids were not usable. 84114007, 318798, 13645005
and 437750 do not exist as OMOP concept_ids and 4299839 is a drug. Others were
different conditions or misplaced: 37311061 (COVID-19), 381537 (organic anxiety
disorder), 374341 (Huntington's chorea), 4173504 (sick child), 192359 (renal
failure syndrome), 4028300 (a fish family), 435754 and 443384 (colon tumors),
443614 (CKD stage 1 only), 443734 (ketoacidosis, an uncomplicated-diabetes
concept), 381316 (stroke, listed under hemiplegia), 443767 (diabetic eye
disease, listed as uncomplicated diabetes) and 4245975 (hepatic failure,
listed as mild liver disease). All concept_ids here were chosen from
vocabulary queries and re-validated from the CSV itself. A later review
replaced the pvd ancestor 40479625 (`Atherosclerosis of artery`), which an
earlier pass of this table had kept, because it admits coronary, carotid and
cerebral atherosclerosis (see the pvd note). Relative to Quan
Table 1 the draft's ICD lists also lacked ICD-10 N03.2-N03.7 and N05.2-N05.7
and ICD-9-CM 710.2, 710.3, 583.3, 583.5 and 572.5-572.7.
<!-- END charlson -->

<!-- BEGIN elixhauser -->
## Elixhauser index (`elixhauser_codes.csv`)

The 31 Elixhauser comorbidity domains as defined by the Quan et al. 2005
coding algorithms (ICD-9-CM and ICD-10), adapted to the ICD-10-CM that the
OMOP vocabulary actually contains, with the van Walraven 2009 point weights and
a curated set of standard SNOMED Condition ancestors per domain for the
standard-concept side. 831 rows: 379 `ICD10CM` prefixes, 314 `ICD9CM`
prefixes, 138 `SNOMED_ANCESTOR` concept_ids. Domain keys and `elix_<domain>`
column names follow the earlier draft (`chf`, `arrhythmia`, `valvular`,
`pulm_circ`, `pvd`, `htn_uncomp`, `htn_comp`, `paralysis`, `neuro_other`,
`copd`, `dm_uncomp`, `dm_comp`, `hypothyroid`, `renal_failure`,
`liver_disease`, `pud`, `hiv`, `lymphoma`, `mets`, `solid_tumor`, `rheumatic`,
`coagulopathy`, `obesity`, `weight_loss`, `fluid_electrolyte`,
`blood_loss_anemia`, `deficiency_anemia`, `alcohol_abuse`, `drug_abuse`,
`psychoses`, `depression`).

### Sources actually consulted

- Quan H, Sundararajan V, Halfon P, et al. Coding algorithms for defining
  comorbidities in ICD-9-CM and ICD-10 administrative data. Med Care
  2005;43(11):1130-9. **The paper itself and its Table 1 were not opened** (no
  accessible full text). The Elixhauser ICD-9-CM and ICD-10 lists were checked
  against the Quan SAS macros `ICD9_E_Elixhauser.sas` ("Enhanced ICD-9-CM
  Elixhauser") and `ICD10_Elixhauser.sas`, as redistributed in the
  `data-raw/` directory of the `icd` R package repository
  (github.com/jackwasey/icd). The macro headers cite Quan et al. 2005 (with
  mis-typed page numbers), so the attribution rests on the header text, not on
  a copy supplied by the authors. The macros were downloaded and parsed
  programmatically, and every per-domain prefix list was diffed against the CSV
  (31 domains x 2 systems). Result: the CSV ICD-9-CM list is exactly Quan's list
  (322 prefixes) minus 8 prefixes that do not exist in the ICD-9-CM vocabulary;
  the ICD-10 list is Quan's list (399 prefixes) minus 40 prefixes absent from
  ICD-10-CM plus 20 ICD-10-CM additions (both listed below), 379 in all. There
  are no other differences. The lists were earlier also compared with two
  further open-source transcriptions of the same table (the `comorbidity` R
  package, `data-raw/make-mapping.R`, and the `comorbidipy` Python package),
  read through a page-summarising fetch tool; the macros are what the final
  diff used. The macros are Quan's own lineage, so they establish that the CSV
  reproduces the Quan algorithm, not that the algorithm is clinically ideal.
- Weights: van Walraven C, Austin PC, Jennings A, Quan H, Forster AJ. A
  modification of the Elixhauser comorbidity measures into a point system for
  hospital death using administrative data. Med Care 2009;47(6):626-33. **The
  paper's weight table was not opened** (PubMed Central and PubMed were blocked
  by bot checks). The weights are those in `comorbidity`
  (`data-raw/make-weights.R`, `vw` column) and `comorbidipy`
  (`codemaps/weights.py`, `van_walraven`), plus a third listing in the
  `comorbidipy` documentation page; all agree with each other and with the
  unreviewed draft. Weak independent corroboration of the aggregate shape only:
  search summaries and the Europe PMC abstract state that 21 of the 30
  Elixhauser groups were independently associated with death in 345,795
  hospitalizations, that the weights range from -7 to 12 and that the other
  groups get 0. The table here has exactly 21 non-zero weights and a range of
  -7 to 12. The per-domain values were **not** checked against the van Walraven
  paper itself. They were cross-checked against an independent republication of
  the van Walraven weights: Table 1 of Thompson NR, et al. A new
  Elixhauser-based comorbidity summary measure to predict in-hospital
  mortality. Med Care 2015;53(4):374-379 (PubMed Central PMC4812819, read
  through the same fetch tool). Its "VW" weight column gives, for 30 groups (it merges hypertension
  into one group, weight 0): CHF 7, arrhythmia 5, valvular -1, pulmonary
  circulation 4, peripheral vascular 2, hypertension 0, paralysis 7, other
  neurological 6, chronic pulmonary 3, diabetes complicated 0, diabetes
  uncomplicated 0, hypothyroidism 0, renal 5, liver 11, peptic ulcer 0,
  AIDS/HIV 0, lymphoma 9, metastatic 12, solid tumor 4, rheumatoid 0,
  coagulopathy 3, obesity -4, weight loss 6, fluid/electrolyte 5, blood loss
  anemia -2, deficiency anemia -2, alcohol 0, drug -7, psychoses 0, depression
  -3: identical to the CSV for every domain. Caveat: the fetch tool returned
  that table with the cells of rows that have an empty adjusted-odds-ratio cell
  shifted one column (valvular, the two diabetes rows, deficiency anemia,
  psychoses and a few zero-weight rows), so for those rows the VW weight was
  read from the position the column pattern implies; the values so read agree
  with every other listing. This is a second-hand check, not the paper. A
  second reading of `make-weights.R` during the final verification pass again
  returned the same 31 values; the Wikipedia article on the index and the
  `coder` package reference page do not list the per-domain weights, so they
  could not serve as an independent check.
- ICD-10-CM adaptation: the HL7 mCODE value sets derived from the AHRQ
  Elixhauser Comorbidity Software Refined (ICD-10-CM 2021.1), read through the
  same kind of fetch tool: "Diabetes without chronic complications" (confirms
  E08, E09, E10, E11 and E13 with the .0x, .1x and .9 pattern), lymphoma
  (confirms C86 and C90.0/C90.2 and C96 are included) and "Renal failure,
  severe" (confirms Z49.01, Z49.02, Z49.31, Z49.32, Z94.0, Z99.2). These three
  value sets were the only AHRQ material consulted; AHRQ's own software
  documentation, the original Elixhauser 1998 paper and the AHRQ hierarchy
  documentation were **not** read.
- Hierarchy rules: taken from the `comorbidity` package `score()` documentation
  and source (`assign0`), and the `comorbidipy` documentation ("Assign Zero
  Logic"). See below.
- OMOP standardized vocabularies (Athena download, vocabulary version
  `v5.0 27-FEB-26`; SNOMED 2025-02-01 International, ICD10CM FY2026, ICD9CM
  v32) for every automated check below. The vocabulary file was opened
  read-only.

### Weights (van Walraven 2009, exactly as in the CSV)

chf 7, arrhythmia 5, valvular -1, pulm_circ 4, pvd 2, htn_uncomp 0, htn_comp 0,
paralysis 7, neuro_other 6, copd 3, dm_uncomp 0, dm_comp 0, hypothyroid 0,
renal_failure 5, liver_disease 11, pud 0, hiv 0, lymphoma 9, mets 12,
solid_tumor 4, rheumatic 0, coagulopathy 3, obesity -4, weight_loss 6,
fluid_electrolyte 5, blood_loss_anemia -2, deficiency_anemia -2,
alcohol_abuse 0, drug_abuse -7, psychoses 0, depression -3.

The score is the sum of weight x flag over the 31 domains after the hierarchy
below. Negative weights are the paper's: these comorbidities were associated
with lower hospital mortality in its model, which is not a statement that they
protect patients.

### Hierarchy rules (apply when scoring and counting, not when flagging)

Raw `elix_*` flags are never altered. When computing the van Walraven score
and the total number of conditions:

- `dm_comp` supersedes `dm_uncomp` (uncomplicated contributes 0 if complicated = 1).
- `mets` supersedes `solid_tumor` (solid tumor contributes 0 if metastatic = 1).
  This one changes the score (12 instead of 16); the other two only change the
  condition count because their weights are 0.
- `htn_comp` supersedes `htn_uncomp` (uncomplicated contributes 0 if complicated = 1).

The standard-concept side makes the diabetes rule mandatory: there is no
"diabetes without complication" concept in SNOMED, so the `dm_uncomp`
ancestors are the diabetes types (Type 1, Type 2, secondary, two genetic
forms) whose descendants include every complicated form. `dm_uncomp` matched
through `SNOMED_ANCESTOR` therefore means "any of these diabetes types" and is
only correct after the hierarchy is applied. The raw `dm_uncomp` flag is
over-inclusive on the standard side by design. SNOMED also files diabetic
ketoacidosis, diabetic coma and hyperosmolarity (which Quan codes
E10.0/E10.1/E11.0/E11.1 as *uncomplicated*) under "complication due to
diabetes mellitus", so those concepts flag `dm_comp` on the standard side. Both
diabetes weights are 0, so only the condition count is affected.

### Prefix semantics

`code` is matched as a prefix after removing dots and upper-casing the source
code (`I50` matches I50.9; `4280` matches 428.0). The vocabulary stores ICD
codes with dots and includes header categories, so the check used
`replace(concept_code, '.', '') LIKE prefix || '%'`.

The profilers apply every prefix to every `condition_source_value`, whatever
its coding system, so a prefix of one system can match a code of the other.
Checked against the vocabulary (ICD10CM prefixes against ICD9CM codes and the
reverse, 17 colliding prefixes in this table): the ICD-9-CM `V` prefixes
(V422, V427, V420, V433, V434, V450, V451, V533, V56, V113) match ICD-10-CM
transport-accident codes, and the ICD-10-CM prefixes E00, E01, E02, E03, E890
(`hypothyroid`) and E86, E87 (`fluid_electrolyte`) match ICD-9-CM external-cause
E codes (E000-E030, E860-E879, E890). These are external-cause codes that are
rarely stored as diagnoses, but where they are, they flag a domain falsely.
Profile data whose coding system is known, or drop such rows first.

### Automated validation against the real vocabulary

All checks were re-run against the written CSV, not the intermediate lists.

- **Format**: exact header, `column = 'elix_' + domain`, one weight per
  domain, no duplicate (domain, code_system, code), ICD codes upper-case
  alphanumeric without dots, SNOMED codes integer, every domain has both
  ICD10CM and ICD9CM prefixes.
- **(a) ICD prefixes**: all 693 prefixes match at least one real
  ICD10CM/ICD9CM concept. Quan's own lists failed this check
  for 48 prefixes (40 ICD-10, 8 ICD-9; see "Prefixes removed" below), removed
  before writing the CSV; the cause is WHO ICD-10 versus ICD-10-CM and unused
  ICD-9 categories, not typos.
- **(b) SNOMED ancestors**: all 138 concept_ids exist, are valid,
  `standard_concept = 'S'`, `domain_id = 'Condition'` and were individually
  read by name (the names are in the `note` column). Zero failures.
- **(c) breadth**: the union of descendants per domain ranges from 4
  (`blood_loss_anemia`) to 11,859 (`lymphoma`); `lymphoma` is large only
  because 11,352 of its descendants are ICDO3 histology-by-site condition
  concepts (every lymphoma subtype x anatomical site). Next largest are
  `neuro_other` 890, `valvular` 558 and `dm_comp` 484; everything else is
  below 400. No whole-body-system root is used.
- **Cross-domain overlap of descendant sets** (concept counts): the largest are
  `htn_comp`/`renal_failure` 40 (chronic kidney disease due to hypertension) and
  `dm_comp`/`renal_failure` 40 (chronic kidney disease due to diabetes); both
  mirror Quan, which lists I12.0/I13.1 and E1x.2 in both domains. Others,
  all <= 12: `dm_comp`/`fluid_electrolyte` 12 (diabetic hyperosmolar and
  ketoacidotic concepts), `dm_comp`/`dm_uncomp` 10 (by construction, see
  hierarchy), `dm_comp`/`pvd` 9 (diabetic peripheral angiopathy, which AHRQ
  ICD-10-CM also keeps in peripheral vascular disease), `mets`/`solid_tumor` 9
  (metastatic melanoma; resolved by the hierarchy), `chf`/`htn_comp` 7,
  `hiv`/`lymphoma` 6, `chf`/`pulm_circ` 6, `hiv`/`neuro_other` 5,
  `dm_uncomp`/`neuro_other` 5, and 17 pairs of 1-3 concepts (concepts that
  genuinely denote two conditions, for example neurological disease due to
  HIV). One overlap is a SNOMED quirk rather than a double diagnosis:
  `blood_loss_anemia`/`deficiency_anemia` 1 (iron deficiency anemia due to
  blood loss is a child of both).
  Larger overlaps found during curation were **removed**, not accepted: see
  "Standard-concept side: ancestors rejected".
- **ICD prefix overlap inside the table** (inherited from Quan, kept): ICD-10
  `I110/I130/I132` in chf and `I11/I13` in htn_comp; `I120/I131` in
  renal_failure and htn_comp; `I426` in chf and alcohol_abuse; `I278/I279` in
  copd and the `I27` prefix of pulm_circ; `K700/K703/K709` in alcohol_abuse and
  the `K70` prefix of liver_disease; `F315` in depression and psychoses; `G114`
  in paralysis and the `G11` prefix of neuro_other. The ICD-9-CM equivalents
  (`40201...40493`, `4255`, `4168/4169`, `5710-5713`, `2965`, `3341`) are also
  duplicated across domains in Quan's table.
- **(d) ICD to SNOMED cross-check** via `concept_relationship 'Maps to'` from
  every ICD10CM/ICD9CM code under each domain's prefixes to standard Condition
  concepts, then the share landing inside the chosen ancestors' descendants
  (concept recall = distinct mapped standard concepts covered; the mismatch
  rate is 1 - recall):

  | domain | ICD-10-CM codes covered | ICD-9-CM codes covered | concept recall | ICD codes outside the prefix set that the ancestors also capture |
  |---|---|---|---|---|
  | chf | 88% | 91% | 80% | 36 (peripartum/postprocedural cardiomyopathy, cor pulmonale failure) |
  | arrhythmia | 28% | 62% | 61% | 4 |
  | valvular | 74% | 78% | 83% | 20 (congenital pulmonary/tricuspid valve, prosthesis) |
  | pulm_circ | 79% | 73% | 72% | 27 (obstetric pulmonary embolism, persistent fetal circulation) |
  | pvd | 97% | 71% | 47% | 50 (limb arterial embolism, atheroembolism, diabetic angiopathy) |
  | htn_uncomp | 100% | 100% | 100% | 0 |
  | htn_comp | 100% | 100% | 95% | 36 (hypertensive disease in pregnancy, Page kidney) |
  | paralysis | 89% | 79% | 73% | 110 (late-effect-of-stroke hemiplegia/monoplegia, I69.x/438.x) |
  | neuro_other | 78% | 76% | 62% | 60 (eclampsia and puerperal convulsion under "Seizure", ADEM, neonatal HIE) |
  | copd | 78% | 86% | 76% | 13 (tuberculous bronchiectasis, silicotuberculosis) |
  | dm_uncomp | 78% | 50% | 22% | 409 (by construction, see hierarchy) |
  | dm_comp | 98% | 100% | 84% | 73 |
  | hypothyroid | 57% | 82% | 62% | 1 |
  | renal_failure | 88% | 79% | 64% | 22 (hypertensive CKD in pregnancy, diabetic CKD) |
  | liver_disease | 71% | 67% | 53% | 8 |
  | pud | 50% | 67% | 63% | 12 (acute ulcers without hemorrhage) |
  | hiv | 100% | 100% | 100% | 12 (asymptomatic HIV status Z21/V08, HIV-2, HIV in pregnancy) |
  | lymphoma | 96% | 81% | 76% | 15 |
  | mets | 0% | 0% | 0% | 0 (see below) |
  | solid_tumor | 9% | 6% | 9% | 0 (see below) |
  | rheumatic | 79% | 26% | 45% | 7 |
  | coagulopathy | 69% | 79% | 74% | 78 (obstetric coagulation defects, HELLP, heparin-induced thrombocytopenia) |
  | obesity | 100% | 100% | 100% | 14 (maternal and childhood obesity) |
  | weight_loss | 80% | 80% | 83% | 8 |
  | fluid_electrolyte | 59% | 65% | 59% | 38 (neonatal electrolyte disturbance, magnesium, diabetic hyperosmolarity) |
  | blood_loss_anemia | 100% | 100% | 100% | 0 |
  | deficiency_anemia | 85% | 73% | 73% | 4 |
  | alcohol_abuse | 36% | 56% | 31% | 6 |
  | drug_abuse | 46% | 61% | 31% | 0 |
  | psychoses | 78% | 79% | 73% | 29 (substance-induced and organic psychotic/delusional disorders) |
  | depression | 84% | 57% | 67% | 7 |

  Reading the table: where recall is low (arrhythmia ICD-10, alcohol_abuse,
  drug_abuse, liver_disease, rheumatic ICD-9, pvd concept level) the ancestors
  are deliberately narrower than Quan's prefix set (intoxication, withdrawal,
  poisoning, T-code and alcoholic organ-damage concepts are left to the ICD
  path), so a standard-concept-only database will under-detect those domains.
  Where "codes outside the prefix set" is non-zero, the ancestor is slightly
  broader than Quan, and the parenthetical names what the extra concepts are.
  Every such extra group was inspected and is clinically a member of the
  domain, but it is a **deviation from Quan's list on the standard side**.

### Prefixes removed from the Quan lists (no ICD-10-CM / ICD-9-CM match)

Quan's ICD-10 lists are written for WHO ICD-10, and a few ICD-9 prefixes are
unused categories; 48 of their prefixes (40 ICD-10, 8 ICD-9) do not exist in the
ICD-10-CM or ICD-9-CM vocabularies and were removed (they could never match a vocabulary code; a
database holding WHO ICD-10 source strings would need them back):

- ICD-10(-CM), 40 prefixes: pvd I792; neuro_other G22, G41; copd J46;
  dm_uncomp E100, E120, E121, E129, E140, E141, E149; dm_comp E107, E117,
  E122-E128, E137, E142-E148; renal_failure Z491, Z492; liver_disease I982;
  hiv B21, B22, B24; solid_tumor C97; alcohol_abuse Z502, Z721; drug_abuse
  Z722; depression F204, F412.
- ICD-9-CM, 8 prefixes: hiv 043, 044; solid_tumor 166, 167, 168, 169, 177,
  178 (unused ICD-9-CM three-digit categories).

An earlier pass of this table also listed ICD-9 prefixes such as 4256, 4277,
4433-4437, 497-499, 5725-5727, 2802-2807 and 2916/2917 as removed. Those were
range expansions present in the second-hand R/Python transcriptions but they are
**not in the Quan SAS macros**, so they are not part of the CSV and are not
removals relative to Quan; that earlier removal count (65) is superseded by the
figures above.

### ICD-10-CM additions (not in Quan; flagged in the `note` column)

- `dm_uncomp`: E080, E081, E089, E090, E091, E099. `dm_comp`: E082-E086, E088,
  E092-E096, E098. ICD-10-CM has E08 (due to underlying condition) and E09
  (drug or chemical induced) which WHO ICD-10 lacks, and no E12 or E14. The
  uncomplicated/complicated split follows Quan's (E10.0/.1/.9 uncomplicated,
  E10.2-E10.8 complicated). For the uncomplicated side the resulting code
  families (E08/E09/E10/E11/E13 with .0x, .1x and .9) are the same families the
  AHRQ ICD-10-CM refined "without chronic complications" value set lists; the
  complicated side was not compared against an AHRQ list.
- `renal_failure`: Z493 (adequacy testing for dialysis; Z49.31, Z49.32), the
  ICD-10-CM successor of Quan's Z49.1/Z49.2, which no longer exist. Z490 (now
  Z49.01/Z49.02) was already present. Confirmed by the AHRQ severe-renal-failure
  value set above.
- `lymphoma`: C86 (other specified T/NK-cell lymphomas), absent from the WHO
  ICD-10 edition Quan used, present in ICD-10-CM and in the AHRQ lymphoma value
  set.

### Other deviations and known quirks

- **pud is "excluding bleeding"**: only chronic or unspecified ulcers without
  hemorrhage or perforation (ICD-10 K25.7, K25.9, K26.7, K26.9, K27.7, K27.9,
  K28.7, K28.9; ICD-9 531.7, 531.9 and the 532, 533, 534 equivalents). The
  draft used the whole K25-K28 and 531-534 blocks, which included hemorrhagic
  and perforated ulcers. The weight is 0, so only the condition count differs.
  The standard-side ancestors ("gastric/duodenal/gastrojejunal ulcer without
  hemorrhage and without perforation") also contain the acute variants.
- **alcohol_abuse keeps two Quan translation quirks**: `E52` (niacin
  deficiency, a translation of ICD-9 265.2 pellagra) and `T51` (toxic effect of
  any alcohol, including methanol and isopropanol). Kept for fidelity; marked in
  `note`. Alcoholic liver disease other than K70.0/K70.3/K70.9 is flagged by
  `liver_disease` only.
- **ICD-9 980** (toxic effect of alcohol) is a three-digit prefix as in Quan;
  the draft used `9800`, which would miss 980.1-980.9.
- **depression** keeps Quan's `F432` and ICD-9 `309` (all adjustment
  disorders) and the bipolar-depressed codes `F313-F315`, `2965`.
- **renal_failure** keeps Quan's whole `N18` (all CKD stages 1-5), not the AHRQ
  refined moderate/severe split; the standard ancestor is "Chronic kidney
  disease" (all stages) and excludes acute kidney injury, as Quan does.
- **solid_tumor** excludes non-melanoma skin cancer (C44, ICD-9 173) exactly as
  Quan does, and the ICD list has no leukemia/myeloma (myeloma is in
  `lymphoma`, as in Quan: C90.0, C90.2, ICD-9 203.0, 238.6).
- **hiv** is B20 / 042 only, as in Quan. ICD-10-CM Z21 and ICD-9 V08
  (asymptomatic infection) are not in the ICD list, but the standard ancestor
  "Human immunodeficiency virus infection" is the concept B20/042 map to and its
  descendants include asymptomatic HIV infection, so that status is flagged on
  the standard side only.
- **Gestational diabetes** (O24.x) is not in the Quan ICD lists and is excluded
  from the standard side by using the diabetes types rather than the generic
  "Diabetes mellitus" concept (201820), whose descendants include gestational
  and neonatal diabetes. The cost is that the generic concept itself is not
  matched.

### Standard-concept side: ancestors rejected

- **solid_tumor**: topographic ancestors ("Primary malignant neoplasm of trunk /
  head / neck", "Malignant neoplasm of respiratory system / digestive system /
  genitourinary organ", ...) were measured and rejected. They are the only
  ancestors that cover most solid-tumor concepts (89% of the ICD-mapped
  concepts), but they cannot exclude what Elixhauser excludes: for example
  "Primary malignant neoplasm of trunk" has 24,342 descendants, 5,028 of which
  are also lymphoma descendants (ICDO3 histology-by-site concepts such as
  "Follicular lymphoma, grade 1 of mouth, NOS") and 754 are non-melanoma skin
  cancers; the upper-limb and lower-limb ancestors are 100% skin. Using them
  would flag every ICDO3-coded lymphoma as also a solid tumor (+4) and basal
  cell carcinoma as a solid tumor. Only morphology-specific ancestors with no
  lymphoma or skin overlap are kept (malignant melanoma, Kaposi sarcoma,
  gastrointestinal stromal tumor, malignant tumor of mesothelial tissue), so
  the standard side covers about 9% of the ICD-mapped concepts. **Solid tumor
  is effectively detected from ICD source codes only.** Adding topographic
  ancestors safely would need an exclusion mechanism (exclude descendants of
  the lymphoma ancestors and of non-melanoma skin cancer) which the table
  format does not have.
- **mets**: in this vocabulary Quan's C77-C79 / 196-199 map to
  **Measurement**-domain "Metastasis to ..." concepts (and C80 / 199 to the
  generic malignant neoplasm), so 0 of the 11 ICD-mapped Condition concepts can
  be reached and the ICD-to-SNOMED cross-check is not possible for this domain.
  The generic ancestor "Metastatic malignant neoplasm" (432851, used by the
  draft) was rejected: about a quarter of its 100 descendants are leukemic
  infiltrates and lymphoma-in-lymph-node concepts. Eight specific
  "Metastatic <histology>" ancestors are kept, validated by name only
  (adenocarcinoma, squamous cell carcinoma, renal cell carcinoma, malignant
  melanoma, sarcoma, non-small cell lung cancer, small cell neuroendocrine
  carcinoma, hepatocellular carcinoma). Metastatic cancer is effectively
  detected from ICD source codes only.
- Others: `Disorder of acid-base balance` (214 descendants incl. neonatal
  acidemia, respiratory failure, inborn errors); `Renal failure syndrome`
  192359 (includes acute kidney injury, which Quan's list excludes);
  `Thrombocytopenic disorder` and `Immune thrombocytopenia` (SNOMED files
  aplastic anemia and pancytopenia beneath them); `Psychoactive substance use
  disorder` / `Substance use disorder` (include alcohol and nicotine); `Disorder
  caused by alcohol` (includes poisoning and alcohol-induced pancreatitis);
  `Cauda equina syndrome` (its descendants include 133 cauda equina tumors);
  `Cardiomyopathy` (includes hypertrophic, which Quan excludes); the generic
  `Diabetes mellitus` (gestational, see above); `Cardiac arrhythmia` (includes
  cardiac arrest, bundle branch block and first-degree block, none of which Quan
  counts).

### Per-domain confidence

ICD lists: high fidelity to Quan's published algorithm for every domain: an
exact, vocabulary-filtered copy of the Quan SAS macros (see Sources) plus the
flagged ICD-10-CM additions. The paper's Table 1 itself was not opened, so any
divergence between the macros and the paper is unchecked.

- **High** (ICD and standard side both clean): htn_uncomp, htn_comp, copd,
  obesity, hypothyroid, dm_comp, blood_loss_anemia, weight_loss, lymphoma,
  chf, deficiency_anemia.
- **Medium** (standard side valid but broader or narrower than Quan, extras
  clinically in-domain): arrhythmia, valvular, pulm_circ, pvd, paralysis,
  neuro_other, renal_failure, liver_disease, pud, rheumatic, coagulopathy,
  fluid_electrolyte, psychoses, depression, drug_abuse, alcohol_abuse, hiv.
- **Low on the standard side / effectively ICD path only**: dm_uncomp (needs
  the hierarchy; see above), mets, solid_tumor.
- Weights: medium-high. Consistent across the `comorbidity` and `comorbidipy`
  listings and the Thompson 2015 republication of the van Walraven weights, but
  the van Walraven paper's own table was not read.

### Corrections to the earlier draft

Of the draft's 73 SNOMED ids only 22 fall inside the chosen ancestors'
descendant sets. 18 are not standard Condition concepts at all. Twelve of those
are SNOMED CT SCTIDs and not OMOP concept_ids (for example 84114007 for heart
failure, whose OMOP concept_id is 316139): 84114007, 422944002, 17366009,
49436004, 368009, 70687000, 399957001, 318798, 38341003, 437750, 13645005,
313296. The other six are not Conditions: 4028300 is a fish family, 4299839 a
drug, 4173504 "Sick child", 40480436 a bacterial strain, 437525 "Overweight"
and 4141639 a uterine bleeding concept. Many existing Condition ids were simply
the wrong disease: 312327 (acute myocardial infarction) under pulmonary
circulation, 433753 (harmful pattern of use of alcohol) under fluid and
electrolyte disorders, 443767 (diabetic eye disease) as uncomplicated diabetes,
316866 (hypertensive disorder; the RFC text calls it type 2 diabetes) as
uncomplicated hypertension, 140214 (eruption) and 4032338 (cellulitis of penis)
under hypothyroidism, 437894 (ventricular fibrillation) under blood loss
anemia, 374375 (impacted cerumen) under alcohol abuse, 436665 (bipolar
disorder) and 437249 (manic episodes) under drug abuse, 444101 (hypertensive
heart failure) under psychoses, 440374 (OCD) and 439857 (parainfluenza
pneumonia) under coagulopathy, 443428 (torus fracture) under fluid and
electrolyte disorders, 4178431 (cartilage disorder) under lymphoma, 197320
(acute kidney injury) under renal failure.
Relative to the Quan lists the draft's ICD sets differed in: peptic ulcer (the
whole K25-K28 / 531-534 blocks), renal failure ICD-9 (582, 5830-5837, not in
Quan), alcohol_abuse (E244, G312, T510/T511/T519, Z502, Z721 and 9800 instead
of Quan's E52, G621, T51 and 980), depression ICD-9 (3090, 3091 instead of 309),
and it lacked ICD-9 arrhythmia 42610, 42612, 99601, 99604, neuro_other 3362 and
rheumatic 72930. All draft weights matched the weights in the CSV.
<!-- END elixhauser -->
