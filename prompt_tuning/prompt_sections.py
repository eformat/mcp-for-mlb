"""Split and reconstruct system_prompt.md around the mutable prediction framework."""

import os
import re

_START_MARKER = "### Game Prediction Framework"
_END_MARKER = "### Statistics Definitions"
_PLACEHOLDER = "{PREDICTION_FRAMEWORK}"

_PROMPT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "agents", "mlb-agent", "system_prompt.md"
)

_REQUIRED_HEADINGS = [
    "#### Step 1",
    "#### Step 2",
    "#### Step 3",
    "#### Step 4",
    "#### Anti-Patterns",
]


def load_and_split_prompt(prompt_path: str = "") -> tuple[str, str]:
    """Split system_prompt.md into (immutable_template, mutable_section).

    The immutable template contains {PREDICTION_FRAMEWORK} where the mutable
    section was extracted from.
    """
    path = prompt_path or _PROMPT_PATH
    with open(path) as f:
        text = f.read()

    start = text.find(_START_MARKER)
    end = text.find(_END_MARKER)
    if start == -1 or end == -1:
        raise ValueError(
            f"Could not find section markers in {path}. "
            f"Expected '{_START_MARKER}' and '{_END_MARKER}'"
        )

    mutable = text[start:end].rstrip() + "\n"
    template = text[:start] + _PLACEHOLDER + "\n" + text[end:]
    return template, mutable


def reconstruct_prompt(template: str, section: str) -> str:
    """Recombine immutable template with a mutable section."""
    return template.replace(_PLACEHOLDER, section.rstrip() + "\n")


def validate_section(section: str) -> tuple[bool, list[str]]:
    """Check that a mutable section has the required structure."""
    issues = []

    if not section.strip().startswith(_START_MARKER):
        issues.append(f"Must start with '{_START_MARKER}'")

    for heading in _REQUIRED_HEADINGS:
        if heading not in section:
            issues.append(f"Missing required heading: {heading}")

    if len(section) > 15_000:
        issues.append(f"Section too long: {len(section)} chars (max 20000)")

    if len(section) < 200:
        issues.append(f"Section too short: {len(section)} chars (min 200)")

    blocked = re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE)\b", re.IGNORECASE
    )
    literals_stripped = re.sub(r"'[^']*'", "''", section)
    code_stripped = re.sub(r"```[\s\S]*?```", "", literals_stripped)
    if blocked.search(code_stripped):
        issues.append("Contains blocked SQL keywords outside code blocks")

    return (len(issues) == 0, issues)
