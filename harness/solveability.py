"""Conservative solveability classification. No network, no LLM."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .task import Task
from .triage import _inspect_data_analysis, _referenced_files, _requires_network, _test_files

Verdict = Literal["solvable", "uncertain", "impossible"]

__all__ = ["SolveabilityResult", "check_solveability"]


@dataclass(frozen=True, slots=True)
class SolveabilityResult:
    task_id: str
    verdict: Verdict
    reason_code: str
    explanation: str
    evidence: tuple[str, ...]

    @property
    def impossible(self) -> bool:
        return self.verdict == "impossible"

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "verdict": self.verdict,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
            "evidence": list(self.evidence),
        }


def check_solveability(task: Task) -> SolveabilityResult:
    """Classify a task. Unknown type and weak priors are uncertain, not impossible."""

    instruction = task.instruction.strip()
    workdir = task.workdir

    if not instruction:
        return SolveabilityResult(
            task.task_id,
            "impossible",
            "empty_instruction",
            "instruction is empty",
            ("instruction_length=0",),
        )

    if _requires_network(task.instruction):
        return SolveabilityResult(
            task.task_id,
            "impossible",
            "network_required",
            "instruction requires internet or URL access outside the LLM proxy",
            ("network_requirement",),
        )

    missing = [ref for ref in _referenced_files(task.instruction) if not (workdir / ref).exists()]
    if missing:
        return SolveabilityResult(
            task.task_id,
            "impossible",
            "missing_input",
            "referenced input file(s) are absent from the task directory",
            tuple(f"missing:{name}" for name in sorted(missing)),
        )

    lowered = instruction.lower()
    if "kb/" in instruction or "knowledge base" in lowered:
        kb = workdir / "kb"
        if not kb.is_dir():
            return SolveabilityResult(
                task.task_id,
                "impossible",
                "missing_kb",
                "knowledge base directory kb/ is required and missing",
                ("kb_missing",),
            )
        if not any(kb.glob("*.md")):
            return SolveabilityResult(
                task.task_id,
                "impossible",
                "empty_kb",
                "knowledge base kb/ contains no markdown documents",
                ("kb_empty",),
            )

    if task.task_type == "coding":
        if not _test_files(workdir):
            return SolveabilityResult(
                task.task_id,
                "impossible",
                "coding_no_tests",
                "coding task has no test_*.py / *_test.py files",
                ("no_tests",),
            )
        sources = [
            path
            for path in workdir.glob("*.py")
            if not path.name.startswith("test_") and not path.name.endswith("_test.py")
        ]
        if not sources:
            return SolveabilityResult(
                task.task_id,
                "impossible",
                "coding_no_sources",
                "coding task has no source .py files to repair",
                ("no_sources",),
            )

    if task.task_type == "data_analysis":
        findings = _inspect_data_analysis(instruction, workdir)
        hard = [reason for weight, reason in findings if weight >= 0.75]
        if hard:
            return SolveabilityResult(
                task.task_id,
                "impossible",
                "data_input",
                hard[0],
                tuple(hard),
            )

    if task.task_type == "search":
        kb = workdir / "kb"
        has_kb = kb.is_dir() and any(kb.glob("*.md"))
        has_flat_md = any(workdir.rglob("*.md"))
        if not has_kb and not has_flat_md:
            return SolveabilityResult(
                task.task_id,
                "impossible",
                "search_no_docs",
                "search task has no knowledge base documents",
                ("no_markdown",),
            )

    if task.task_type in {"math", "search", "data_analysis", "coding"}:
        return SolveabilityResult(
            task.task_id,
            "solvable",
            "supported_path",
            "a supported solver path exists and required inputs are present",
            (f"task_type={task.task_type}",),
        )

    return SolveabilityResult(
        task.task_id,
        "uncertain",
        "unknown_type",
        "unknown task type is not proof of impossibility; generic solver remains a candidate",
        (f"task_type={task.task_type}",),
    )
