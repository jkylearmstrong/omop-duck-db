library(dplyr)
library(duckplyr)

con <- DBI::dbConnect(duckdb::duckdb(), dbdir = "omop_cdm.duckdb", read_only = TRUE)
concept <- tbl(con, "concept")
concept_relationship <- tbl(con, "concept_relationship")

cat("LOINC count:\n")
concept %>%
    filter(vocabulary_id == "LOINC") %>%
    filter(concept_code == "LP6118-6") %>%
    collect() %>% print()

cat("\nMaps To standard concept id for LOINC code LP6118-6:\n")
loinc_concept <- concept %>%
    filter(vocabulary_id == "LOINC", concept_code == "LP6118-6") %>%
    select(concept_id)

standard_concept_id <- concept_relationship %>%
    inner_join(loinc_concept, by = join_by(concept_id_1 == concept_id)) %>%
    filter(relationship_id == "Maps to") %>%
    collect()
print(standard_concept_id)

cat("\nSubsumes relationship?\n")
concept_relationship %>%
    inner_join(loinc_concept, by = join_by(concept_id_1 == concept_id)) %>%
    collect() %>% print()

DBI::dbDisconnect(con, shutdown=TRUE)
