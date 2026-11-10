"""Deterministic knowledge-base retrieval.

The official search examples use a ``kb/*.md`` directory of documents with
``Key: value`` fields.  The solver finds the entity named in the question,
reads the requested field, and follows relation chains such as
``person -> Mentor -> person -> Employer -> company -> Headquarters``.

Relations are parsed from the natural-language instruction in reading order and
applied outwards from the entity, so repeated relations (``mentor of the mentor
of X``) and verb forms (``employs``, ``works for``, ``employed by``) are kept.
Optional filters such as ``founded in 1978`` constrain the final company.
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

_QUESTION_SUFFIX_RE = re.compile(
    r"\b(?:use the file tools|strict mode|work only inside|when you are done)\b",
    re.IGNORECASE,
)

_ATTRIBUTE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("headquarters", re.compile(r"\bheadquarters?\b|\bhq\b", re.IGNORECASE)),
    (
        "born",
        re.compile(r"\bborn\b|\bbirth(?:\s+year)?\b|\byear of birth\b", re.IGNORECASE),
    ),
    (
        "founded",
        re.compile(r"\bfounded\b|\bfounding(?:\s+year)?\b|\byear founded\b", re.IGNORECASE),
    ),
    ("mentor", re.compile(r"\bmentors?\b", re.IGNORECASE)),
    (
        "employer",
        re.compile(
            r"\bemployers?\b|\bworks?\s+for\b|\bemployed\s+by\b|\bemploy(?:s|ed)?\b",
            re.IGNORECASE,
        ),
    ),
    ("ceo", re.compile(r"\bceo\b|\bchief executive officer\b", re.IGNORECASE)),
    ("population", re.compile(r"\bpopulation\b", re.IGNORECASE)),
    ("capital", re.compile(r"\bcapital\b", re.IGNORECASE)),
    ("country", re.compile(r"\bcountry\b", re.IGNORECASE)),
    ("city", re.compile(r"\b(?:city|town)\b", re.IGNORECASE)),
)

_COMPANY_REQUEST_RE = re.compile(
    r"\b(?:which|what)\s+(?:company|firm|corporation|business|organization)\b"
    r"|\bname\s+of\s+the\s+(?:company|firm)\b",
    re.IGNORECASE,
)

_FOUNDED_FILTER_RE = re.compile(
    r"\bfounded\s+in\s+(?:the\s+year\s+)?(?P<year>1[5-9]\d{2}|20\d{2})\b",
    re.IGNORECASE,
)

# Relations written as "<relation> of <object>".  The match must end directly
# before the object, otherwise it belongs to a different part of the sentence.
_LEFT_RELATION_RE = re.compile(
    r"(?:"
    r"(?P<mentor>\bmentors?)\s+of"
    r"|(?P<employer>\bemployers?)\s+of"
    r"|(?P<employed_by>\bemployed\s+by)"
    r"|(?P<works_for>\bworks?\s+for)"
    r"|(?P<employs>\bemploy(?:s|ed)?)"
    r")\s+(?:the\s+)?$",
    re.IGNORECASE,
)

# Possessive relation directly attached to the entity: "X's mentor".
_RIGHT_POSSESSIVE_RE = re.compile(
    r"^\s*(?:'s|’s)\s*(?P<relation>mentors?|employers?)\b", re.IGNORECASE
)

# Verb relation after the noun phrase: "X works for ...", "X is employed by ...".
_RIGHT_VERB_RE = re.compile(
    r"^\s+(?:"
    r"(?P<works_for>\bworks?\s+for)\b"
    r"|(?:is\s+)?(?P<employed_by>\bemployed\s+by)\b"
    r"|(?P<employs>\bemploy(?:s|ed)?)\b"
    r")",
    re.IGNORECASE,
)

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

_NONE_VALUES = frozenset({"", "none", "n/a", "na", "unknown", "-", "null"})


@dataclass(frozen=True, slots=True)
class _Doc:
    path: Path
    stem: str
    title: str
    fields: dict[str, str]
    text: str


@dataclass(frozen=True, slots=True)
class _Question:
    text: str
    attribute: str | None
    wants_company: bool
    attribute_span: tuple[int, int] | None
    filters: tuple[tuple[str, str], ...]
    relations: tuple[str, ...]


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


def _is_missing(value: str | None) -> bool:
    return value is None or value.strip().lower() in _NONE_VALUES


def _overlaps(
    span: tuple[int, int], other: tuple[int, int] | None
) -> bool:
    if other is None:
        return False
    return span[0] < other[1] and other[0] < span[1]


def _question_text(instruction: str) -> str:
    """Drop the boilerplate that follows the actual question."""

    match = _QUESTION_SUFFIX_RE.search(instruction)
    return instruction[: match.start()] if match else instruction


def _requested_attribute(text: str) -> tuple[str | None, tuple[int, int] | None, bool]:
    if _COMPANY_REQUEST_RE.search(text):
        return "name", None, True

    # Plain field attributes are unambiguous; "headquarters" deliberately
    # outranks the looser "city" synonym.
    field_order = (
        "headquarters",
        "born",
        "founded",
        "ceo",
        "population",
        "capital",
        "country",
        "city",
    )
    patterns = dict(_ATTRIBUTE_PATTERNS)
    filter_spans = [match.span() for match in _FOUNDED_FILTER_RE.finditer(text)]
    for attribute in field_order:
        matches = patterns[attribute].finditer(text)
        for match in matches:
            # "founded in 1978" is a selection filter, not the requested field.
            if attribute == "founded" and any(
                _overlaps(match.span(), span) for span in filter_spans
            ):
                continue
            return attribute, match.span(), False

    # "employer" and "mentor" can also appear as relations, so the requested
    # attribute is the outermost (leftmost) one: "the employer of the mentor
    # of X" asks for an employer.
    relational = [
        (match.start(), attribute, match.span())
        for attribute in ("mentor", "employer")
        if (match := patterns[attribute].search(text)) is not None
    ]
    if relational:
        _, attribute, span = min(relational)
        return attribute, span, False
    return None, None, False


def _parse_filters(text: str) -> tuple[tuple[str, str], ...]:
    filters: list[tuple[str, str]] = []
    for match in _FOUNDED_FILTER_RE.finditer(text):
        filters.append(("founded", match.group("year")))
    return tuple(filters)


def _relation_chain(
    text: str,
    anchor: tuple[int, int],
    attribute_span: tuple[int, int] | None,
) -> tuple[str, ...]:
    """Return relations in application order (innermost first).

    ``A of B of Entity`` is parsed as ``B`` then ``A``.  Possessive relations
    (``Entity's A``) attach directly to the entity and verb relations
    (``Entity works for ...``) apply to the whole preceding noun phrase.
    """

    start, end = anchor

    possessive: list[str] = []
    cursor = end
    while True:
        match = _RIGHT_POSSESSIVE_RE.match(text[cursor:])
        if match is None:
            break
        span = (cursor + match.start(), cursor + match.end())
        if _overlaps(span, attribute_span):
            break
        relation = match.group("relation").lower()
        possessive.append("mentor" if relation.startswith("mentor") else "employer")
        cursor += match.end()

    outward: list[str] = []
    cursor = start
    while True:
        prefix = text[:cursor]
        match = _LEFT_RELATION_RE.search(prefix)
        if match is None:
            break
        span = match.span()
        if _overlaps(span, attribute_span):
            break
        outward.append("mentor" if match.group("mentor") else "employer")
        cursor = span[0]

    trailing: list[str] = []
    match = _RIGHT_VERB_RE.match(text[cursor:])
    if match is not None:
        span = (cursor + match.start(), cursor + match.end())
        if not _overlaps(span, attribute_span):
            trailing.append("employer")

    return tuple(possessive + outward + trailing)


def _locate_name(text: str, name: str) -> tuple[int, int] | None:
    match = re.search(
        rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text, re.IGNORECASE
    )
    return match.span() if match is not None else None


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


def _doc_kind(doc: _Doc) -> str:
    if doc.stem.startswith("company_"):
        return "company"
    if doc.stem.startswith("person_"):
        return "person"
    if "founded" in doc.fields or "headquarters" in doc.fields:
        return "company"
    return "person"


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
    if attribute is not None and attribute != "name":
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


def _follow_relation(
    doc: _Doc,
    relation: str,
    docs: list[_Doc],
    chain: list[str],
) -> tuple[_Doc | None, str | None]:
    value = _get_field(doc, relation)
    if _is_missing(value):
        return None, f"field {relation!r} missing in {doc.path.name}"
    assert value is not None  # narrowed by _is_missing
    chain.append(f"{relation}={value!r}")
    next_kind = "company" if relation == "employer" else "person"
    next_doc = _select_doc(docs, value, next_kind) or _select_doc(docs, value, None)
    if next_doc is None:
        return None, f"related document not found for {value!r}"
    return next_doc, None


def _apply_filters(
    doc: _Doc, filters: tuple[tuple[str, str], ...]
) -> tuple[bool, str | None]:
    for attribute, expected in filters:
        value = _get_field(doc, attribute)
        if _is_missing(value):
            return False, f"field {attribute!r} missing in {doc.path.name}"
        assert value is not None
        actual = _extract_year(value)
        if actual != expected:
            return (
                False,
                f"{attribute} year {actual!r} in {doc.path.name} "
                f"does not match requested {expected!r}",
            )
    return True, None


def _resolve(
    doc: _Doc,
    question: _Question,
    docs: list[_Doc],
) -> tuple[str | None, list[str], str]:
    chain: list[str] = []
    for relation in question.relations:
        next_doc, error = _follow_relation(doc, relation, docs, chain)
        if error is not None:
            return None, chain, error
        assert next_doc is not None
        doc = next_doc

    if question.wants_company and _doc_kind(doc) != "company":
        # The chain answered "who", but the question asks for the company.
        next_doc, error = _follow_relation(doc, "employer", docs, chain)
        if error is not None:
            return None, chain, error
        assert next_doc is not None
        doc = next_doc

    if question.filters:
        ok, error = _apply_filters(doc, question.filters)
        if not ok:
            return None, chain, error or "filter mismatch"

    if question.wants_company:
        return doc.title.strip(), chain, f"{doc.path.name}: name"

    attribute = question.attribute
    if attribute is None:
        return None, chain, "no requested attribute recognized"

    value = _get_field(doc, attribute)
    if value is None and attribute == "city":
        value = _get_field(doc, "headquarters")
    if value is None:
        return None, chain, f"field {attribute!r} missing in {doc.path.name}"

    if attribute in {"born", "founded"} and (
        "year" in question.text.lower() or "when" in question.text.lower()
    ):
        value = _extract_year(value)

    return value.strip(), chain, f"{doc.path.name}: {attribute}"


def solve(task: Task, ctx: SolverContext) -> SolveResult:  # noqa: ARG001 - uniform signature
    workdir = task.workdir
    docs = _load_docs(workdir)
    if not docs:
        return SolveResult(None, 0.0, "search", "no markdown knowledge-base documents found")

    instruction = task.instruction
    text = _question_text(instruction)
    attribute, attribute_span, wants_company = _requested_attribute(text)
    filters = _parse_filters(text)

    preferred_kind: str | None = None
    if wants_company or attribute in {"born", "mentor", "employer"}:
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
    anchor: tuple[int, int] | None = None
    for name in names:
        for kind in (preferred_kind, "person", "company", None):
            if kind is None and preferred_kind is not None:
                continue
            candidate = _select_doc(docs, name, kind)
            if candidate is not None:
                doc = candidate
                anchor = _locate_name(text, name)
                break
        if doc is not None:
            break

    if doc is None:
        fallback = _keyword_fallback(instruction, docs, attribute)
        if fallback is not None:
            return SolveResult(fallback, 0.0, "search", "keyword fallback after entity miss")
        return SolveResult(None, 0.0, "search", "entity not found in knowledge base")

    relations = (
        _relation_chain(text, anchor, attribute_span) if anchor is not None else ()
    )
    question = _Question(
        text=text,
        attribute=attribute,
        wants_company=wants_company,
        attribute_span=attribute_span,
        filters=filters,
        relations=relations,
    )

    answer, chain, note = _resolve(doc, question, docs)
    if answer is None:
        return SolveResult(None, 0.0, "search", note)
    if chain:
        note += " via " + ", ".join(chain)
    return SolveResult(answer, 0.0, "search", note)
