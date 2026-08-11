"""Run the four frozen RAG profile experiments on rag-hard-patient-v1.

Each profile executes preflight -> smoke -> test -> report -> verify with the
same frozen qwen3-8b rewrite cache, serially, so gateway latency and rate
limits are not confounded across arms.
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
    ("20260809T090000Z-hard-current-v12", "current-v12"),
    ("20260809T090100Z-hard-source-diverse", "current-v12-source-diverse"),
    ("20260809T090200Z-hard-dual-rrf", "current-v12-dual-rrf"),
    ("20260809T090300Z-hard-dual-rrf-source-diverse", "current-v12-dual-rrf-source-diverse"),
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
