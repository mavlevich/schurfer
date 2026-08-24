"""Validate repository AI instruction entrypoints and repo-local skills."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POINTER_RELATIVE_PATHS = (
    "AGENTS.md",
    "CLAUDE.md",
    ".github/copilot-instructions.md",
)
ALIAS_RELATIVE_PATHS = (
    "GEMINI.md",
    ".cursorrules",
    ".windsurfrules",
)
EXPECTED_SKILLS = frozenset(
    {
        "schurfer-pr-review",
        "schurfer-execution-safety",
        "schurfer-research-integrity",
        "schurfer-production-deploy",
    }
)
FRONTMATTER_PATTERN = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)

# A pointer file must clearly say to read and follow AI_RULES.md, not merely
# mention its name somewhere -- see the colleague-review finding this fixes:
# a bare `"AI_RULES.md" in text` substring check would pass a file that says
# "ignore AI_RULES.md, do X instead" just as happily as a real pointer,
# because the literal filename still appears in the sentence.
POINTER_REQUIRED_PATTERN = re.compile(r"(?i)read\s+and\s+follow\s+`?/?AI_RULES\.md`?")
# Defense in depth on top of the required-phrase check above: even a file
# that does contain the exact "read and follow AI_RULES.md" phrase must not
# ALSO carry language nearby that could tell a reader (human or AI) to
# disregard, override, or deprioritize it elsewhere in the same file.
POINTER_FORBIDDEN_PATTERN = re.compile(
    r"(?i)\b(ignore|disregard|override|supersede[sd]?|do not follow|don't follow|skip)\b"
)
# These are meant to be thin pointers (see AI_RULES.md's own "Keep tool-
# specific entrypoints thin" instruction) -- a file this large is itself
# suspicious: real content has been inlined here instead of in the single
# source of truth, where it can drift unreviewed.
POINTER_MAX_BYTES = 600


def _fail(errors: list[str], message: str) -> None:
    errors.append(message)


def _frontmatter_value(body: str, key: str) -> str | None:
    prefix = f"{key}:"
    for line in body.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip().strip('"')
    return None


def validate(root: Path = ROOT) -> tuple[str, ...]:
    errors: list[str] = []
    canonical_rules = root / "AI_RULES.md"
    if not canonical_rules.is_file() or not canonical_rules.read_text().strip():
        _fail(errors, "AI_RULES.md is missing or empty")

    for relative in POINTER_RELATIVE_PATHS:
        pointer = root / relative
        if not pointer.is_file():
            _fail(errors, f"{relative} is missing")
            continue
        text = pointer.read_text()
        if len(text.encode()) > POINTER_MAX_BYTES:
            _fail(
                errors,
                f"{relative} is too long to be a thin pointer "
                f"(> {POINTER_MAX_BYTES} bytes) -- inline instructions belong in "
                "AI_RULES.md, not here",
            )
        if not POINTER_REQUIRED_PATTERN.search(text):
            _fail(
                errors,
                f"{relative} does not clearly say to read and follow AI_RULES.md",
            )
        forbidden = POINTER_FORBIDDEN_PATTERN.search(text)
        if forbidden:
            _fail(
                errors,
                f"{relative} contains {forbidden.group(0)!r}, which could contradict "
                "or override AI_RULES.md",
            )

    canonical_target = canonical_rules.resolve()
    for relative in ALIAS_RELATIVE_PATHS:
        alias = root / relative
        if not alias.is_symlink():
            _fail(errors, f"{relative} must be a symlink to AI_RULES.md")
            continue
        if alias.resolve() != canonical_target:
            _fail(errors, f"{relative} points somewhere other than AI_RULES.md")

    skills_root = root / ".agents" / "skills"
    if not skills_root.is_dir():
        _fail(errors, ".agents/skills is missing")
        return tuple(errors)

    discovered = {path.name for path in skills_root.iterdir() if path.is_dir()}
    missing = EXPECTED_SKILLS - discovered
    if missing:
        _fail(errors, f"missing expected skills: {', '.join(sorted(missing))}")
    # The reverse of the check above: EXPECTED_SKILLS is meant to be a
    # closed set. Without this, a new skill directory with a plausible-
    # looking SKILL.md could be added and would pass every check below
    # (it validates internal consistency, not membership) without ever
    # being flagged as an unreviewed addition to what this repo's AI
    # instructions actually claim to offer.
    unexpected = discovered - EXPECTED_SKILLS
    if unexpected:
        _fail(
            errors,
            f"unexpected skill directories not in EXPECTED_SKILLS: "
            f"{', '.join(sorted(unexpected))} -- add to EXPECTED_SKILLS "
            "deliberately if this is intentional",
        )

    seen_names: set[str] = set()
    for skill_dir in sorted(path for path in skills_root.iterdir() if path.is_dir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            _fail(errors, f"{skill_dir.relative_to(root)} has no SKILL.md")
            continue
        content = skill_file.read_text()
        match = FRONTMATTER_PATTERN.match(content)
        if match is None:
            _fail(errors, f"{skill_file.relative_to(root)} has invalid frontmatter")
            continue
        name = _frontmatter_value(match.group("body"), "name")
        description = _frontmatter_value(match.group("body"), "description")
        if name != skill_dir.name:
            _fail(errors, f"{skill_file.relative_to(root)} name does not match its directory")
        if name in seen_names:
            _fail(errors, f"duplicate skill name: {name}")
        if name:
            seen_names.add(name)
        if not description or "TODO" in description:
            _fail(errors, f"{skill_file.relative_to(root)} has no usable description")
        if "TODO" in content:
            _fail(errors, f"{skill_file.relative_to(root)} contains an unfinished TODO")

        metadata_file = skill_dir / "agents" / "openai.yaml"
        if not metadata_file.is_file():
            _fail(errors, f"{metadata_file.relative_to(root)} is missing")
        elif name and f"${name}" not in metadata_file.read_text():
            _fail(errors, f"{metadata_file.relative_to(root)} default prompt does not name ${name}")

    return tuple(errors)


def main() -> int:
    errors = validate(ROOT)
    if errors:
        for error in errors:
            sys.stderr.write(f"ERROR: {error}\n")
        return 1
    sys.stdout.write("AI rules and repo-local skills are valid\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
