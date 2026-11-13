# Server round-trip record

Result: **PASS**

## Server

- dialect: oracle
- driver: oracledb
- server_version: Oracle Database 19c EE Extreme Perf Release 19.0.0.0.0 - Production
- NLS_LENGTH_SEMANTICS: BYTE
- NLS_NCHAR_CHARACTERSET: AL16UTF16
- NLS_CHARACTERSET: WE8MSWIN1252

## Load

- OMOP_RT_A86C4C35: 2000 rows in 0.23 s

## Checks

| column / statistic | local | remote | match |
|---|---|---|---|
| (rows) | 2000 | 2000 | yes |
| id:non_null | 2000.0 | 2000.0 | yes |
| id:sum | 1999000.0 | 1999000.0 | yes |
| id:min | 0.0 | 0.0 | yes |
| id:max | 1999.0 | 1999.0 | yes |
| near_limit:non_null | 2000.0 | 2000.0 | yes |
| near_limit:sum | 0.0 | 0.0 | yes |
| near_limit:min | -4.611686018427388e+18 | -4.611686018427388e+18 | yes |
| near_limit:max | 4.611686018427388e+18 | 4.611686018427388e+18 | yes |
| dbl:non_null | 2000.0 | 2000.0 | yes |
| dbl:sum | 199900.0 | 199899.99999999997 | yes |
| dbl:min | 0.0 | 0.0 | yes |
| dbl:max | 199.9 | 199.9 | yes |
| dec:non_null | 2000.0 | 2000.0 | yes |
| dec:sum | 2467765.5 | 2467765.5 | yes |
| dec:min | 0.0 | 0.0 | yes |
| dec:max | 2467.7655 | 2467.7655 | yes |
| flag:non_null | 2000.0 | 2000.0 | yes |
| flag:content | 2000:5dae1e1f656154e0 | 2000:5dae1e1f656154e0 | yes |
| d:non_null | 2000.0 | 2000.0 | yes |
| d:content | 2000:8b6115a392d290ce | 2000:8b6115a392d290ce | yes |
| ts:non_null | 2000.0 | 2000.0 | yes |
| ts:content | 2000:d1e4e7b2ba0fb370 | 2000:d1e4e7b2ba0fb370 | yes |
| txt:non_null | 2000.0 | 2000.0 | yes |
| txt:content | 2000:e3e23769a7cebee1 | 2000:e3e23769a7cebee1 | yes |
| txt_nonlatin:non_null | 2000.0 | 2000.0 | yes |
| txt_nonlatin:content | 2000:0213ff07cc530b07 | 2000:0213ff07cc530b07 | yes |
| txt_empty:non_null | 667.0 | 0.0 | known difference |
| txt_empty:content | 0:0000000000000000 | 0:0000000000000000 | yes |
| all_null:non_null | 0.0 | 0.0 | yes |
| all_null:sum | None | None | yes |
| all_null:min | None | None | yes |
| all_null:max | None | None | yes |
