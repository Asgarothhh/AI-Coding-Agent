"""Cheap, deterministic feasibility triage.

No LLM is ever called here.  The goal is to spend zero tokens on tasks that
are structurally impossible with the provided workspace: missing input files,
an empty knowledge base, coding tasks without tests, requests that need the
internet, or explicitly contradictory instructions.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .task import Task

__all__ = ["DEFAULT_THRESHOLD", "TriageResult", "triage", "is_infeasible"]

DEFAULT_THRESHOLD = 0.6

_FILE_REF_RE = re.compile(
    r"(?<![\w./-])((?:\./)?(?:[\w.-]+/)*[\w.-]+\.(?:csv|tsv|json|jsonl|md|txt|py|yaml|yml))",
    re.IGNORECASE,
)
_IGNORED_FILES = frozenset({"answer.txt", "task.json", "solutions.json", "trace.jsonl"})
_INTERNET_RE = re.compile(
    r"("
    r"https?://"
    r"|\bweb[\s-]?search\b"
    r"|\bbrowse\s+the\s+web\b"
    r"|\binternet\b"
    r"|\bwget\b"
    r"|\bcurl\s"
    # A bare "download" is ordinary math/data vocabulary ("a download manager
    # processes 30 files"), so only a remote source makes it a network
    # requirement: "download ... from the internet/web/url".
    r"|\bdownload(?:s|ed|ing)?\b[^.\n]{0,120}?\bfrom\s+(?:the\s+)?(?:internet|web|url)\b"
    r")",
    re.IGNORECASE,
)
# Prohibitive/negated mentions of the network ("no internet", "work offline",
# "internet is not available", ...) must not look like a network requirement.
# Such spans are blanked out before ``_INTERNET_RE`` runs; genuine requirements
# elsewhere in the instruction (URLs, "download ... from the internet", "wget",
# ...) are unaffected.
_NEGATED_NETWORK_RE = re.compile(
    r"(?:"
    r"\b(?:no|without)\s+(?:internet|network|web)(?:\s+access)?"
    r"|\boffline\b"
    r"|\b(?:do\s+not|don['\u2019]t|does\s+not|doesn['\u2019]t|never)\s+"
    r"(?:use|access|browse)\s+(?:the\s+)?(?:internet|network|web)\b"
    r"|\b(?:the\s+)?(?:internet|network|web)\s+(?:is\s+)?(?:not\s+available|unavailable)\b"
    r")",
    re.IGNORECASE,
)
_CONTRADICTION_PHRASES = (
    "impossible",
    "cannot be determined",
    "can not be determined",
    "not enough information",
    "insufficient data",
    "insufficient information",
    "contradict",
    "no data available",
    "undefined",
    "undetermined",
)
_COLUMNS_RE = re.compile(r"columns?\s+([A-Za-z0-9_,\s]+?)(?:\s*\(|\s*\.|$)", re.IGNORECASE)
_ROWS_RE = re.compile(r"(\d+)\s+rows?\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class TriageResult:
    task_id: str
    score: float
    threshold: float
    reasons: tuple[str, ...]

    @property
    def infeasible(self) -> bool:
        return self.score >= self.threshold

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "score": round(self.score, 4),
            "threshold": self.threshold,
            "infeasible": self.infeasible,
            "reasons": list(self.reasons),
        }


def triage(task: Task, threshold: float = DEFAULT_THRESHOLD) -> TriageResult:
    """Score task feasibility in ``[0, 1]``.

    A score at or above ``threshold`` means "skip without calling an LLM".
    """

    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be within [0, 1]")

    instruction = task.instruction.strip()
    lowered = instruction.lower()
    workdir = task.workdir
    findings: list[tuple[float, str]] = []

    if len(instruction) < 12:
        findings.append((1.0, "instruction is empty or too short"))

    if _requires_network(instruction):
        findings.append((0.85, "instruction appears to require internet/URL access"))

    contradiction = next((p for p in _CONTRADICTION_PHRASES if p in lowered), None)
    if contradiction is not None:
        findings.append((0.95, f"instruction contains contradiction/insufficiency marker: {contradiction!r}"))

    # Referenced input files must exist in the workspace.
    referenced = _referenced_files(instruction)
    missing = [ref for ref in referenced if not (workdir / ref).exists()]
    if missing:
        weight = 0.75 if len(missing) == 1 else 0.9
        findings.append((weight, "referenced file(s) missing: " + ", ".join(sorted(missing))))

    # Knowledge-base tasks need a non-empty kb/ directory.
    if "kb/" in instruction or "knowledge base" in lowered:
        kb = workdir / "kb"
        if not kb.is_dir():
            findings.append((0.9, "knowledge base directory kb/ is missing"))
        elif not any(kb.glob("*.md")):
            findings.append((0.95, "knowledge base kb/ contains no markdown documents"))

    if task.task_type == "coding":
        if not _test_files(workdir):
            findings.append((0.95, "coding task has no test_*.py / *_test.py files"))
        if not any(p.suffix == ".py" and not p.name.startswith("test_") for p in workdir.glob("*.py")):
            findings.append((0.85, "coding task has no source .py files to repair"))

    if task.task_type == "data_analysis":
        findings.extend(_inspect_data_analysis(instruction, workdir))

    if task.task_type == "math" and not re.search(r"\d", instruction):
        findings.append((0.7, "math task contains no numeric data"))

    if task.task_type == "search":
        kb = workdir / "kb"
        has_kb = kb.is_dir() and any(kb.glob("*.md"))
        has_flat_md = any(workdir.rglob("*.md"))
        if not has_kb and not has_flat_md:
            findings.append((0.85, "search task has no knowledge base documents"))

    # Deduplicate equal reasons while keeping the highest weights first.
    findings.sort(key=lambda item: (-item[0], item[1]))
    seen: set[str] = set()
    reasons: list[str] = []
    for _, reason in findings:
        if reason not in seen:
            seen.add(reason)
            reasons.append(reason)

    if not findings:
        score = 0.0
    else:
        # Multiple independent signals are worse than one; cap at 1.0.
        weight = max(w for w, _ in findings) + 0.03 * (len(findings) - 1)
        score = round(min(1.0, weight), 4)

    return TriageResult(
        task_id=task.task_id,
        score=score,
        threshold=threshold,
        reasons=tuple(reasons),
    )


def is_infeasible(task: Task, threshold: float = DEFAULT_THRESHOLD) -> bool:
    return triage(task, threshold).infeasible


# -- helpers ------------------------------------------------------------------
def _requires_network(instruction: str) -> bool:
    """Return ``True`` when a non-negated network requirement is present."""

    positive_text = _NEGATED_NETWORK_RE.sub(" ", instruction)
    return _INTERNET_RE.search(positive_text) is not None


def _referenced_files(instruction: str) -> list[str]:
    refs: list[str] = []
    for match in _FILE_REF_RE.finditer(instruction):
        raw = match.group(1)
        while raw.startswith("./"):
            raw = raw[2:]
        if Path(raw).name.lower() in _IGNORED_FILES:
            continue
        if raw not in refs:
            refs.append(raw)
    return refs


def _test_files(workdir: Path) -> list[Path]:
    found: list[Path] = []
    for pattern in ("test_*.py", "*_test.py"):
        found.extend(sorted(workdir.glob(pattern)))
    return found


def _inspect_data_analysis(instruction: str, workdir: Path) -> list[tuple[float, str]]:
    findings: list[tuple[float, str]] = []
    csvs = sorted(workdir.glob("*.csv")) + sorted(workdir.glob("*/*.csv"))
    if not csvs:
        findings.append((0.9, "data_analysis task has no CSV file in the workspace"))
        return findings

    referenced = [ref for ref in _referenced_files(instruction) if ref.lower().endswith((".csv", ".tsv"))]
    target = workdir / referenced[0] if referenced and (workdir / referenced[0]).is_file() else csvs[0]
    try:
        with target.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            columns = [str(c) for c in (reader.fieldnames or []) if c]
            rows = sum(1 for _ in reader)
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        findings.append((0.9, f"CSV file {target.name} cannot be read: {exc}"))
        return findings

    if rows == 0:
        findings.append((0.9, f"CSV file {target.name} has no data rows"))
        return findings

    claimed_columns = _claimed_columns(instruction)
    if claimed_columns:
        actual_lower = {c.lower() for c in columns}
        missing_columns = [c for c in claimed_columns if c.lower() not in actual_lower]
        if missing_columns:
            findings.append(
                (0.75, f"CSV file {target.name} is missing claimed column(s): {', '.join(missing_columns)}")
            )

    row_match = _ROWS_RE.search(instruction)
    if row_match:
        claimed_rows = int(row_match.group(1))
        if claimed_rows != rows:
            findings.append((0.55, f"claimed {claimed_rows} rows but {target.name} has {rows}"))

    return findings


def _claimed_columns(instruction: str) -> list[str]:
    match = _COLUMNS_RE.search(instruction)
    if not match:
        return []
    raw = match.group(1)
    columns = [part.strip() for part in re.split(r"[,\s]+", raw) if part.strip()]
    # Avoid treating a stray sentence fragment as a column list.
    if not columns or any(len(part) > 40 for part in columns):
        return []
    return columns
