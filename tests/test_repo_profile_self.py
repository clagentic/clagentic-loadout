"""This repository keeps the profile it asks others to keep: it is a released
tool, its profile is valid by the review's own loader, and the tracked tree
carries nothing the class rules forbid."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from clagentic_loadout.review import profile_rules as pr
from clagentic_loadout.review import repo_profile as rp
from clagentic_loadout.review import stakes as stk
from clagentic_loadout.review.chunking import plan_chunks
from clagentic_loadout.review.policy_source import PROFILE_OK, PROFILE_PATH, PolicySource
from clagentic_loadout.review.profile_settings import ProfileSettings

REPO_ROOT = Path(__file__).resolve().parent.parent
_MAX_FILE_BYTES = 512 * 1024


def _own_profile() -> rp.RepoProfile:
    return rp.parse_profile_document(
        yaml.safe_load((REPO_ROOT / PROFILE_PATH).read_text(encoding="utf-8"))
    )


def _tracked_text_files() -> dict[str, list[str]]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout.decode("utf-8")
    files: dict[str, list[str]] = {}
    for name in filter(None, listing.split("\0")):
        path = REPO_ROOT / name
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            continue
        try:
            files[name] = path.read_text(encoding="utf-8").split("\n")
        except UnicodeDecodeError:
            continue
    return files


def _as_additions(files: dict[str, list[str]]) -> str:
    parts: list[str] = []
    for name, lines in files.items():
        if lines and lines[-1] == "":
            lines = lines[:-1]
        if not lines:
            continue
        parts += [
            f"diff --git a/{name} b/{name}",
            "new file mode 100644",
            "--- /dev/null",
            f"+++ b/{name}",
            f"@@ -0,0 +1,{len(lines)} @@",
            *(f"+{line}" for line in lines),
        ]
    return "\n".join(parts) + "\n"


def test_this_repository_is_a_released_tool_with_a_valid_profile():
    assert _own_profile().repo_class == rp.CLASS_RELEASED_TOOL


def test_the_shipped_example_profile_is_valid_and_exercises_every_field():
    text = (REPO_ROOT / ".clagentic" / "loadout" / "profile.yaml.example").read_text(encoding="utf-8")

    profile = rp.parse_profile_document(yaml.safe_load(text))

    assert profile.repo_class == rp.CLASS_RELEASED_TOOL
    assert profile.paths and profile.deny.names and profile.deny.hosts and profile.deny.users
    # tool_packages is shown commented out, because it applies to internal paths;
    # uncommented it must still be valid.
    uncommented = text.replace("# tool_packages:", "tool_packages:")
    assert rp.parse_profile_document(yaml.safe_load(uncommented)).tool_packages == ("some_tool",)


def test_the_profile_carries_no_denial_list_because_the_deployment_owns_it():
    assert _own_profile().deny == rp.ProfileDeny()


def _scan(deployment_deny: rp.ProfileDeny | None = None) -> list[dict]:
    policy = PolicySource((), stk.Stakes(), (), _own_profile(), PROFILE_OK, None)
    chunks = plan_chunks(_as_additions(_tracked_text_files()), 5000)
    return pr.profile_findings(
        policy, chunks, ProfileSettings(deny=deployment_deny or rp.ProfileDeny())
    )


def test_the_tracked_tree_carries_nothing_the_class_rules_forbid():
    found = _scan()

    assert found == [], "\n".join(f"{f['file']}:{f['line']}: {f['message']}" for f in found)


def test_the_scan_reads_the_whole_tracked_tree_so_a_clean_result_is_not_vacuous():
    assert len(_tracked_text_files()) > 200
    # A value known to appear in the tree is found, outside the exempted paths.
    hits = _scan(rp.ProfileDeny(names=("loadout",)))
    assert hits and all(not f["file"].startswith(".crew/") for f in hits)
