"""Contract: triage and planning over a large synthetic task set.

The planner must be cheap, deterministic, and independent of the ground-truth
``expected`` field.  It has to skip structurally impossible tasks, schedule
all deterministic task types before LLM-dependent ones, and order work by
``reward / estimated_cost``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.planner import DETERMINISTIC_TYPES, build_plan, estimate_cost
from harness.task import load_tasks
from harness.triage import triage

THRESHOLD = 0.6


def _write_task(
    root: Path,
    task_id: str,
    task_type: str,
    instruction: str,
    *,
    reward: float = 1.0,
    files: dict[str, str] | None = None,
    check: str = "exact_int",
    expected: str | None = None,
) -> Path:
    task_dir = root / task_id
    if task_dir.exists():
        raise AssertionError(f"duplicate synthetic task id {task_id}")
    task_dir.mkdir(parents=True)
    payload: dict[str, object] = {
        "task_id": task_id,
        "task_type": task_type,
        "instruction": instruction,
        "reward": reward,
        "check": check,
    }
    if expected is not None:
        payload["expected"] = expected
    (task_dir / "task.json").write_text(json.dumps(payload), encoding="utf-8")

    workspace = task_dir / "workspace"
    workspace.mkdir()
    for relative, content in (files or {}).items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return task_dir


def _unit_csv(rows: int = 3) -> str:
    body = "".join(f"{index},{index * 3}\n" for index in range(1, rows + 1))
    return "id,units\n" + body


def _search_doc() -> str:
    return "# Alice Smith\n\nBorn: 1983\nEmployer: Fjord Data\n"


def _coding_files() -> dict[str, str]:
    return {
        "mylib.py": "def add(a, b):\n    return a + b\n",
        "test_mylib.py": "import mylib\nassert mylib.add(1, 2) == 3\n",
    }


class SyntheticTaskSet:
    """Builds 20 infeasible + 30 deterministic + 50 coding tasks."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.infeasible: set[str] = set()
        self.deterministic: set[str] = set()
        self.llm: set[str] = set()
        self._build()

    def _build(self) -> None:
        # -- 20 structurally infeasible tasks ------------------------------
        for index in range(5):
            task_id = f"bad_missing_{index:02d}"
            _write_task(
                self.root,
                task_id,
                "data_analysis",
                (
                    f"The file missing_{index}.csv has columns id,units. "
                    "How many units are there?"
                ),
                files={"data.csv": _unit_csv()},
            )
            self.infeasible.add(task_id)

        for index in range(5):
            task_id = f"bad_kb_{index:02d}"
            _write_task(
                self.root,
                task_id,
                "search",
                (
                    "The directory kb/ contains a small knowledge base. "
                    "Who is Alice Smith?"
                ),
                files={"kb/readme.txt": "plain text, not markdown\n"},
                check="exact_str",
            )
            self.infeasible.add(task_id)

        for index in range(5):
            task_id = f"bad_coding_{index:02d}"
            _write_task(
                self.root,
                task_id,
                "coding",
                "Fix the bugs in mylib.py.",
                files={"mylib.py": "def add(a, b):\n    return a - b\n"},
                check="unittest",
            )
            self.infeasible.add(task_id)

        for index in range(5):
            task_id = f"bad_url_{index:02d}"
            _write_task(
                self.root,
                task_id,
                "search",
                (
                    "Download the latest report from "
                    "https://example.com/report.csv and summarize it."
                ),
                files={"kb/note.md": "# Note\n\nKey: value\n"},
                check="exact_str",
            )
            self.infeasible.add(task_id)

        # -- 30 feasible deterministic tasks -------------------------------
        for index in range(10):
            task_id = f"math_ok_{index:02d}"
            _write_task(
                self.root,
                task_id,
                "math",
                f"Compute {index + 1} + {index + 2}.",
                reward=float(index + 1),
            )
            self.deterministic.add(task_id)

        for index in range(10):
            task_id = f"data_ok_{index:02d}"
            _write_task(
                self.root,
                task_id,
                "data_analysis",
                (
                    "The file sales.csv has columns id,units. "
                    "What is the sum of units?"
                ),
                reward=float(index + 1),
                files={"sales.csv": _unit_csv()},
            )
            self.deterministic.add(task_id)

        for index in range(10):
            task_id = f"search_ok_{index:02d}"
            _write_task(
                self.root,
                task_id,
                "search",
                (
                    "The directory kb/ contains a small knowledge base. "
                    "In which year was Alice Smith born?"
                ),
                reward=float(index + 1),
                files={"kb/alice_smith.md": _search_doc()},
                check="exact_str",
            )
            self.deterministic.add(task_id)

        # -- 50 feasible coding / LLM tasks --------------------------------
        for index in range(50):
            task_id = f"coding_ok_{index:02d}"
            _write_task(
                self.root,
                task_id,
                "coding",
                "Fix mylib.py so that test_mylib.py passes.",
                reward=2.0,
                files=_coding_files(),
                check="unittest",
            )
            self.llm.add(task_id)

        self.feasible = self.deterministic | self.llm
        assert len(self.infeasible) == 20
        assert len(self.deterministic) == 30
        assert len(self.llm) == 50


