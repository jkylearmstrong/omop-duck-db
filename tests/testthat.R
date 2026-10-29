library(testthat)
library(omopduckdb)

# The check reporter fails the run as before. The location reporter prints the file and line of every test as it
# starts, so a hard crash of the R process (no R error, seen intermittently on Windows) names the test that was
# running in tests/testthat.Rout instead of ending on the last unrelated message.
test_check("omopduckdb", reporter = MultiReporter$new(list(CheckReporter$new(), LocationReporter$new())))
