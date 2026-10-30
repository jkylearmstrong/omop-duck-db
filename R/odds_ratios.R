#' Table 3: multivariable logistic-regression odds ratios with clinically scaled increments
#'
#' Per-1-unit odds ratios of continuous predictors (systolic BP, glucose, eGFR) round to an uninformative
#' `1.00 (1.00-1.00)`. This reports them per 1 SD (or a stated increment such as 10 mmHg) instead, with Wald 95% CIs,
#' standardized betas and p-values. Mirror of Python `omop_etl.odds_ratios.generate_table3_odds_ratios`.
#'
#' @param model A fitted binomial [stats::glm()] (its coefficients and standard errors are used), or `NULL` to fit an
#'   unpenalised logistic regression here.
#' @param X Data frame / matrix of predictors, in the model's coefficient order.
#' @param y 0/1 outcome, one value per row of `X`.
#' @param feature_names Column names (default `colnames(X)`).
#' @param continuous_scale `"sd"` reports non-binary predictors per 1 SD; a named vector / list `c(systolic_bp = 10)` sets the
#'   increment per predictor (others stay per 1 unit); `NULL` keeps everything per 1 unit. 0/1 predictors are always
#'   "Yes vs No".
#' @param labels Optional named character vector of display names.
#' @param beta_places Decimals for the standardized beta (default 2).
#' @return A data.frame `Variable, Increment, OR (95% CI), Std. beta, p_value` plus numeric `or`, `ci_low`, `ci_high`.
#' @export
generate_table3_odds_ratios <- function(model, X, y, feature_names = NULL, continuous_scale = "sd", labels = NULL,
                                        beta_places = 2L) {
  A <- as.matrix(X)
  storage.mode(A) <- "double"
  names_ <- feature_names %||% colnames(X) %||% paste0("x", seq_len(ncol(A)))
  if (length(names_) != ncol(A)) stop("feature_names length does not match the columns of X.", call. = FALSE)
  yv <- as.numeric(y)
  if (length(yv) != nrow(A) || !all(yv %in% c(0, 1))) stop("y must be a 0/1 outcome with one value per row of X.", call. = FALSE)
  if (is.character(continuous_scale) && !identical(continuous_scale, "sd")) {
    stop("continuous_scale must be 'sd', a named vector, or NULL.", call. = FALSE)
  }

  if (is.null(model)) {
    d <- as.data.frame(A); colnames(d) <- paste0("v", seq_len(ncol(A)))
    model <- stats::glm(yv ~ ., data = cbind(d, yv = yv), family = stats::binomial())
  } else if (!inherits(model, "glm")) {
    stop("model must be NULL or a binomial glm.", call. = FALSE)
  }
  sm <- summary(model)$coefficients
  coefs <- sm[rownames(sm) != "(Intercept)", , drop = FALSE]
  if (nrow(coefs) != ncol(A)) stop(sprintf("model has %d coefficients for %d predictors.", nrow(coefs), ncol(A)), call. = FALSE)
  beta <- coefs[, 1]; se <- coefs[, 2]

  z95 <- stats::qnorm(0.975)
  fmt_or <- function(o, lo, hi, places = 2L, max_places = 4L) {
    d <- places
    while (d < max_places && !isTRUE(all.equal(lo, hi)) &&
           (round(lo, d) == round(hi, d) || round(o, d) %in% c(round(lo, d), round(hi, d)))) d <- d + 1L
    sprintf("%.*f (%.*f–%.*f)", d, o, d, lo, d, hi)
  }
  rows <- lapply(seq_along(names_), function(j) {
    nm <- names_[[j]]; col <- A[, j]
    sdv <- stats::sd(col, na.rm = TRUE)
    binary <- all(col[!is.na(col)] %in% c(0, 1))
    if (binary) {
      scale <- 1; inc <- "Yes vs No"
    } else if (!is.null(continuous_scale) && !is.character(continuous_scale) && !is.null(continuous_scale[[nm]])) {
      scale <- as.numeric(continuous_scale[[nm]]); inc <- sprintf("per %g-unit", scale)
    } else if (identical(continuous_scale, "sd")) {
      scale <- sdv; inc <- sprintf("per 1 SD (%s)", format(signif(sdv, 3)))
    } else {
      scale <- 1; inc <- "per 1 unit"
    }
    b <- unname(beta[[j]]); s <- unname(se[[j]])
    o <- exp(b * scale); lo <- exp((b - z95 * s) * scale); hi <- exp((b + z95 * s) * scale)
    if (scale < 0) { tmp <- lo; lo <- hi; hi <- tmp }
    p <- if (s > 0) 2 * stats::pnorm(-abs(b / s)) else NA_real_
    data.frame(Variable = if (!is.null(labels) && !is.na(labels[nm])) unname(labels[nm]) else nm,
               Increment = inc, `OR (95% CI)` = fmt_or(o, lo, hi),
               `Std. beta` = sprintf("%.*f", as.integer(beta_places), b * (if (binary) 1 else sdv)),
               p_value = if (is.na(p)) "-" else .fmt_pval(p), or = o, ci_low = lo, ci_high = hi,
               check.names = FALSE, stringsAsFactors = FALSE)
  })
  do.call(rbind, rows)
}

#' Export a Table 3 odds-ratio table
#'
#' @param table3 Result of [generate_table3_odds_ratios()].
#' @param format `"markdown"`, `"quarto"`, `"typst"`, `"latex"` or `"csv"`.
#' @param output_path Optional file to write.
#' @param caption Table caption.
#' @param ... Passed to [export_table1()].
#' @return The formatted text.
#' @export
export_table3 <- function(table3, format = "markdown", output_path = NULL, caption = NULL, ...) {
  public <- table3[, setdiff(names(table3), c("or", "ci_low", "ci_high")), drop = FALSE]
  fmt <- tolower(trimws(format))
  extra <- list(...)
  if (fmt %in% c("qmd", "quarto", "typ", "typst")) {
    if (is.null(extra$caption)) extra$caption <- caption %||% "Multivariable Logistic Regression: Odds Ratios"
    if (is.null(extra$include_loinc_footnotes)) extra$include_loinc_footnotes <- FALSE
    if (is.null(extra$label) && fmt %in% c("qmd", "quarto")) extra$label <- "tbl-table3"
  }
  do.call(export_table1, c(list(public, output_path = output_path, format = format), extra))
}
