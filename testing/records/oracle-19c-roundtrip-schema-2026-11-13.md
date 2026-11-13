# Server round-trip record

Result: **PASS**

## Server

- dialect: oracle
- driver: oracledb
- server_version: Oracle Database 19c EE Extreme Perf Release 19.0.0.0.0 - Production
- NLS_LENGTH_SEMANTICS: BYTE
- NLS_NCHAR_CHARACTERSET: AL16UTF16
- NLS_CHARACTERSET: WE8MSWIN1252
- explicit schema= given: yes

## Load

- OMOP_RT_3720F342: 2000 rows in 0.12 s

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
| dbl:sum | 199900.0 | 199900.0 | yes |
| dbl:min | 0.0 | 0.0 | yes |
| dbl:max | 199.9 | 199.9 | yes |
| dec:non_null | 2000.0 | 2000.0 | yes |
| dec:sum | 2467765.5 | 2467765.5 | yes |
| dec:min | 0.0 | 0.0 | yes |
| dec:max | 2467.7655 | 2467.7655 | yes |
| flag:non_null | 2000.0 | 2000.0 | yes |
| flag:content | 2000:5ee66aa1fc708ea0 | 2000:5ee66aa1fc708ea0 | yes |
| d:non_null | 2000.0 | 2000.0 | yes |
| d:content | 2000:c3e4a8b22ace8d1f | 2000:c3e4a8b22ace8d1f | yes |
| ts:non_null | 2000.0 | 2000.0 | yes |
| ts:content | 2000:d44c98119633f65d | 2000:d44c98119633f65d | yes |
| txt:non_null | 2000.0 | 2000.0 | yes |
| txt:content | 2000:efada0be43d289ad | 2000:efada0be43d289ad | yes |
| txt_nonlatin:non_null | 2000.0 | 2000.0 | yes |
| txt_nonlatin:content | 2000:e8ee1fdec328e0b2 | 2000:f3889091cc15bdf0 | known difference |
| txt_empty:non_null | 667.0 | 0.0 | known difference |
| txt_empty:content | 0:0000000000000000 | 0:0000000000000000 | yes |
| all_null:non_null | 0.0 | 0.0 | yes |
| all_null:sum | None | None | yes |
| all_null:min | None | None | yes |
| all_null:max | None | None | yes |

## Differing values (synthetic data)

- txt_nonlatin:content: local only: ["'日本語のテキスト 0'", "'日本語のテキスト 1'"]; remote only: ["'¿¿¿¿¿¿¿¿ 74'", "'¿¿¿¿¿¿¿¿ 75'"]

## Extended checks

| check | result | note |
|---|---|---|
| if_exists='fail' refuses an existing table | PASS | ValueError |
| if_exists='append' doubles the rows (remote equals local twice) | PASS |  |
| if_exists='replace' restores the original rows | PASS |  |
| NaN, +Infinity, -Infinity, NULL and 1.5 read back unchanged | PASS | read back: [1.5, 'nan', '+inf', '-inf', None] |
| verify_transfer on the NaN/Infinity table | PASS |  |
