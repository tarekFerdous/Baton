"""Turn a grilling-style assistant reply into structured question blocks.

Pure local text parsing (regex) -- no extra Claude calls. Best-effort: any
question the heuristics can't cleanly split into options just falls back to
a free-text answer box, so nothing is ever lost, only sometimes rendered
less richly.
"""

import re

_HEADING_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _clean(text: str) -> str:
    return _BOLD_RE.sub(r"\1", text).strip()


def _detect_options(question: str) -> list[str] | None:
    if not question.endswith("?"):
        return None
    body = question[:-1]
    if " or " not in body:
        return None
    normalized = re.sub(r"\s+or\s+", ", ", body)
    parts = [p.strip(" '\"") for p in normalized.split(",") if p.strip()]
    if 2 <= len(parts) <= 4 and all(0 < len(p) <= 40 for p in parts):
        return parts
    return None


def parse_grilling_response(text: str) -> dict:
    """Return {"preamble": str, "sections": [{"heading": str|None, "questions": [...]}]}.

    Each question is {"id": str, "text": str, "options": list[str] | None}.
    """
    sections: list[dict] = []
    preamble_lines: list[str] = []
    current: dict | None = None
    counter = 0
    seen_structure = False

    def new_question(raw: str) -> dict:
        nonlocal counter
        counter += 1
        clean = _clean(raw)
        return {"id": f"q{counter}", "text": clean, "options": _detect_options(clean)}

    for line in text.splitlines():
        bullet_match = _BULLET_RE.match(line)
        heading_match = _HEADING_RE.match(line) if not bullet_match else None

        if bullet_match:
            seen_structure = True
            if current is None:
                current = {"heading": None, "questions": []}
                sections.append(current)
            current["questions"].append(new_question(bullet_match.group(1)))
            continue

        if heading_match:
            seen_structure = True
            heading_text = _clean(heading_match.group(1))
            if heading_text.endswith("?"):
                sections.append({"heading": None, "questions": [new_question(heading_text)]})
                current = None
            else:
                current = {"heading": heading_text, "questions": []}
                sections.append(current)
            continue

        stripped = line.strip()
        if stripped and not seen_structure:
            preamble_lines.append(stripped)

    return {
        "preamble": " ".join(preamble_lines).strip(),
        "sections": [s for s in sections if s["questions"]],
    }
