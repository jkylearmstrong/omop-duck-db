"""Regenerate ``tests/fixtures/sanitize_expected.csv``, the golden result for sanitize_measurements().

The rows come from the independent pure-Python/numpy reference model in
``tests/test_sanitize_measurements.py`` (``golden_rows()``); this script never calls
``sanitize_measurements()``, so the golden file stays an independent statement of the rules. Both the Python
and the R tests assert the implementation against the file this script writes.

Run it after changing ``inst/extdata/physiologic_limits.csv`` or ``tests/fixtures/sanitize_measurements.csv``
and review the resulting diff by hand::

    python tests/fixtures/sanitize_make_golden.py
"""

import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "python"))

from test_sanitize_measurements import golden_rows  # noqa: E402


def _fmt(value):
    # 12 significant digits are plenty: the tests compare with a 1e-9 relative tolerance
    return "" if value is None else format(value, ".12g")


def main() -> None:
    out = HERE / "sanitize_expected.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["method", "measurement_id", "sanitize_status", "value_nullify", "value_clamp"])
        for method, mid, status, nullified, clamped in golden_rows():
            writer.writerow([method, mid, status, _fmt(nullified), _fmt(clamped)])
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
