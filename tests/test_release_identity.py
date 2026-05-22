import json
import subprocess
import sys

import yaml

from src.deploy import ReleaseHistory, ReleaseIdentity, render_manifest
from src.deploy.release_identity import (
    COMMIT_SHA_KEY,
    IMAGE_DIGEST_KEY,
    PACKAGE_VERSION_KEY,
    RELEASE_ID_KEY,
    SOURCE_REF_KEY,
)


def sample_workload():
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "agent-worker",
            "annotations": {"existing": "kept"},
        },
        "spec": {
            "template": {
                "metadata": {"annotations": {}},
                "spec": {
                    "containers": [
                        {"name": "worker", "image": "agent:main"},
                    ],
                },
            },
        },
    }


def release_identity():
    return ReleaseIdentity(
        commit_sha="abc123def4567890",
        package_version="2.4.1",
        image_digest="sha256:0123456789abcdef",
        source_ref="main",
        source_ref_type="branch",
    )


def test_render_manifest_uses_immutable_release_id_annotations():
    rendered = render_manifest(sample_workload(), release_identity())
    annotations = rendered["metadata"]["annotations"]
    pod_annotations = rendered["spec"]["template"]["metadata"]["annotations"]

    assert annotations[RELEASE_ID_KEY] == "2.4.1-abc123def456-0123456789ab"
    assert annotations[COMMIT_SHA_KEY] == "abc123def4567890"
    assert annotations[PACKAGE_VERSION_KEY] == "2.4.1"
    assert annotations[IMAGE_DIGEST_KEY] == "sha256:0123456789abcdef"
    assert pod_annotations[RELEASE_ID_KEY] == annotations[RELEASE_ID_KEY]
    assert annotations["existing"] == "kept"


def test_branch_ref_is_secondary_not_primary_identity():
    rendered = render_manifest(sample_workload(), release_identity())

    assert rendered["release"]["id"] == "2.4.1-abc123def456-0123456789ab"
    assert rendered["release"]["primary_identity"] == "release_id"
    assert rendered["release"]["source_ref"] == "main"
    assert rendered["metadata"]["annotations"][SOURCE_REF_KEY] == "main"
    assert rendered["release_history_key"] == rendered["release"]["id"]


def test_release_history_can_query_by_release_identifier():
    history = ReleaseHistory()
    rendered = render_manifest(sample_workload(), release_identity())

    release_id = history.record(rendered)
    stored = history.get(release_id)

    assert release_id == "2.4.1-abc123def456-0123456789ab"
    assert stored == rendered
    assert history.list_release_ids() == [release_id]
    assert history.get("main") is None


def test_cli_renders_manifest_with_release_identity(tmp_path):
    manifest_path = tmp_path / "agent.yaml"
    manifest_path.write_text(yaml.safe_dump(sample_workload()))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.cli.main",
            "deploy",
            str(manifest_path),
            "--commit-sha",
            "abc123def4567890",
            "--package-version",
            "2.4.1",
            "--image-digest",
            "sha256:0123456789abcdef",
            "--source-ref",
            "main",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    rendered = json.loads(result.stdout)
    assert rendered["release"]["id"] == "2.4.1-abc123def456-0123456789ab"
    assert rendered["release"]["primary_identity"] == "release_id"
