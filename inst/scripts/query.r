# 1. Create a new DuckDB database file
db_path <- "omop_cdm.duckdb"
con <- DBI::dbConnect(
    duckdb::duckdb(),
    dbdir = db_path,
    read_only = TRUE
)

DBI::dbListTables(con)

library("dplyr")
library("duckplyr")

concept <- tbl(con, "concept")
concept_relationship <- tbl(con, "concept_relationship")

# List of target LOINCs provided by user
TARGET_LOINCS <- c(
    "1751-7" = "Albumin (1751-7)",
    "33037-3" = "Anion gap (33037-3)",
    "15152-2" = "Bilirubin, Conjugated (15152-2)",
    "34543-9" = "Bilirubin, Direct/Total (34543-9)",
    "3094-0" = "BUN S/P (3094-0)",
    "6299-2" = "BUN Blood (6299-2)",
    "42757-5" = "Troponin I, Blood (42757-5)",
    "10839-9" = "Troponin I, S/P (10839-9)",
    "26464-8" = "Leukocytes, Blood (26464-8)",
    "6690-2" = "Leukocytes, Auto (6690-2)",
    "1920-8" = "AST (1920-8)",
    "4548-4" = "HbA1c (4548-4)",
    "13457-7" = "LDL Calc (13457-7)",
    "42637-3" = "BNP (42637-3)",
    "33762-6" = "NT-proBNP (any)",
    "71425-3" = "NT-proBNP (any)",
    "33763-4" = "NT-proBNP (any)",
    "47087-2" = "NT-proBNP (any)",
    "83107-3" = "NT-proBNP (any)"
)

#' Get OMOP Concept Hierarchy (Roll-up or Drill-down)
#'
#' @param con Database connection
#' @param input_codes Named character vector of source concept codes (e.g. TARGET_LOINCS)
#' @param vocab_id string specifying vocabulary_id of the inputs (default "LOINC")
#' @param direction "ancestors" (rolls up to broader classes) or "descendants" (drills down)
get_concept_hierarchy <- function(con, input_codes, vocab_id = "LOINC", direction = "ancestors") {
    # Convert named list to a local dataframe
    input_df <- tibble::tibble(
        concept_code = names(input_codes),
        input_label = as.character(input_codes)
    )

    # Upload inputs to DuckDB as a temporary table for fast joining against OMOP
    input_tbl <- copy_to(con, input_df, "input_codes_tmp", overwrite = TRUE, temporary = TRUE)

    # Step 1: Link raw user input codes to their OMOP concept_id
    base_concepts <- tbl(con, "concept") |>
        filter(vocabulary_id == vocab_id) |>
        inner_join(input_tbl, by = "concept_code") |>
        select(
            input_concept_id = concept_id,
            input_concept_code = concept_code,
            input_label
        )

    concept_anc <- tbl(con, "concept_ancestor")
    concept_table <- tbl(con, "concept")

    if (direction == "ancestors") {
        # ROLL-UP: Take specific tests and find all broader classes/groups they belong to
        results <- base_concepts |>
            inner_join(concept_anc, by = join_by(input_concept_id == descendant_concept_id)) |>
            inner_join(concept_table, by = join_by(ancestor_concept_id == concept_id)) |>
            select(
                input_label,
                input_concept_code,
                min_levels_of_separation,
                max_levels_of_separation,
                related_concept_id = ancestor_concept_id,
                related_concept_code = concept_code,
                related_concept_name = concept_name,
                related_vocabulary = vocabulary_id,
                related_concept_class = concept_class_id
            )
    } else {
        # DRILL-DOWN: Take broad components/groups and find all narrow specific tests
        results <- base_concepts |>
            inner_join(concept_anc, by = join_by(input_concept_id == ancestor_concept_id)) |>
            inner_join(concept_table, by = join_by(descendant_concept_id == concept_id)) |>
            select(
                input_label,
                input_concept_code,
                min_levels_of_separation,
                max_levels_of_separation,
                related_concept_id = descendant_concept_id,
                related_concept_code = concept_code,
                related_concept_name = concept_name,
                related_vocabulary = vocabulary_id,
                related_concept_class = concept_class_id
            )
    }

    return(results |> collect())
}

# Example 1: Run an Ancestor "Roll-up" for the entire LOINC list!
cat("\nRunning Ancestor Roll-up for TARGET_LOINCS...\n")
roll_up_results <- get_concept_hierarchy(con, TARGET_LOINCS, direction = "ancestors")

# Show the results (Order by min_levels to see immediate parents first)
roll_up_results <- roll_up_results |>
    arrange(input_label, min_levels_of_separation)

roll_up_results |>
    # select(input_label, related_concept_name, related_vocabulary, min_levels_of_separation) |>
    glimpse()

cat("\n(Query completed. Data contains", nrow(roll_up_results), "hierarchical connections across", length(TARGET_LOINCS), "targets.)\n")

# Example 2: Drill down using a specific Ancestor ID
# For instance, if you found a SNOMED class from the roll-up that represents "eGFR",
# you could find all specific LOINC descendants belonging to it!


if (!dir.exists("out")) {
    dir.create("out")
}

roll_up_results |>
    readr::write_csv(
        here::here("out", "roll_up_results.csv")
    )
