# The R ablation / run comparison tools behave like omop_etl.model_ablation (tests/test_model_ablation.py).
abl_data <- function(n = 1500, seed = 3) {
  set.seed(seed)
  x <- data.frame(s1 = rnorm(n), s2 = rnorm(n), n1 = rnorm(n), n2 = rnorm(n), age = rnorm(n))
  z <- 1.2 * x$s1 + 0.9 * x$s2 + 0.3 * x$age - 0.5
  list(x = x, y = as.integer(runif(n) < 1 / (1 + exp(-z))))
}
abl_groups <- list(signal = c("s1", "s2"), noise = c("n1", "n2"))

test_that("a useful group hurts when removed and a noise group does not", {
  d <- abl_data()
  res <- ablate_feature_groups(d$x, d$y, abl_groups, n_boot = 150, seed = 1)
  full <- res[res$mode == "full", ]
  expect_equal(full$n_features, 5); expect_equal(full$events, sum(d$y))
  sig <- res[res$mode == "drop" & res$group == "signal", ]
  noise <- res[res$mode == "drop" & res$group == "noise", ]
  expect_lt(sig$auroc_delta, -0.1); expect_lt(sig$auroc_delta_hi, 0); expect_lt(sig$auroc_p_boot, 0.05)
  expect_gt(sig$brier_delta, 0); expect_lt(sig$auprc_delta, 0)
  expect_lt(abs(noise$auroc_delta), 0.01)
  expect_lt(abs(noise$auroc_delta), abs(sig$auroc_delta) / 20)
  expect_equal(c(sig$n_features, noise$n_features), c(3, 3))
})

test_that("only mode keeps a group alone and both returns both; results are reproducible and the caller's RNG is untouched", {
  d <- abl_data()
  both <- ablate_feature_groups(d$x, d$y, abl_groups, mode = "both", n_boot = 60, seed = 1)
  expect_setequal(both$mode, c("full", "drop", "only"))
  only <- both[both$mode == "only", ]
  expect_equal(only$n_features[only$group == "signal"], 3)
  expect_gt(only$auroc_variant[only$group == "signal"], only$auroc_variant[only$group == "noise"])
  set.seed(99); before <- runif(1); set.seed(99)
  a <- ablate_feature_groups(d$x, d$y, abl_groups, n_boot = 30, seed = 7)
  expect_equal(runif(1), before)
  expect_equal(a, ablate_feature_groups(d$x, d$y, abl_groups, n_boot = 30, seed = 7))
  calls <- 0
  prior_only <- function(xt, yt, xe) { calls <<- calls + 1; rep(mean(yt), nrow(xe)) }
  flat <- ablate_feature_groups(d$x, d$y, abl_groups, fit_predict = prior_only, n_boot = 20, seed = 7)
  expect_gt(calls, 0)
  expect_lt(abs(flat$auroc_full[1] - 0.5), 0.05)
})

test_that("the ablation validates its inputs", {
  d <- abl_data(200)
  expect_error(ablate_feature_groups(d$x, d$y, list(g = "nope")), "not in features")
  expect_error(ablate_feature_groups(d$x, d$y, list(g = character(0))), "empty")
  expect_error(ablate_feature_groups(d$x, d$y + 1, abl_groups), "0/1")
  expect_error(ablate_feature_groups(d$x, d$y[-1], abl_groups), "same number")
  expect_error(ablate_feature_groups(d$x, d$y, abl_groups, mode = "x"), "mode")
  expect_error(ablate_feature_groups(d$x, rep(0L, nrow(d$x)), abl_groups), "events")
})

test_that("the default model recovers a known signal and handles missing values", {
  d <- abl_data(1200)
  d$x$s1[seq(1, 1200, by = 7)] <- NA
  p <- ridge_logistic_fit_predict(d$x[1:900, ], d$y[1:900], d$x[901:1200, ])
  expect_length(p, 300); expect_true(all(p > 0 & p < 1))
  expect_gt(.m_auroc(d$y[901:1200], p), 0.7)
})

abl_runs <- function(n = 1000, seed = 5) {
  set.seed(seed)
  y <- as.integer(runif(n) < 0.3)
  list(y = y, p = pmin(pmax(0.3 + 0.25 * (y - 0.3) + rnorm(n, 0, 0.15), 0.01), 0.99))
}

test_that("two almost identical runs are not different", {
  r <- abl_runs(); set.seed(9)
  a <- data.frame(person_id = seq_along(r$y), pred = r$p, outcome = r$y)
  b <- data.frame(person_id = seq_along(r$y), pred = pmin(pmax(r$p + rnorm(length(r$y), 0, 0.003), 0.01), 0.99))
  out <- compare_model_runs(a, b, run_names = c("v706", "v710"), n_boot = 200)
  expect_equal(out$n, list(common = 1000L, only_v706 = 0L, only_v710 = 0L, events = sum(r$y)))
  d <- out$difference
  expect_lt(abs(d$delta[d$metric == "auroc"]), 0.01)
  expect_lt(d$delta_lo[d$metric == "auroc"], 0); expect_gt(d$delta_hi[d$metric == "auroc"], 0)
  expect_gt(d$p_boot[d$metric == "auroc"], 0.05)
  expect_gt(out$agreement$spearman, 0.99); expect_gt(out$agreement$top_overlap, 0.9)
  expect_equal(out$metrics$run, c("v706", "v710"))
})

test_that("a clearly better run is detected and unmatched patients are counted", {
  r <- abl_runs(); set.seed(11)
  better <- pmin(pmax(0.3 + 0.55 * (r$y - 0.3) + rnorm(length(r$y), 0, 0.15), 0.01), 0.99)
  a <- data.frame(person_id = 1:1000, pred = r$p, outcome = r$y)
  b <- data.frame(person_id = 1:1000, pred = better, outcome = r$y)
  d <- compare_model_runs(a, b, n_boot = 200)$difference
  expect_gt(d$delta[d$metric == "auroc"], 0.02); expect_gt(d$delta_lo[d$metric == "auroc"], 0); expect_lt(d$p_boot[d$metric == "auroc"], 0.05)
  expect_lt(d$delta[d$metric == "brier"], 0)
  y_all <- c(r$y, rep(0L, 50))
  a2 <- data.frame(person_id = 1:1000, pred = r$p, outcome = y_all[1:1000])
  b2 <- data.frame(person_id = 51:1050, pred = c(better[51:1000], rep(0.3, 50)), outcome = y_all[51:1050])
  n <- compare_model_runs(a2, b2, n_boot = 20)$n
  expect_equal(c(n$common, n$only_A, n$only_B), c(950, 50, 50))
})

test_that("compare_model_runs validates its inputs", {
  r <- abl_runs(100)
  a <- data.frame(person_id = 1:100, pred = r$p, outcome = r$y)
  expect_error(compare_model_runs(rbind(a, a), a), "duplicate")
  expect_error(compare_model_runs(a[, c("person_id", "outcome")], a), "no column")
  expect_error(compare_model_runs(a[, c("person_id", "pred")], a[, c("person_id", "pred")]), "neither run")
  expect_error(compare_model_runs(a, transform(a, outcome = 1 - outcome)), "disagree")
  expect_error(compare_model_runs(a, transform(a, person_id = person_id + 1000)), "in common")
})
