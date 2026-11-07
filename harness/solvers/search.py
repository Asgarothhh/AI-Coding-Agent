"""Deterministic knowledge-base retrieval.

The official search examples use a ``kb/*.md`` directory of documents with
``Key: value`` fields.  The solver finds the entity named in the question,
reads the requested field, and follows simple relation chains such as
``person -> Employer -> company -> Headquarters``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .base import SolveResult, SolverContext
from ..task import Task

__all__ = ["solve"]

_KEY_VALUE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 _-]{0,40}):\s*(.+?)\s*$", re.MULTILINE)
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")

_ATTRIBUTE_ALIASES: dict[str, tuple[str, ...]] = {
    "born": ("born", "birth", "birth year", "year of birth", "date of birth"),
    "employer": ("employer", "company", "works at", "works for", "employed by"),
    "headquarters": ("headquarters", "headquarter", "hq", "location", "city"),
    "founded": ("founded", "founding year", "year founded"),
    "city": ("city", "headquarters", "location"),
    "country": ("country",),
    "mentor": ("mentor",),
    "ceo": ("ceo", "chief executive officer"),
    "founder": ("founder",),
    "population": ("population",),
    "capital": ("capital",),
}

_STOPWORDS = frozenset(
    {
        "the", "a", "an", "in", "which", "what", "who", "where", "when", "was", "is",
        "were", "are", "of", "for", "to", "and", "or", "does", "did", "do", "has",
        "have", "had", "with", "by", "on", "at", "from", "that", "this", "it", "its",
        "directory", "contains", "small", "knowledge", "base", "markdown", "files",
        "use", "file", "tools", "bash", "find", "work", "only", "inside", "current",
        "when", "you", "are", "done", "write", "final", "answer", "example", "with",
    }
)


@dataclass(frozen=True, slots=True)
class _Doc:
    path: Path
    stem: str
    title: str
    fields: dict[str, str]
    text: str


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _load_docs(workdir: Path) -> list[_Doc]:
    kb = workdir / "kb"
    files = sorted(kb.rglob("*.md")) if kb.is_dir() else sorted(workdir.rglob("*.md"))
    docs: list[_Doc] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        title_match = _TITLE_RE.search(text)
        title = title_match.group(1).strip() if title_match else path.stem.replace("_", " ").title()
        fields: dict[str, str] = {}
        for match in _KEY_VALUE_RE.finditer(text):
            fields[_norm_key(match.group(1))] = match.group(2).strip()
        docs.append(
            _Doc(
                path=path,
                stem=path.stem.lower(),
                title=title,
                fields=fields,
                text=text,
            )
        )
    return docs


def _get_field(doc: _Doc, attribute: str) -> str | None:
    aliases = _ATTRIBUTE_ALIASES.get(attribute, (attribute,))
    for alias in aliases:
        value = doc.fields.get(_norm_key(alias))
        if value:
            return value
    # Last resort: normalized substring match on field names.
    wanted = _norm_key(attribute)
    for key, value in doc.fields.items():
        if wanted and (wanted in key or key in wanted):
            return value
    return None


def _determine_attribute(instruction: str) -> str | None:
    text = instruction.lower()
    if "headquarter" in text or re.search(r"\bhq\b", text):
        return "headquarters"
    if "born" in text or "birth" in text:
        return "born"
    if "founded" in text or "founding" in text:
        return "founded"
    if "employer" in text or "works for" in text or "work for" in text or "employed by" in text:
        return "employer"
    if "mentor" in text:
        return "mentor"
    if "ceo" in text or "chief executive" in text:
        return "ceo"
    if "population" in text:
        return "population"
    if "capital" in text:
        return "capital"
    if "city" in text:
        return "city"
    if "country" in text:
        return "country"
    return None


def _candidate_names(instruction: str, docs: list[_Doc]) -> list[str]:
    lowered = instruction.lower()
    names: list[str] = []
    for doc in docs:
        title = doc.title.strip()
        if title and title.lower() in lowered and title not in names:
            names.append(title)
    for match in _NAME_RE.finditer(instruction):
        name = match.group(1).strip()
        if name and name not in names and name.lower() not in _STOPWORDS:
            names.append(name)
    return names


def _select_doc(docs: list[_Doc], name: str, kind: str | None = None) -> _Doc | None:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    best: tuple[int, _Doc] | None = None
    for doc in docs:
        if kind is not None and not doc.stem.startswith(kind + "_"):
            continue
        score = 0
        if doc.stem in {slug, f"{kind}_{slug}" if kind else slug}:
            score = 100
        elif doc.title.lower() == name.lower():
            score = 90
        elif name.lower() in doc.title.lower():
            score = 70
        elif slug and slug in doc.stem:
            score = 50
        if score and (best is None or score > best[0]):
            best = (score, doc)
    return best[1] if best else None


def _extract_year(value: str) -> str:
    match = _YEAR_RE.search(value)
    return match.group(1) if match else value


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [word for word in words if len(word) > 2 and word not in _STOPWORDS]


def _keyword_fallback(instruction: str, docs: list[_Doc], attribute: str | None) -> str | None:
    """Retrieve a unique ``Key: value`` answer by lexical overlap."""

    tokens = _tokens(instruction)
    if not tokens:
        return None

    ranked: list[tuple[int, _Doc]] = []
    for doc in docs:
        lowered = doc.text.lower()
        score = sum(1 for token in tokens if token in lowered)
        if score:
            ranked.append((score, doc))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], item[1].stem))
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None

    best_score, best_doc = ranked[0]
    if attribute is not None:
        value = _get_field(best_doc, attribute)
        if value:
            return _extract_year(value) if attribute == "born" and "year" in instruction.lower() else value

    # Find field lines whose key or value overlaps the question.
    matches: dict[str, str] = {}
    for match in _KEY_VALUE_RE.finditer(best_doc.text):
        key = match.group(1).strip()
        value = match.group(2).strip()
        haystack = f"{key} {value}".lower()
        if any(token in haystack for token in tokens):
            matches[_norm_key(key)] = value
    if len(matches) == 1:
        return next(iter(matches.values()))
    _ = best_score
    return None


def solve(task: Task, ctx: SolverContext) -> SolveResult:  # noqa: ARG001 - uniform signature
    workdir = task.workdir
    docs = _load_docs(workdir)
    if not docs:
        return SolveResult(None, 0.0, "search", "no markdown knowledge-base documents found")

    instruction = task.instruction
    text = instruction.lower()
    attribute = _determine_attribute(instruction)

    relations: list[str] = []
    if "employer" in text and attribute != "employer":
        relations.append("employer")
    if "mentor" in text and attribute != "mentor":
        relations.append("mentor")

    preferred_kind: str | None = None
    if attribute in {"born", "employer", "mentor"} or relations:
        preferred_kind = "person"
    elif attribute in {"headquarters", "founded", "ceo", "population"}:
        preferred_kind = "company"

    names = _candidate_names(instruction, docs)
    if not names:
        fallback = _keyword_fallback(instruction, docs, attribute)
        if fallback is not None:
            return SolveResult(fallback, 0.0, "search", "keyword fallback")
        return SolveResult(None, 0.0, "search", "no entity name found in instruction")

    # Prioritise names that resolve to the expected document kind.
    names.sort(
        key=lambda name: (
            0 if preferred_kind and _select_doc(docs, name, preferred_kind) else 1,
            -len(name),
        )
    )

    doc: _Doc | None = None
    for name in names:
        for kind in (preferred_kind, "person", "company", None):
            if kind is None and preferred_kind is not None:
                continue
            candidate = _select_doc(docs, name, kind)
            if candidate is not None:
                doc = candidate
                break
        if doc is not None:
            break

    if doc is None:
        fallback = _keyword_fallback(instruction, docs, attribute)
        if fallback is not None:
            return SolveResult(fallback, 0.0, "search", "keyword fallback after entity miss")
        return SolveResult(None, 0.0, "search", "entity not found in knowledge base")

    chain: list[str] = []
    for relation in relations:
        value = _get_field(doc, relation)
        if value is None:
            return SolveResult(None, 0.0, "search", f"field {relation!r} missing in {doc.path.name}")
        chain.append(f"{relation}={value!r}")
        next_kind = "company" if relation == "employer" else None
        next_doc = _select_doc(docs, value, next_kind) or _select_doc(docs, value, None)
        if next_doc is None:
            return SolveResult(None, 0.0, "search", f"related document not found for {value!r}")
        doc = next_doc

    if attribute is None:
        fallback = _keyword_fallback(instruction, docs, attribute)
        if fallback is not None:
            return SolveResult(fallback, 0.0, "search", "keyword fallback for unknown attribute")
        return SolveResult(None, 0.0, "search", "no requested attribute recognized")

    value = _get_field(doc, attribute)
    if value is None and attribute == "city":
        value = _get_field(doc, "headquarters")
    if value is None:
        return SolveResult(None, 0.0, "search", f"field {attribute!r} missing in {doc.path.name}")

    if attribute in {"born", "founded"} and ("year" in text or "when" in text):
        value = _extract_year(value)

    note = f"{doc.path.name}: {attribute}"
    if chain:
        note += " via " + ", ".join(chain)
    return SolveResult(value.strip(), 0.0, "search", note)
