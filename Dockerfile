# syntax=docker/dockerfile:1.6

# Multi-stage build for R package: omopduckdb
# (also installs and tests the Python package, omop-duck-db, since this repo
# ships both -- usethis::use_dockerfile() only knows about the R side)
#
# NOTE on the repos date: usethis::use_dockerfile() date-pins the Posit
# Package Manager snapshot to Sys.Date() at generation time. On this machine
# that produced a *future* date (2026-07-31) that Posit's snapshot service
# won't have data for yet in the real world -- swapped for the rolling
# `/latest` alias instead. Worth fixing upstream in the PR: pinning to
# Sys.Date() is only safe if that's guaranteed to be <= the real current date.

# Stage 1: builder
FROM rocker/r-ver:4.4.2 AS builder

# The repo's .Rprofile activates renv on load; renv's own project library is
# irrelevant here (this image installs straight into /rlib instead), and
# without this it silently swaps .libPaths() to an empty renv library,
# hiding everything just installed. Same fix needed locally on Windows.
ENV RENV_CONFIG_AUTOLOADER_ENABLED=FALSE
ENV R_LIBS=/rlib
WORKDIR /build

# Install system dependencies (git from the generator; python3 added so this
# stage can also build the Python package)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git python3 python3-pip python3-venv libicu-dev \
    && rm -rf /var/lib/apt/lists/*

# Install R packages into a dedicated library
RUN mkdir -p /rlib && Rscript -e 'install.packages(c("bigrquery", "CommonDataModel", "DBI", "duckdb", "googledrive", "quarto", "stringr", "testthat", "usethis", "yaml"), repos = "https://packagemanager.posit.co/cran/__linux__/noble/latest", lib = "/rlib")'

# Build + test both packages in the builder stage, so a failing test fails
# the image build, not just a later `docker run`.
COPY . /build
RUN R_LIBS=/rlib R CMD INSTALL --no-multiarch --with-keep.source --install-tests /build --library=/rlib
RUN cd /build/tests && R_LIBS=/rlib Rscript testthat.R

RUN python3 -m venv /pyvenv \
    && /pyvenv/bin/pip install --no-cache-dir "/build[dev]" \
    && R_LIBS=/rlib PATH="/pyvenv/bin:$PATH" /pyvenv/bin/python -m pytest /build/tests -v

# Stage 2: final runtime
FROM rocker/r-ver:4.4.2

LABEL org.opencontainers.image.title="omopduckdb"

# Create non-root user (skip if already exists)
RUN id -u ruser >/dev/null 2>&1 || useradd -m -u 1000 ruser

# Install runtime system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        git python3 python3-venv libicu-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Copy R packages and the built Python venv from builder
COPY --from=builder /rlib /rlib
COPY --from=builder /pyvenv /pyvenv
ENV RENV_CONFIG_AUTOLOADER_ENABLED=FALSE
ENV R_LIBS=/rlib
ENV PATH="/pyvenv/bin:$PATH"

# Copy project source
COPY --chown=ruser:ruser . .

# Ensure working directory is owned by non-root user
RUN chown -R ruser:ruser /workspace

# Switch to non-root user
USER ruser

# Default: R interactive
CMD ["R", "--no-save"]
