# Server round-trip record

Result: **PASS**

## Server

- dialect: oracle
- driver: oracledb
- server_version: Oracle Database 19c EE Extreme Perf Release 19.0.0.0.0 - Production
- NLS_LENGTH_SEMANTICS: BYTE
- NLS_NCHAR_CHARACTERSET: AL16UTF16
- NLS_CHARACTERSET: WE8MSWIN1252
- explicit schema= given: no (account default)

## Load

- OMOP_RT_2CA80023: 2000 rows in 0.21 s

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
| flag:content | 2000:037d91dcab725e90 | 2000:037d91dcab725e90 | yes |
| d:non_null | 2000.0 | 2000.0 | yes |
| d:content | 2000:e89e547385b07612 | 2000:e89e547385b07612 | yes |
| ts:non_null | 2000.0 | 2000.0 | yes |
| ts:content | 2000:d47224e1408a37e5 | 2000:d47224e1408a37e5 | yes |
| txt:non_null | 2000.0 | 2000.0 | yes |
| txt:content | 2000:66b4c210b96ea4c6 | 2000:66b4c210b96ea4c6 | yes |
| txt_nonlatin:non_null | 2000.0 | 2000.0 | yes |
| txt_nonlatin:content | 2000:f9fc9ea2184841cb | 2000:948ea14e75584370 | known difference |
| txt_empty:non_null | 667.0 | 0.0 | known difference |
| txt_empty:content | 0:0000000000000000 | 0:0000000000000000 | yes |
| all_null:non_null | 0.0 | 0.0 | yes |
| all_null:sum | None | None | yes |
| all_null:min | None | None | yes |
| all_null:max | None | None | yes |

## Differing values (synthetic data)

- txt_nonlatin:content: local only: ["'日本語のテキスト 0'", "'日本語のテキスト 1'"]; remote only: ["'¿¿¿¿¿¿¿¿ 38'", "'¿¿¿¿¿¿¿¿ 39'"]

## Extended checks

| check | result | note |
|---|---|---|
| if_exists='fail' refuses an existing table | PASS | ValueError |
| if_exists='append' doubles the rows (remote equals local twice) | PASS |  |
| if_exists='replace' restores the original rows | PASS |  |
| NaN, +Infinity, -Infinity, NULL and 1.5 read back unchanged | PASS | read back: [1.5, 'nan', '+inf', '-inf', None] |
| verify_transfer on the NaN/Infinity table | PASS |  |
| wrong-password error shows neither the wrong nor the real password | PASS | error codes: ORA-01017; leaked: none |
