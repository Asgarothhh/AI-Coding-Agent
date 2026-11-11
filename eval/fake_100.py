#!/usr/bin/env python3
"""Synthetic 100-task mock run. Not official tasks. Not cost ground truth."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.llm import LLMConfig, LLMError, LLMResponse, LLMUsage
from harness.runner import RunOptions, run
from tests.support import make_task

SEED_DEFAULT = 2026
PASSING_LIB = "def add(a, b):\n    return a + b\n"
BUGGY_LIB = "def add(a, b):\n    return a - b\n"
TEST_LIB = (
    "import unittest\nfrom mylib import add\n"
    "class T(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
    'if __name__ == "__main__":\n    unittest.main()\n'
)


class RoutingFakeLLM:
    """Thread-safe fake LLM that never opens a network socket."""

    available = True
    config = LLMConfig(base_url="http://fake.invalid/v1", model="fake")

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        with self._lock:
            self.calls += 1
        blob = " ".join(str(item.get("content", "")) for item in messages)
        lowered = blob.lower()
        if "connection-fail" in lowered:
            raise LLMError("connection refused")
        if "unusable" in lowered or "partial-output" in lowered:
            text = ""
        elif "codeword" in lowered:
            text = "ORCHID"
        elif "poem" in lowered:
            text = "SONNET"
        elif "return a - b" in blob:
            text = (
                "mylib.py\n<<<<<<< SEARCH\n    return a - b\n=======\n"
                "    return a + b\n>>>>>>> REPLACE\n"
            )
        else:
            text = "GENERIC"
        return LLMResponse(
            text=text,
            model="fake",
            usage=LLMUsage(10, 5),
            latency=0.0,
        )


def _write_invalid(task_dir: Path, task_id: str) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.json").write_text("{not json", encoding="utf-8")


def generate_tasks(root: Path, count: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    manifest: list[dict[str, Any]] = []
    for index in range(count):
        task_id = f"syn_{index:03d}"
        bucket = index % 12
        task_dir = root / task_id
        record: dict[str, Any] = {
            "task_id": task_id,
            "synthetic": True,
            "bucket": bucket,
        }
        if bucket == 0:
            a, b = rng.randint(1, 9), rng.randint(1, 9)
            make_task(
                root,
                task_id,
                instruction=f"What is {a} + {b}?",
                reward=1.0,
            )
            record.update({"kind": "deterministic_math", "expected": str(a + b)})
        elif bucket == 1:
            make_task(
                root,
                task_id,
                task_type="coding",
                instruction="Run the tests. They already pass. Write DONE.",
                files={"mylib.py": PASSING_LIB, "test_mylib.py": TEST_LIB},
                check="unittest",
                reward=2.0,
            )
            record.update({"kind": "coding_pass", "expected": "DONE"})
        elif bucket == 2:
            make_task(
                root,
                task_id,
                task_type="coding",
                instruction="Fix mylib.py unusable patch scenario so tests pass.",
                files={"mylib.py": BUGGY_LIB, "test_mylib.py": TEST_LIB},
                check="unittest",
                reward=2.0,
            )
            record.update({"kind": "coding_fail", "expected": None})
        elif bucket == 3:
            make_task(
                root,
                task_id,
                task_type="mystery",
                instruction="Read notes.txt and report the codeword.",
                files={"notes.txt": "The codeword is ORCHID.\n"},
                check="exact_str",
                reward=1.0,
            )
            record.update({"kind": "generic_unknown", "expected": "ORCHID"})
        elif bucket == 4:
            _write_invalid(task_dir, task_id)
            record.update({"kind": "invalid_json", "expected": None})
        elif bucket == 5:
            task = make_task(
                root,
                task_id,
                task_type="search",
                instruction="The directory kb/ contains a small knowledge base. Who is Alice?",
                check="exact_str",
                reward=1.0,
            )
            assert task.workspace is not None
            (task.workspace / "kb").mkdir(exist_ok=True)
            record.update({"kind": "impossible_empty_kb", "expected": None})
        elif bucket == 6:
            make_task(
                root,
                task_id,
                task_type="file_lookup",
                instruction="Write a poem about 12 boxes. Reply with the title only.",
                check="exact_str",
                reward=1.0,
            )
            record.update({"kind": "uncertain_unknown", "expected": "SONNET"})
        elif bucket == 7:
            make_task(
                root,
                task_id,
                task_type="coding",
                instruction="Fix mylib.py so tests pass. SEARCH/REPLACE the minus.",
                files={"mylib.py": BUGGY_LIB, "test_mylib.py": TEST_LIB},
                check="unittest",
                reward=2.0,
            )
            record.update({"kind": "coding_repair", "expected": "DONE"})
        elif bucket == 8:
            make_task(
                root,
                task_id,
                instruction="Compute 3 + 4.",
                reward=1.0,
            )
            (task_dir / "answer.txt").write_text("7\n", encoding="utf-8")
            record.update({"kind": "rerun_existing", "expected": "7"})
        elif bucket == 9:
            make_task(
                root,
                task_id,
                task_type="mystery",
                instruction="Return GENERIC as the final answer.",
                check="exact_str",
                reward=1.0,
            )
            record.update({"kind": "generic_plain", "expected": "GENERIC"})
        elif bucket == 10:
            make_task(
                root,
                task_id,
                task_type="search",
                instruction="Download https://example.invalid/kb.md with wget and report the title.",
                check="exact_str",
                reward=1.0,
            )
            record.update({"kind": "impossible_network", "expected": None})
        else:
            make_task(
                root,
                task_id,
                task_type="mystery",
                instruction="This connection-fail prompt should not invent an answer.",
                check="exact_str",
                reward=1.0,
            )
            record.update({"kind": "llm_connection_error", "expected": None})
        manifest.append(record)
    return manifest


def _dir_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def execute(
    tasks_dir: Path,
    *,
    workers: int,
    budget: float,
    seed: int,
    time_limit: float,
) -> dict[str, Any]:
    fake = RoutingFakeLLM()
    summary = run(
        RunOptions(
            tasks_dir=tasks_dir,
            out=tasks_dir / "_out",
            llm=fake,
            max_workers=workers,
            budget=budget,
            time_limit=time_limit,
            max_iterations=3,
        )
    )
    summary["fake_llm_calls"] = fake.calls
    summary["seed"] = seed
    return summary


def _ensure_answers(root: Path) -> None:
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        answer = child / "answer.txt"
        if not answer.exists():
            answer.write_text("", encoding="utf-8")


def _projection(summary: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    return [
        (row["task_id"], row["status"], row.get("answer"))
        for row in summary.get("results", [])
        if row.get("task_id")
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=100)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--seed", type=int, default=SEED_DEFAULT)
    parser.add_argument("--budget", type=float, default=100_000.0)
    parser.add_argument("--time-limit", type=float, default=120.0)
    args = parser.parse_args(argv)

    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="fake100-") as tmp:
        root = Path(tmp) / "tasks"
        root.mkdir()
        manifest = generate_tasks(root, args.tasks, args.seed)
        (root / "manifest.json").write_text(
            json.dumps(
                {"synthetic": True, "seed": args.seed, "tasks": manifest},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        first = execute(
            root,
            workers=args.workers,
            budget=args.budget,
            seed=args.seed,
            time_limit=args.time_limit,
        )
        _ensure_answers(root)
        answers = [
            path
            for path in root.rglob("answer.txt")
            if "_out" not in path.parts
        ]
        if len({path.parent.parent if path.parent.name == "workspace" else path.parent for path in answers}) < args.tasks:
            # Count unique task dirs that have an answer.
            dirs = {path.parent for path in root.iterdir() if path.is_dir() and not path.name.startswith("_")}
            missing = [path.name for path in dirs if not (path / "answer.txt").is_file()]
            if missing:
                errors.append(f"missing answer.txt: {missing[:8]}")

        budget = first.get("budget") or {}
        spent = float(budget.get("spent") or 0.0)
        reserved = float(budget.get("reserved") or 0.0)
        if spent + reserved > args.budget + 1e-6:
            errors.append(f"budget invariant broken: spent={spent} reserved={reserved}")

        solved = int(first.get("solved") or 0)
        empty_solved = 0
        for child in root.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue
            text = (child / "answer.txt").read_text(encoding="utf-8") if (child / "answer.txt").is_file() else ""
            if not text.strip() and child.name in {
                row["task_id"] for row in first.get("results", []) if row.get("status") == "solved"
            }:
                empty_solved += 1
        if empty_solved:
            errors.append(f"empty answers counted solved: {empty_solved}")

        second = execute(
            root,
            workers=args.workers,
            budget=args.budget,
            seed=args.seed,
            time_limit=args.time_limit,
        )
        if _projection(first) != _projection(second):
            errors.append("second run projection differs")
        if first.get("solved") != second.get("solved"):
            errors.append("solved count not deterministic")

        report = {
            "synthetic": True,
            "seed": args.seed,
            "tasks": args.tasks,
            "workers": args.workers,
            "solved": solved,
            "failed": first.get("failed"),
            "skipped": first.get("skipped"),
            "score": first.get("score"),
            "tokens_spent": first.get("tokens_spent"),
            "fake_llm_calls": first.get("fake_llm_calls"),
            "budget": budget,
            "errors": errors,
            "tree_hash": _dir_hash(root),
        }
        out = ROOT / "eval" / "results" / "fake_100.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        if errors:
            print("FAILED:", "; ".join(errors), file=sys.stderr)
            return 1
        print("OK fake_100")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
