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

- OMOP_RT_FC1752BA: 2000 rows in 0.26 s

## Checks

| column / statistic | local | remote | match |
|---|---|---|---|
| (rows) | 2000.0 | 2000.0 | yes |
| id:non_null | 2000.0 | 2000.0 | yes |
| id:sum | 1999000.0 | 1999000.0 | yes |
| id:min | 0.0 | 0.0 | yes |
| id:max | 1999.0 | 1999.0 | yes |
| near_limit:non_null | 2000.0 | 2000.0 | yes |
| near_limit:sum | 0.0 | 0.0 | yes |
| near_limit:min | -4.611686018427388e+18 | -4.611686018427388e+18 | yes |
| near_limit:max | 4.611686018427388e+18 | 4.611686018427388e+18 | yes |
| dbl:non_null | 2000.0 | 2000.0 | yes |
| dbl:sum | 199900.0 | 199900.0 | yes |
| dbl:min | 0.0 | 0.0 | yes |
| dbl:max | 199.9 | 199.9 | yes |
| dec:non_null | 2000.0 | 2000.0 | yes |
| dec:sum | 2467765.5 | 2467765.5 | yes |
| dec:min | 0.0 | 0.0 | yes |
| dec:max | 2467.7655 | 2467.7655 | yes |
| flag:non_null | 2000.0 | 2000.0 | yes |
| d:non_null | 2000.0 | 2000.0 | yes |
| ts:non_null | 2000.0 | 2000.0 | yes |
| txt:non_null | 2000.0 | 2000.0 | yes |
| txt_empty:non_null | 667.0 | 0.0 | known difference |
| all_null:non_null | 0.0 | 0.0 | yes |
| all_null:sum | nan | nan | yes |
| all_null:min | nan | nan | yes |
| all_null:max | nan | nan | yes |
