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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.llm import LLMConfig, LLMError, LLMResponse, LLMUsage
from harness.runner import RunOptions, run
from harness.task import load_tasks
from tests.support import make_task

KNOWN_REPORT_STATUSES = frozenset({"solved", "failed", "skipped", "error", "invalid"})
_RUNNER_STATUSES = frozenset({"solved", "failed", "skipped", "error"})
_STATUS_ORDER = ("solved", "failed", "skipped", "error", "invalid")
INVALID_LOAD_REASON = "load_skipped:invalid_json"
_HISTORICAL_REPORTS = frozenset(
    {
        ROOT / "eval" / "results" / "fake_100.json",
        ROOT / "eval" / "results" / "fake_100_baseline.json",
    }
)

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


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _reason_from_runner(row: dict[str, Any]) -> str | None:
    notes = row.get("notes")
    if notes:
        return str(notes)
    error = row.get("error")
    if error:
        return str(error)
    return None


def _terminal_record(
    item: dict[str, Any],
    *,
    raw_status: Any,
    report_status: Any,
    reason: str | None,
    answer: Any = None,
    reward: Any = None,
    solver: Any = None,
    notes: Any = None,
) -> dict[str, Any]:
    return {
        "task_id": item.get("task_id"),
        "bucket": item.get("bucket"),
        "kind": item.get("kind"),
        "expected": item.get("expected"),
        "raw_status": raw_status,
        "report_status": report_status,
        "reason": reason,
        "answer": answer,
        "reward": reward,
        "solver": solver,
        "notes": notes,
    }


def reconcile_records(
    manifest: list[dict],
    loaded_ids: set[str],
    runner_results: list[dict],
) -> tuple[list[dict], list[str]]:
    """One terminal record per generated task_id. problems lists missing/duplicate/unknown/incomplete."""

    problems: list[str] = []
    manifest_ids = {
        str(item.get("task_id")) for item in manifest if item.get("task_id")
    }
    first_by_id: dict[str, dict[str, Any]] = {}
    for row in runner_results:
        task_id = row.get("task_id")
        if not task_id:
            problems.append("missing_task_id:runner_result")
            continue
        task_id = str(task_id)
        if task_id in first_by_id:
            problems.append(f"duplicate:{task_id}")
            continue
        first_by_id[task_id] = row
        if task_id not in manifest_ids:
            problems.append(f"unknown_id:{task_id}")

    for loaded_id in sorted(loaded_ids):
        if loaded_id not in manifest_ids:
            problems.append(f"loaded_unknown:{loaded_id}")

    records: list[dict[str, Any]] = []
    for item in manifest:
        task_id = str(item.get("task_id") or "")
        if task_id not in loaded_ids:
            if task_id in first_by_id:
                problems.append(f"result_for_unloaded:{task_id}")
            records.append(
                _terminal_record(
                    item,
                    raw_status=None,
                    report_status="invalid",
                    reason=INVALID_LOAD_REASON,
                )
            )
            continue

        runner = first_by_id.get(task_id)
        if runner is None:
            problems.append(f"missing_terminal:{task_id}")
            records.append(
                _terminal_record(
                    item,
                    raw_status=None,
                    report_status=None,
                    reason="missing_terminal",
                )
            )
            continue

        raw_status = runner.get("status")
        if raw_status in _RUNNER_STATUSES:
            report_status: Any = raw_status
        else:
            problems.append(f"unknown_status:{task_id}:{raw_status}")
            report_status = raw_status
        records.append(
            _terminal_record(
                item,
                raw_status=raw_status,
                report_status=report_status,
                reason=_reason_from_runner(runner),
                answer=runner.get("answer"),
                reward=runner.get("reward"),
                solver=runner.get("solver"),
                notes=runner.get("notes"),
            )
        )
    return records, problems


def _checked_score(records: list[dict]) -> float:
    total = 0.0
    for rec in records:
        expected = rec.get("expected")
        if expected is None:
            continue
        answer = rec.get("answer")
        if answer is None:
            continue
        if _as_text(answer).strip() == _as_text(expected).strip():
            total += float(rec.get("reward") or 0.0)
    return round(total, 6)


def summarize_records(records: list[dict]) -> dict:
    """Counts by report_status derived ONLY from records. includes generated, loaded, invalid_ids, by_status, complete."""

    by_status_raw: dict[str, int] = {}
    invalid_ids: list[str] = []
    for rec in records:
        status = rec.get("report_status")
        if status is None:
            continue
        key = str(status)
        by_status_raw[key] = by_status_raw.get(key, 0) + 1
        if key == "invalid":
            invalid_ids.append(str(rec.get("task_id") or ""))

    by_status: dict[str, int] = {}
    for key in _STATUS_ORDER:
        if key in by_status_raw:
            by_status[key] = by_status_raw[key]
    for key, value in by_status_raw.items():
        if key not in by_status:
            by_status[key] = value

    generated = len(records)
    known_total = sum(by_status.get(status, 0) for status in KNOWN_REPORT_STATUSES)
    complete = known_total == generated and all(
        rec.get("report_status") in KNOWN_REPORT_STATUSES for rec in records
    )
    return {
        "generated": generated,
        "loaded": generated - len(invalid_ids),
        "invalid_ids": invalid_ids,
        "by_status": by_status,
        "solved": by_status.get("solved", 0),
        "failed": by_status.get("failed", 0),
        "skipped": by_status.get("skipped", 0),
        "error": by_status.get("error", 0),
        "invalid": by_status.get("invalid", 0),
        "checked_score": _checked_score(records),
        "complete": complete,
    }


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