class TriagePlannerContractTests(unittest.TestCase):
    def test_triage_flags_exactly_the_infeasible_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = SyntheticTaskSet(Path(tmp) / "tasks")
            tasks = load_tasks(fixture.root)
            self.assertEqual(len(tasks), 100)

            flagged = {
                task.task_id
                for task in tasks
                if triage(task, THRESHOLD).infeasible
            }
            self.assertEqual(flagged, fixture.infeasible)

            # Explicit spot checks for each infeasibility family.
            by_id = {task.task_id: task for task in tasks}
            for task_id in sorted(fixture.infeasible):
                with self.subTest(task=task_id):
                    result = triage(by_id[task_id], THRESHOLD)
                    self.assertTrue(result.infeasible)
                    self.assertGreaterEqual(result.score, THRESHOLD)
                    self.assertTrue(result.reasons)

    def test_plan_skips_infeasible_and_covers_all_feasible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = SyntheticTaskSet(Path(tmp) / "tasks")
            tasks = load_tasks(fixture.root)
            plan = build_plan(tasks, THRESHOLD)

            self.assertEqual(len(plan), 100)
            planned = [item for item in plan if item.status == "planned"]
            skipped = [item for item in plan if item.status == "skipped"]
            self.assertEqual(len(planned), 80)
            self.assertEqual(len(skipped), 20)
            self.assertEqual({item.task.task_id for item in planned}, fixture.feasible)
            self.assertEqual({item.task.task_id for item in skipped}, fixture.infeasible)

    def test_deterministic_tasks_come_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = SyntheticTaskSet(Path(tmp) / "tasks")
            plan = build_plan(load_tasks(fixture.root), THRESHOLD)

            prefix = plan[: len(fixture.deterministic)]
            self.assertTrue(all(item.status == "planned" for item in prefix))
            self.assertTrue(all(item.deterministic for item in prefix))
            self.assertEqual(
                {item.task.task_id for item in prefix}, fixture.deterministic
            )
            self.assertEqual(
                {item.task.task_type for item in prefix}, set(DETERMINISTIC_TYPES)
            )

            tail = plan[len(fixture.deterministic):]
            planned_coding = tail[: len(fixture.llm)]
            skipped = tail[len(fixture.llm):]
            # Planned coding tasks follow the deterministic block; skipped
            # tasks (of any type) are always last.
            self.assertTrue(all(item.status == "planned" for item in planned_coding))
            self.assertTrue(all(not item.deterministic for item in planned_coding))
            self.assertEqual(
                {item.task.task_id for item in planned_coding}, fixture.llm
            )
            self.assertTrue(all(item.status == "skipped" for item in skipped))
            self.assertEqual(
                {item.task.task_id for item in skipped}, fixture.infeasible
            )

    def test_priority_ordering_is_reward_over_estimated_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = SyntheticTaskSet(Path(tmp) / "tasks")
            plan = build_plan(load_tasks(fixture.root), THRESHOLD)

            for item in plan:
                with self.subTest(task=item.task.task_id):
                    expected = item.task.reward / max(item.estimated_cost, 1.0)
                    self.assertAlmostEqual(item.priority, expected, places=12)

            def sort_key(item):
                return (
                    item.status != "planned",
                    not item.deterministic,
                    -item.priority,
                    item.task.task_id,
                )

            self.assertEqual(
                [sort_key(item) for item in plan],
                sorted(sort_key(item) for item in plan),
            )

            # Plan slices must be individually non-increasing in priority.
            deterministic_prefix = plan[: len(fixture.deterministic)]
            coding_planned = plan[
                len(fixture.deterministic): len(fixture.deterministic) + len(fixture.llm)
            ]
            for group in (deterministic_prefix, coding_planned):
                priorities = [item.priority for item in group]
                self.assertEqual(priorities, sorted(priorities, reverse=True))

            # ``estimated_cost`` is the public estimate used by the priority.
            for item in plan:
                with self.subTest(task=item.task.task_id):
                    self.assertEqual(
                        item.estimated_cost, estimate_cost(item.task)
                    )

    def test_production_does_not_read_expected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            # Include every task type and every deterministic solver family.
            _write_task(
                root,
                "math_with_expected",
                "math",
                "Compute 10 + 32.",
                expected="definitely-not-42",
            )
            _write_task(
                root,
                "data_with_expected",
                "data_analysis",
                (
                    "The file sales.csv has columns id,units. "
                    "What is the sum of units?"
                ),
                files={"sales.csv": _unit_csv()},
                expected="999999",
            )
            _write_task(
                root,
                "search_with_expected",
                "search",
                (
                    "The directory kb/ contains a small knowledge base. "
                    "In which year was Alice Smith born?"
                ),
                files={"kb/alice_smith.md": _search_doc()},
                check="exact_str",
                expected="1600",
            )
            _write_task(
                root,
                "coding_with_expected",
                "coding",
                "Fix mylib.py so that test_mylib.py passes.",
                files=_coding_files(),
                check="unittest",
                expected="tests_pass",
            )

            with_expected = [item.as_dict() for item in build_plan(load_tasks(root))]
            self.assertTrue(with_expected)

            for task_json in root.glob("*/task.json"):
                payload = json.loads(task_json.read_text(encoding="utf-8"))
                self.assertIn("expected", payload)
                del payload["expected"]
                task_json.write_text(json.dumps(payload), encoding="utf-8")

            tasks_without_expected = load_tasks(root)
            without_expected = [
                item.as_dict() for item in build_plan(tasks_without_expected)
            ]

            self.assertEqual(without_expected, with_expected)
            for task in tasks_without_expected:
                with self.subTest(task=task.task_id):
                    self.assertFalse(hasattr(task, "expected"))


if __name__ == "__main__":
    unittest.main()
