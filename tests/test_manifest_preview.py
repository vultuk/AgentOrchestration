from src.cli.main import cli
from src.cli.manifest_preview import (
    ManifestPreviewError,
    build_review_diff,
    render_preview,
)


def test_dry_run_renders_candidate_and_redacts_sensitive_diff(
    tmp_path,
    capsys,
):
    manifest = tmp_path / "release.yaml"
    manifest.write_text(
        """
name: checkout
resources:
  - kind: Deployment
    name: api
    env:
      IMAGE_TAG: old
      API_TOKEN: original-secret
""",
        encoding="utf-8",
    )

    code = cli(
        [
            "deploy",
            str(manifest),
            "--dry-run",
            "--set",
            "resources.0.env.IMAGE_TAG=new",
            "--set",
            "resources.0.env.API_TOKEN=replacement-secret",
        ]
    )

    captured = capsys.readouterr()

    assert code == 0
    assert "Dry-run manifest validation passed." in captured.out
    assert "resources.0.env.IMAGE_TAG: \"old\" -> \"new\"" in captured.out
    assert (
        "resources.0.env.API_TOKEN: <redacted> -> <redacted>"
        in captured.out
    )
    assert "replacement-secret" not in captured.out
    assert captured.err == ""


def test_invalid_manifest_fails_before_deploy_approval(tmp_path, capsys):
    manifest = tmp_path / "release.yaml"
    manifest.write_text(
        """
resources:
  - kind: Deployment
    name: api
""",
        encoding="utf-8",
    )

    code = cli(["deploy", str(manifest), "--dry-run"])
    captured = capsys.readouterr()

    assert code == 1
    assert "Dry-run validation failed" in captured.err
    assert "requires name or metadata.name" in captured.err
    assert "Deploying agent" not in captured.out


def test_manifest_preview_validates_resources_and_returns_rendered_copy(
    tmp_path,
):
    manifest = tmp_path / "release.json"
    manifest.write_text(
        """
{
  "metadata": {"name": "checkout"},
  "resources": [
    {"kind": "Service", "metadata": {"name": "api"}, "env": {"PORT": "8080"}}
  ]
}
""",
        encoding="utf-8",
    )

    rendered, changes = render_preview(
        str(manifest),
        ["resources.0.env.PORT=9090"],
    )

    assert rendered["resources"][0]["env"]["PORT"] == "9090"
    assert changes == ['resources.0.env.PORT: "8080" -> "9090"']


def test_review_diff_redacts_secret_like_paths():
    changes = build_review_diff(
        {"metadata": {"name": "api"}, "password": "old"},
        {"metadata": {"name": "api"}, "password": "new"},
    )

    assert changes == ["password: <redacted> -> <redacted>"]


def test_null_environment_value_is_rejected(tmp_path):
    manifest = tmp_path / "release.yaml"
    manifest.write_text(
        """
name: checkout
environment:
  REQUIRED: null
""",
        encoding="utf-8",
    )

    try:
        render_preview(str(manifest), [])
    except ManifestPreviewError as exc:
        assert "manifest.environment.REQUIRED cannot be null" in str(exc)
    else:
        raise AssertionError("expected dry-run validation failure")
