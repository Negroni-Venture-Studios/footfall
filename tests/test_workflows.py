"""The CI workflows, read as text a step at a time. No YAML library is needed."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
SETUP_UV = "- uses: astral-sh/setup-uv@"
# The step before each setup-uv. It reads the version from the footfall image's Dockerfile.
# It fails if it finds none. An empty version would give setup-uv the latest uv.
READ_UV = """\
      - name: Read the uv the footfall image copies in
        id: uv
        run: |
          image=images/footfall/Dockerfile
          version=$(sed -nE 's#^FROM ghcr\\.io/astral-sh/uv:([0-9.]+)@sha256:.*#\\1#p' "$image")
          test -n "$version"
          echo "version=$version" >> "$GITHUB_OUTPUT"
"""


def test_ci_sets_up_the_uv_the_footfall_image_copies_in() -> None:
    """Each job takes uv's version from the footfall image. Dependabot's update to the image
    then moves CI's uv with it. A version written into a workflow falls behind unnoticed.
    """
    image = (ROOT / "images" / "footfall" / "Dockerfile").read_text()
    found = re.findall(r"^FROM ghcr\.io/astral-sh/uv:([0-9.]+)@sha256:", image, re.MULTILINE)
    assert len(found) == 1, "the sed in READ_UV must find exactly one version"
    setups = 0
    for workflow in WORKFLOWS:
        text = workflow.read_text()
        assert not re.search(r"^\s+version: \"?\d+\.\d+\.\d+", text, re.MULTILINE), workflow.name
        for before, after in zip(text.split(SETUP_UV)[:-1], text.split(SETUP_UV)[1:], strict=True):
            setups += 1
            assert before.endswith(READ_UV + "      "), f"{workflow.name}: read uv's version first"
            step = after.split("\n      - ", 1)[0]
            assert "          version: ${{ steps.uv.outputs.version }}\n" in step, workflow.name
    assert setups == 6
