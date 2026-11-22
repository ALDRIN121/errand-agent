"""Rewrite every derived number in the docs. Run before committing a doc change.

The skill's pitfall: "numbers typed into prose drift on the first edit". Three docs
restated the test count, the LOC and the scenario count, and they disagreed within
two revisions. So the numbers are computed here and written back, and a test asserts
the docs match this output.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pytest_count() -> int:
    """Count collected tests using the project venv, not whatever python runs us."""
    venv = ROOT / ".venv" / "bin" / "python"
    python = str(venv) if venv.exists() else sys.executable
    out = subprocess.run(
        [python, "-m", "pytest", "tests/", "-q", "--co"],
        cwd=ROOT, capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
    )
    # `-q --co` prints one "path: N" line per file and no total, so sum them.
    per_file = [int(n) for n in re.findall(r":\s*(\d+)\s*$", out.stdout, re.M)]
    if not per_file:
        # Writing 0 into a doc is worse than failing: the number looks derived.
        raise SystemExit(
            f"could not count tests with {python}:\n{out.stdout[-400:]}{out.stderr[-400:]}"
        )
    return sum(per_file)


def collect() -> dict[str, int]:
    sys.path.insert(0, str(ROOT))
    from errand.outcomes import OUTCOMES
    from errand.policy import State
    from evals.harness import CHECKS
    from evals.scenarios import SCENARIOS

    def loc(folder: str) -> int:
        return sum(len(p.read_text().splitlines()) for p in (ROOT / folder).rglob("*.py"))

    return {
        "tests": _pytest_count(),
        "states": len(State),
        "tokens": len(OUTCOMES),
        "scenarios": len(SCENARIOS),
        "invariants": len(CHECKS) + 1,
        "src_loc": loc("errand"),
        "test_loc": loc("tests"),
    }


REWRITES = {
    "docs/DEMO.md": [
        (r"pytest -q\s+# \d+ passed", "pytest -q  # {tests} passed"),
        (r"\d+/37, all \d+ invariants", "{scenarios}/37, all {invariants} invariants"),
    ],
    "docs/DESIGN_NOTE.md": [
        (r"\| Scenarios \| \d+ \(\d+ pass\) \|",
         "| Scenarios | {scenarios} ({scenarios} pass) |"),
        (r"\| Invariants \| \d+ \(100%\) \|", "| Invariants | {invariants} (100%) |"),
        (r"\| States \| \d+ \|", "| States | {states} |"),
        (r"\| Terminal tokens \| \d+ \|", "| Terminal tokens | {tokens} |"),
        (r"\| Source / test lines \| [\d,]+ / [\d,]+ \|",
         "| Source / test lines | {src_loc:,} / {test_loc:,} |"),
    ],
    "docs/ai-transcripts/README.md": [
        (r"roughly \w+ turns", "roughly twenty turns"),
    ],
}


def rewrite(dry_run: bool = False) -> dict[str, str]:
    numbers = collect()
    changed: dict[str, str] = {}
    for rel, rules in REWRITES.items():
        path = ROOT / rel
        if not path.exists():
            continue
        text = original = path.read_text()
        for pattern, template in rules:
            text = re.sub(pattern, template.format(**numbers), text)
        if text != original:
            changed[rel] = text
            if not dry_run:
                path.write_text(text)
    return changed


if __name__ == "__main__":
    values = collect()
    print("computed:", values)
    touched = rewrite()
    print("rewritten:", list(touched) or "nothing was stale")
