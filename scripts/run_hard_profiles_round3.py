"""Re-run the current-v12 baseline under the current evaluator revision.

Round 2 added new retrieval profiles, which changes the evaluator source
identity recorded by preflight.  The original A/B/C/D runs were verified
before that change; this script re-runs the current-v12 reference arm so the
round-2 head-to-head comparisons (A vs G1, A vs E) share one code identity.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_ROOT = next((PROJECT_ROOT / "docs").glob("05_RAG*/runs"))
DATASET_DIR = PROJECT_ROOT / "data" / "rag_eval_hard_patient" / "v1"
CACHE_PATH = next(RUNS_ROOT.glob("20260809T083017Z-hard-qwen3-8b-rewrites.json"))
COLLECTION = "xuanhu_knowledge_v4"

PROFILES = [
    ("20260809T100000Z-hard-current-v12-r2", "current-v12"),
]


def run_command(argv: list[str]) -> None:
    command = " ".join(str(part) for part in argv)
    print(f"RUN {command}", flush=True)
    result = subprocess.run(argv, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {command}")
    print(f"OK {command}", flush=True)


def main() -> int:
    python = sys.executable
    evaluator = PROJECT_ROOT / "scripts" / "evaluate_rag_silver.py"
    for run_name, profile in PROFILES:
        run_dir = RUNS_ROOT / run_name
        print(f"\n===== PROFILE {profile} -> {run_name} =====", flush=True)
        run_command(
            [
                python,
                "-X",
                "utf8",
                str(evaluator),
                "preflight",
                "--dataset-dir",
                str(DATASET_DIR),
                "--run-dir",
                str(run_dir),
                "--collection",
                COLLECTION,
                "--profile",
                profile,
                "--rewrite-cache",
                str(CACHE_PATH),
            ]
        )
        for split in ("smoke", "test"):
            run_command(
                [
                    python,
                    "-X",
                    "utf8",
                    str(evaluator),
                    "run",
                    "--dataset-dir",
                    str(DATASET_DIR),
                    "--run-dir",
                    str(run_dir),
                    "--collection",
                    COLLECTION,
                    "--profile",
                    profile,
                    "--split",
                    split,
                    "--arms",
                    "baseline,full",
                    "--top-k",
                    "8",
                    "--rewrite-cache",
                    str(CACHE_PATH),
                ]
            )
        run_command(
            [
                python,
                "-X",
                "utf8",
                str(evaluator),
                "report",
                "--dataset-dir",
                str(DATASET_DIR),
                "--run-dir",
                str(run_dir),
                "--bootstrap-samples",
                "10000",
                "--seed",
                "20260807",
            ]
        )
        run_command(
            [
                python,
                "-X",
                "utf8",
                str(evaluator),
                "verify",
                "--dataset-dir",
                str(DATASET_DIR),
                "--run-dir",
                str(run_dir),
            ]
        )
        print(f"===== DONE {profile} =====", flush=True)
    print("ALL_PROFILES_COMPLETE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
