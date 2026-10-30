# Minimal protobuf writer: just enough of the ONNX schema (ModelProto / GraphProto / NodeProto / TensorProto /
# ValueInfoProto) to serialise a logistic-blend graph with no R/Python ONNX dependency.
.pb_varint <- function(n) {
  out <- raw(0)
  repeat {
    b <- n %% 128; n <- n %/% 128
    out <- c(out, as.raw(if (n > 0) b + 128 else b))
    if (n == 0) break
  }
  out
}
.pb_tag <- function(field, wire) .pb_varint(field * 8 + wire)
.pb_int <- function(field, v) c(.pb_tag(field, 0), .pb_varint(v))
.pb_msg <- function(field, payload) c(.pb_tag(field, 2), .pb_varint(length(payload)), payload)
.pb_str <- function(field, s) .pb_msg(field, charToRaw(enc2utf8(s)))
.pb_f32 <- function(x) writeBin(as.numeric(x), raw(), size = 4L, endian = "little")

.onnx_tensor <- function(name, dims, values) {
  c(unlist(lapply(dims, function(d) .pb_int(1, d))), .pb_int(2, 1), .pb_str(8, name), .pb_msg(9, .pb_f32(values)))
}
.onnx_node <- function(op, inputs, output, name = output) {
  c(unlist(lapply(inputs, function(i) .pb_str(1, i))), .pb_str(2, output), .pb_str(3, name), .pb_str(4, op))
}
.onnx_value_info <- function(name, dims) {
  dim_msg <- unlist(lapply(dims, function(d) .pb_msg(1, if (is.character(d)) .pb_str(2, d) else .pb_int(1, d))))
  tensor_type <- c(.pb_int(1, 1), .pb_msg(2, dim_msg))
  c(.pb_str(1, name), .pb_msg(2, .pb_msg(1, tensor_type)))
}

#' Export a model bundle to one standalone ONNX file
#'
#' Serialises a bundle for C++ / Java / Go / .NET runtimes. The graph takes `float32 [N, n_features]` in
#' `bundle$feature_spec` order and returns `probabilities` (`float32 [N, 1]`), equal to [bundle_predict_proba()]. Supported:
#' `logistic` components (any number, weighted blend) with `platt` calibration or none. Anything else (isotonic
#' calibration, XGBoost, other kinds) raises an error rather than exporting a model whose output differs from the bundle's.
#' Bounds clipping and imputation are **not** in the graph: `<output_path>.features.json` lists the ordered features with
#' their bounds and `impute` values and the risk tiers, for the host system to apply. Mirror of Python
#' `omop_etl.model_bundle.export_to_onnx` (which also handles a single ONNX / XGBoost component).
#'
#' @param bundle A `model_bundle` or bundle directory.
#' @param output_path Where to write the `.onnx` file.
#' @param opset ONNX opset (default 13).
#' @return `output_path`, invisibly.
#' @export
export_to_onnx <- function(bundle, output_path, opset = 13L) {
  b <- if (inherits(bundle, "model_bundle")) bundle else load_model_bundle(bundle)
  cal <- b$calibration
  method <- if (is.null(cal)) NULL else cal$method
  if (!is.null(method) && method != "platt") stop(sprintf("ONNX export of '%s' calibration is not supported.", method), call. = FALSE)
  kinds <- unique(vapply(b$components, function(c) c$kind, character(1)))
  if (!identical(kinds, "logistic")) {
    stop(sprintf("export_to_onnx supports logistic blends only in R; this bundle has: %s.", paste(kinds, collapse = ", ")), call. = FALSE)
  }
  fnames <- vapply(b$feature_spec$features, function(f) f$name, character(1))
  nf <- length(fnames)

  nodes <- list(); inits <- list(); probs <- character(); wts <- numeric()
  const <- function(name, dims, v) { inits[[length(inits) + 1L]] <<- .onnx_tensor(name, dims, v); name }
  add <- function(op, ins, out) { nodes[[length(nodes) + 1L]] <<- .onnx_node(op, ins, out); out }
  for (i in seq_along(b$components)) {
    c <- b$components[[i]]
    s <- jsonlite::fromJSON(c$path, simplifyVector = FALSE)
    coef <- as.numeric(unlist(s$coef))
    if (length(coef) != nf) .bundle_error(sprintf("%s: %d coefficients for %d features.", basename(c$path), length(coef), nf))
    mean_ <- if (is.null(s$mean)) rep(0, nf) else as.numeric(unlist(s$mean))
    scale_ <- if (is.null(s$scale)) rep(1, nf) else as.numeric(unlist(s$scale))
    w_eff <- coef / scale_  # ((x - mean) / scale) %*% coef + b  ==  x %*% w_eff + (b - mean %*% w_eff)
    bias <- (if (is.null(s$intercept)) 0 else as.numeric(s$intercept)) - sum(mean_ * w_eff)
    z <- add("MatMul", c("X", const(paste0("W", i), c(nf, 1), w_eff)), paste0("z", i))
    zb <- add("Add", c(z, const(paste0("b", i), 1, bias)), paste0("zb", i))
    probs <- c(probs, add("Sigmoid", zb, paste0("p", i)))
    wts <- c(wts, c$weight)
  }
  acc <- NULL
  for (i in seq_along(probs)) {
    wp <- add("Mul", c(probs[[i]], const(paste0("w", i), 1, wts[[i]] / sum(wts))), paste0("wp", i))
    acc <- if (is.null(acc)) wp else add("Add", c(acc, wp), paste0("acc", i))
  }
  if (identical(method, "platt")) {
    pc <- add("Clip", c(acc, const("eps", 1, 1e-7), const("one_m_eps", 1, 1 - 1e-7)), "pc")
    q <- add("Sub", c(const("one", 1, 1), pc), "q")
    odds <- add("Div", c(pc, q), "odds")
    lg <- add("Log", odds, "lg")
    ls <- add("Mul", c(lg, const("slope", 1, as.numeric(cal$slope))), "ls")
    lc <- add("Add", c(ls, const("icpt", 1, as.numeric(cal$intercept))), "lc")
    add("Sigmoid", lc, "probabilities")
  } else {
    add("Identity", acc, "probabilities")
  }

  graph <- c(unlist(lapply(nodes, function(n) .pb_msg(1, n))), .pb_str(2, "omop_duck_db_bundle"),
             unlist(lapply(inits, function(t) .pb_msg(5, t))),
             .pb_msg(11, .onnx_value_info("X", list("N", nf))), .pb_msg(12, .onnx_value_info("probabilities", list("N", 1L))))
  json <- function(x) as.character(jsonlite::toJSON(x, auto_unbox = TRUE, null = "null", na = "null"))
  meta <- list(feature_names = json(as.list(fnames)), risk_tiers = json(b$risk_tiers),
               bundle_name = as.character(b$manifest$name), bundle_version = as.character(b$manifest$version))
  model <- c(.pb_int(1, 8), .pb_str(2, "omopduckdb"), .pb_msg(7, graph), .pb_msg(8, c(.pb_str(1, ""), .pb_int(2, as.integer(opset)))),
             unlist(lapply(names(meta), function(k) .pb_msg(14, c(.pb_str(1, k), .pb_str(2, meta[[k]]))))))
  dir.create(dirname(output_path), showWarnings = FALSE, recursive = TRUE)
  writeBin(model, output_path)
  jsonlite::write_json(list(features = b$feature_spec$features, risk_tiers = b$risk_tiers, input = "X", output = "probabilities"),
                       paste0(output_path, ".features.json"), auto_unbox = TRUE, null = "null", na = "null", digits = NA, pretty = TRUE)
  invisible(output_path)
}