def _resolve_out_dir(value: str | None) -> Path:
    forbidden = {path.resolve() for path in _HISTORICAL_REPORTS}
    if value:
        out_dir = Path(value)
        if not out_dir.is_absolute():
            out_dir = Path.cwd() / out_dir
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        out_dir = ROOT / "eval" / "results" / f"fake_100_{stamp}"
    out_dir = out_dir.resolve()
    if out_dir in forbidden or out_dir.is_file():
        raise SystemExit(f"refusing to overwrite historical report path: {out_dir}")
    for name in ("summary.json", "manifest.json", "tasks.jsonl"):
        target = (out_dir / name).resolve()
        if target in forbidden:
            raise SystemExit(f"refusing to overwrite historical report path: {target}")
    return out_dir


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=100)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--seed", type=int, default=SEED_DEFAULT)
    parser.add_argument("--budget", type=float, default=100_000.0)
    parser.add_argument("--time-limit", type=float, default=120.0)
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Directory for manifest.json, tasks.jsonl, and summary.json. "
        "Default: a new timestamped directory under eval/results. "
        "Never writes eval/results/fake_100.json.",
    )
    args = parser.parse_args(argv)
    out_dir = _resolve_out_dir(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="fake100-") as tmp:
        root = Path(tmp) / "tasks"
        root.mkdir()
        manifest = generate_tasks(root, args.tasks, args.seed)
        if len(manifest) != args.tasks:
            errors.append(
                f"generated {len(manifest)} != tasks_requested {args.tasks}"
            )
        manifest_payload = {
            "synthetic": True,
            "seed": args.seed,
            "tasks": manifest,
        }
        (root / "manifest.json").write_text(
            json.dumps(manifest_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        loaded_tasks = load_tasks(root)
        loaded_ids = {task.task_id for task in loaded_tasks}

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

        empty_solved = 0
        solved_ids = {
            row["task_id"]
            for row in first.get("results", [])
            if row.get("status") == "solved"
        }
        for child in root.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue
            text = (child / "answer.txt").read_text(encoding="utf-8") if (child / "answer.txt").is_file() else ""
            if not text.strip() and child.name in solved_ids:
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

        records, problems = reconcile_records(
            manifest,
            loaded_ids,
            list(first.get("results") or []),
        )
        counts = summarize_records(records)
        if counts["loaded"] != len(loaded_ids):
            problems.append(
                f"loaded_mismatch:records={counts['loaded']} load_tasks={len(loaded_ids)}"
            )
        known_total = (
            counts["solved"]
            + counts["failed"]
            + counts["skipped"]
            + counts["error"]
            + counts["invalid"]
        )
        if counts["complete"] and known_total != len(manifest):
            problems.append(
                f"invariant_broken: known={known_total} generated={len(manifest)}"
            )

        report = {
            "synthetic": True,
            "seed": args.seed,
            "tasks_requested": args.tasks,
            "generated": len(manifest),
            "loaded": len(loaded_ids),
            "invalid_ids": counts["invalid_ids"],
            "by_status": counts["by_status"],
            "solved": counts["solved"],
            "failed": counts["failed"],
            "skipped": counts["skipped"],
            "error": counts["error"],
            "invalid": counts["invalid"],
            "score": first.get("score"),
            "checked_score": counts["checked_score"],
            "tokens_spent": first.get("tokens_spent"),
            "fake_llm_calls": first.get("fake_llm_calls"),
            "budget": budget,
            "workers": args.workers,
            "complete": counts["complete"],
            "problems": problems,
            "errors": errors,
            "tree_hash": _dir_hash(root),
        }
        _write_json(out_dir / "manifest.json", manifest_payload)
        _write_jsonl(out_dir / "tasks.jsonl", records)
        _write_json(out_dir / "summary.json", report)
        print(json.dumps(report, indent=2))
        failed = bool(errors) or bool(problems) or not counts["complete"]
        if failed:
            fail_bits = list(errors) + list(problems)
            if not counts["complete"] and not any(
                item.startswith("missing_terminal:") for item in problems
            ):
                fail_bits.append("incomplete")
            print("FAILED:", "; ".join(fail_bits), file=sys.stderr)
            return 1
        print("OK fake_100")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
