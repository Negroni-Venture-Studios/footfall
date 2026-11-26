"""Every relative link and image in a Markdown file points at a file that exists.

A moved or renamed file leaves broken links on GitHub, where the documents are
read. Links to other sites, to anchors within a page, and to mail addresses are
not checked here.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Folders that are not the repository's documents: tools' caches, the plan workspace, and
# .claude, where an agent's git worktree of another commit can sit.
SKIPPED = {
    ".venv",
    "node_modules",
    ".git",
    ".superpowers",
    ".claude",
    ".mypy_cache",
    ".pytest_cache",
}
# The plans quote the links of files they describe, as examples, not as links.
PLANS = ROOT / "docs" / "plans"
DOCUMENTS = sorted(
    p
    for p in ROOT.glob("**/*.md")
    if not SKIPPED & set(p.relative_to(ROOT).parts) and PLANS not in p.parents
)
# [text](target) and ![alt](target), plus src and srcset in embedded HTML.
LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)|(?:src|srcset|href)=\"([^\"]+)\"")
FENCE = re.compile(r"^(```|~~~).*?^\1", re.DOTALL | re.MULTILINE)
EXTERNAL = ("http://", "https://", "mailto:", "#")


def targets(document: pathlib.Path) -> list[str]:
    text = FENCE.sub("", document.read_text())
    text = re.sub(r"`[^`\n]*`", "", text)
    found = [a or b for a, b in LINK.findall(text)]
    return [t for t in found if not t.startswith(EXTERNAL)]


def test_there_are_documents() -> None:
    assert ROOT / "README.md" in DOCUMENTS


def test_every_relative_link_and_image_exists() -> None:
    missing = []
    for document in DOCUMENTS:
        for target in targets(document):
            path = target.split("#", 1)[0]
            if path and not (document.parent / path).exists():
                missing.append(f"{document.relative_to(ROOT)}: {target}")
    assert not missing, "\n".join(missing)


def test_the_readme_links_the_community_files() -> None:
    linked = set(targets(ROOT / "README.md"))
    for name in ("CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md", "AGENTS.md"):
        assert name in linked, name


def test_every_path_in_codeowners_exists() -> None:
    """An owner for a path that has moved would review nothing."""
    lines = (ROOT / ".github" / "CODEOWNERS").read_text().splitlines()
    paths = [line.split()[0] for line in lines if line.strip() and not line.startswith("#")]
    assert "*" in paths
    missing = [path for path in paths if path != "*" and not (ROOT / path.strip("/")).exists()]
    assert not missing
