# Frozen concept vocabulary and self-supervised pre-training targets (v0.13.0).
# Mirror of the matching parts of python/omop_etl/deep_dataset.py. The vocabulary manifest is the SAME JSON file in both
# languages (checksummed token list), so a Python lead site can distribute it to R sites and back. The multimodal tensor
# extractor, intra-stay tokens, memmap/Arrow/Zarr output stay Python only (R's extract_longitudinal_tensors() uses a
# different, hash-based token layout).

.VOCAB_FORMAT <- "omop-duck-db/concept-vocabulary"
.SPECIAL_TOKENS <- c("[BOS]", "[EOS]", "[SEP]", "[PAD]", "[UNK]")

.vocab_digest <- function(tokens) digest::digest(paste(tokens, collapse = "\n"), algo = "sha256", serialize = FALSE)

#' Build a concept tokenizer (omop-learn layout)
#'
#' Ids 0-4 are `[BOS] [EOS] [SEP] [PAD] [UNK]` (PAD = 3); the concept tokens follow, sorted and de-duplicated.
#' @param concept_tokens character vector of concept token strings (for example `"201826 - condition - T2DM"`).
#' @return A list with `concept_list`, `concept_map` (named integer ids) and `vocab_size`.
#' @export
build_concept_tokenizer <- function(concept_tokens) {
  lst <- c(.SPECIAL_TOKENS, sort(unique(as.character(concept_tokens)), method = "radix"))
  list(concept_list = lst, concept_map = stats::setNames(seq_along(lst) - 1L, lst), vocab_size = length(lst))
}

#' Freeze and share a tokenizer
#'
#' `export_concept_vocabulary()` writes a checksummed JSON manifest (token list, size, SHA-256, no patient data);
#' `import_concept_vocabulary()` rebuilds the tokenizer with identical ids and refuses a corrupted or foreign file.
#' The file format is shared with the Python functions of the same names.
#' @param tokenizer from [build_concept_tokenizer()] (or any list with `concept_list`). @param path file path. @param name label.
#' @return `export_concept_vocabulary()`: the manifest (invisibly); `import_concept_vocabulary()`: the tokenizer.
#' @export
export_concept_vocabulary <- function(tokenizer, path, name = "history") {
  tokens <- as.character(tokenizer$concept_list)
  if (!identical(tokens[seq_along(.SPECIAL_TOKENS)], .SPECIAL_TOKENS)) {
    stop("Unexpected special tokens; expected ", paste(.SPECIAL_TOKENS, collapse = " "), ".", call. = FALSE)
  }
  manifest <- list(format = .VOCAB_FORMAT, format_version = 1L, name = name,
                   omop_duck_db_version = as.character(utils::packageVersion("omopduckdb")),
                   vocab_size = length(tokens), n_special_tokens = length(.SPECIAL_TOKENS), pad_token_idx = 3L,
                   unk_token_idx = 4L, mask_token_id = length(tokens), sha256 = .vocab_digest(tokens), tokens = as.list(tokens))
  writeLines(jsonlite::toJSON(manifest, auto_unbox = TRUE, pretty = TRUE), path, useBytes = TRUE)
  invisible(manifest)
}

#' @rdname export_concept_vocabulary
#' @export
import_concept_vocabulary <- function(path) {
  m <- jsonlite::fromJSON(paste(readLines(path, warn = FALSE, encoding = "UTF-8"), collapse = "\n"), simplifyVector = FALSE)
  if (!identical(m$format, .VOCAB_FORMAT)) stop(sprintf("'%s' is not a concept vocabulary manifest.", path), call. = FALSE)
  tokens <- as.character(unlist(m$tokens))
  if (!identical(.vocab_digest(tokens), m$sha256) || length(tokens) != m$vocab_size) {
    stop(sprintf("'%s' is corrupted: the token list does not match its checksum.", path), call. = FALSE)
  }
  tok <- build_concept_tokenizer(tokens[-seq_along(.SPECIAL_TOKENS)])
  if (!identical(tok$concept_list, tokens)) stop("The rebuilt tokenizer assigns different token ids than the manifest; refusing to continue.", call. = FALSE)
  tok
}

#' Masked-concept and next-visit pre-training targets
#'
#' For a token array `(N, V, L)`: each non-special position (id >= `n_special`) is selected with probability `mask_prob`;
#' a selected position becomes `[MASK]` 80 percent of the time, a random concept 10 percent, stays 10 percent. `mlm_labels`
#' holds the original id at selected positions and -100 elsewhere; `next_visit_flag[i, v]` is 1 when row `i` has a visit
#' after `v`. `[MASK]` is `vocab_size` (embedding needs `vocab_size + 1` rows). Draws use R's RNG, so for the same seed
#' they differ from the Python function; the properties (rates, labels, flags) are the same.
#' @param concept_tensor integer array. @param n_visits visits per row. @param vocab_size tokenizer size.
#' @param mask_prob selection probability. @param seed RNG seed. @param n_special number of special ids (5).
#' @return list(masked_concept_tensor, mlm_labels, next_visit_flag).
#' @export
make_pretraining_targets <- function(concept_tensor, n_visits, vocab_size, mask_prob = 0.15, seed = 20261104, n_special = 5L) {
  if (!(mask_prob > 0 && mask_prob < 1)) stop("mask_prob must be in (0, 1).", call. = FALSE)
  d <- dim(concept_tensor)
  if (length(d) != 3L) stop("concept_tensor must be a 3-d array (N, V, L).", call. = FALSE)
  set.seed(as.integer(seed %% .Machine$integer.max))
  m <- length(concept_tensor)
  u <- stats::runif(m); kind <- stats::runif(m)
  rand_tok <- sample.int(max(vocab_size - n_special, 1L), m, replace = TRUE) + n_special - 1L
  sel <- concept_tensor >= n_special & u < mask_prob
  masked <- concept_tensor
  labels <- array(-100L, dim = d)
  labels[sel] <- concept_tensor[sel]
  masked[sel & kind < 0.8] <- as.integer(vocab_size)
  swap <- sel & kind >= 0.8 & kind < 0.9
  masked[swap] <- rand_tok[swap]
  slots <- matrix(seq_len(d[[2]]) - 1L, nrow = d[[1]], ncol = d[[2]], byrow = TRUE)
  list(masked_concept_tensor = masked, mlm_labels = labels,
       next_visit_flag = (slots + 1L < matrix(as.integer(n_visits), nrow = d[[1]], ncol = d[[2]])) * 1L)
}
