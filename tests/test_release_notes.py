import tomllib

import pytest

from localplaud import __version__
from localplaud.system_info import package_version
from scripts.release_notes import ROOT, release_notes

CHANGELOG = "# Changelog\n\n## [0.2.0]\n\n- New feature\n\n## [0.1.0] - 2026-11-04\n\nOld notes\n"


def test_current_version_has_release_notes_and_packaging_uses_source_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["hatch"]["version"]["path"] == "src/localplaud/__init__.py"
    assert package_version() == __version__
    assert release_notes(f"v{__version__}", __version__, (ROOT / "CHANGELOG.md").read_text())


def test_release_notes_extract_only_requested_version():
    assert release_notes("v0.2.0", "0.2.0", CHANGELOG) == "- New feature"


@pytest.mark.parametrize("tag", ["0.2.0", "v0.1.0", "v0.02.0", "v0.2.0-rc1", "v0.2.0\n"])
def test_release_rejects_an_invalid_or_mismatched_tag(tag):
    with pytest.raises(ValueError, match="stable release tag"):
        release_notes(tag, "0.2.0", CHANGELOG)


@pytest.mark.parametrize("changelog", ["# Changelog", "## [0.2.0]\n\n", CHANGELOG + "\n## [0.2.0]\nDuplicate"])
def test_release_requires_one_nonempty_changelog_entry(changelog):
    with pytest.raises(ValueError, match="CHANGELOG.md"):
        release_notes("v0.2.0", "0.2.0", changelog)
