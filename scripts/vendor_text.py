"""Read a vendor PDF's text layer.

The reviewed extract under ``docs/vendor/*_extracted.txt`` is the build input,
not the PDF. It is byte-identical to ``pdftotext -layout`` output, so the
generators produce the same tables on a machine with no poppler - which is what
CI is, and what the generated-table drift gate depends on. Re-extracting is the
fallback for a freshly dropped PDF that has no reviewed extract yet; commit the
result so the next build is deterministic again.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

PDFTOTEXT_ARGS = ("-layout",)


def vendor_lines(pdf: Path, extract: Path) -> List[str]:
    """Lines of *extract*, re-extracting *pdf* only when it is absent."""
    if extract.is_file():
        return extract.read_text(encoding="utf-8", errors="replace").splitlines()
    if not shutil.which("pdftotext"):
        sys.exit(
            f"{extract.name} is missing and pdftotext is not on PATH. Either "
            f"restore the reviewed extract or install poppler (brew install "
            f"poppler) and re-run to regenerate it."
        )
    if not pdf.is_file():
        sys.exit(f"Neither {extract.name} nor {pdf.name} is present.")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "extract.txt"
        subprocess.run(  # noqa: S603 - fixed native executable, no shell
            ["pdftotext", *PDFTOTEXT_ARGS, str(pdf), str(out)],
            check=True, capture_output=True,
        )
        # splitlines() also splits pdftotext's form-feed page breaks, which is
        # what the table parsers want: every row lands on its own entry.
        return out.read_text(encoding="utf-8", errors="replace").splitlines()
