library(omopduckdb)

lv <- list(sex = list(levels = c("Female", "Male", "Other"), reference = "Female"),
           sdi = list(levels = c("Q1", "Q2", "SDI: Missing / Unknown")))

test_that("columns are fixed regardless of which levels are present", {
  nm <- encoded_column_names(lv)
  expect_equal(nm, c("sex_Male", "sex_Other", "sdi_Q1", "sdi_Q2", "sdi_SDI: Missing / Unknown"))
  out <- encode_categoricals(data.frame(sex = c("Male", "Male"), sdi = c("Q2", "Q2"), x = 1:2), lv)
  expect_equal(names(out), c("x", nm))
  expect_equal(out$sex_Male, c(1, 1)); expect_equal(out$sdi_Q2, c(1, 1)); expect_equal(sum(out$sex_Other), 0)
})

test_that("missing, unknown and invalid specs", {
  df <- data.frame(sex = c(NA, "Alien", " Male "), sdi = "Q1")
  expect_error(encode_categoricals(df, lv), "not in the model's levels")
  out <- encode_categoricals(df, lv, unknown = "zero")
  expect_equal(out$sex_Male, c(0, 0, 1))
  expect_error(encode_categoricals(df[, "sex", drop = FALSE], lv), "missing from the frame")
  expect_error(encode_categoricals(df, list(sex = list(levels = "a", reference = "b"))), "reference")
  expect_error(encode_categoricals(df, list(sex = c("a", "a"))), "unique")
  expect_true("sex" %in% names(encode_categoricals(df, lv, unknown = "zero", keep = TRUE)))
})
