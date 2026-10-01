"""Validate image delivery and actual rendered renderer deployment contracts."""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "update_argocd_images", ROOT / "scripts/update_argocd_images.py"
)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


def load(path, *, github=False):
    return yaml.load(
        (ROOT / path).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader if github else yaml.SafeLoader,
    )


def test_both_image_workflows_build_the_renderer_from_its_own_context():
    for filename, job in (
        ("docker-build.yml", "build"),
        ("docker-release.yml", "release"),
    ):
        workflow = load(f".github/workflows/{filename}", github=True)
        build = workflow["jobs"][job]
        assert build["needs"] == "validate"
        assert (
            workflow["jobs"]["validate"]["uses"]
            == "./.github/workflows/deploy-validate.yml"
        )
        images = {
            entry["image"]: entry for entry in build["strategy"]["matrix"]["include"]
        }
        assert images["landppt"]["context"] == "."
        assert images["landppt-pptx-renderer"]["context"] == "./scripts/pptx-renderer"
        assert (
            images["landppt-pptx-renderer"]["dockerfile"]
            == "./scripts/pptx-renderer/Dockerfile"
        )
        push = next(
            step
            for step in build["steps"]
            if step.get("uses", "").startswith("docker/build-push-action@")
        )
        assert push["with"]["context"] == "${{ matrix.context }}"
        assert push["with"]["file"] == "${{ matrix.dockerfile }}"
        assert "scope=${{ matrix.image }}" in push["with"]["cache-to"]
    assert workflow["env"]["PLATFORMS"] == "linux/amd64,linux/arm64"


def test_deployment_tag_update_waits_for_both_images_and_excludes_prs():
    workflow = load(".github/workflows/docker-build.yml", github=True)
    assert "scripts/pptx-renderer/**" in workflow["on"]["push"]["paths"]
    update = workflow["jobs"]["update-deployment"]
    assert update["needs"] == "build"
    assert "github.event_name != 'pull_request'" in update["if"]
    assert "push_image != 'false'" in update["if"]
    assert "refs/heads/master" in update["if"]
    assert "refs/heads/main" in update["if"]
    command = update["steps"][-1]["run"]
    assert "scripts/update_argocd_images.py" in command
    assert "--renderer-repository" in command
    assert "git-${{ github.sha }}" == update["steps"][-1]["env"]["IMAGE_TAG"]


def test_updater_changes_both_images_and_preserves_other_settings():
    text = (ROOT / "helm/landppt/values-argocd.yaml").read_text(encoding="utf-8")
    before = yaml.safe_load(text)
    updated = updater.update_images(
        text,
        "ghcr.io/example/landppt",
        "ghcr.io/example/landppt-pptx-renderer",
        "git-test",
    )
    after = yaml.safe_load(updated)
    assert after["image"] == {
        "repository": "ghcr.io/example/landppt",
        "tag": "git-test",
        "pullPolicy": "IfNotPresent",
    }
    assert after["pptxRenderer"]["image"] == {
        "repository": "ghcr.io/example/landppt-pptx-renderer",
        "tag": "git-test",
        "pullPolicy": "IfNotPresent",
    }
    before.pop("image")
    after.pop("image")
    before["pptxRenderer"].pop("image")
    after["pptxRenderer"].pop("image")
    assert before == after
    assert [line for line in text.splitlines() if line.lstrip().startswith("#")] == [
        line for line in updated.splitlines() if line.lstrip().startswith("#")
    ]
    assert (
        updater.update_images(
            updated,
            "ghcr.io/example/landppt",
            "ghcr.io/example/landppt-pptx-renderer",
            "git-test",
        )
        == updated
    )


@pytest.mark.parametrize("defect", ["missing", "duplicate"])
def test_updater_rejects_incomplete_or_ambiguous_image_settings(defect):
    text = (ROOT / "helm/landppt/values-argocd.yaml").read_text(encoding="utf-8")
    tag_line = next(
        line for line in text.splitlines(keepends=True) if line.startswith("    tag:")
    )
    if defect == "missing":
        text = text.replace(tag_line, "", 1)
    else:
        text = text.replace(tag_line, tag_line + "    tag: duplicate\n", 1)
    with pytest.raises(ValueError):
        updater.update_images(text, "app", "renderer", "git-test")


def test_production_compose_uses_published_renderer_and_dev_still_builds():
    production = load("docker-compose.yml")["services"]["pptx-renderer"]
    development = load("docker-compose-dev.yaml")["services"]["pptx-renderer"]
    assert "build" not in production
    assert "ghcr.io/sligter/landppt-pptx-renderer:latest" in production["image"]
    assert development["build"] == "./scripts/pptx-renderer"
    for renderer in (production, development):
        assert renderer["network_mode"] == "none"
        assert renderer["read_only"] is True
        assert renderer["user"] == "10001:10001"


@pytest.fixture
def helm():
    executable = os.environ.get("LANDPPT_TEST_HELM") or shutil.which("helm")
    if not executable:
        pytest.skip("Helm is needed for rendered manifest checks")
    return executable


def render(helm, *options, expect_error=False):
    command = [
        helm,
        "template",
        "landppt",
        "helm/landppt",
        "--set",
        "app.existingSecret=validation-app",
        "--set",
        "postgresql.auth.existingSecret=validation-postgresql",
        "--set",
        "minio.auth.existingSecret=validation-minio",
        "--set",
        "storage.s3.existingSecret=validation-minio",
        *options,
    ]
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30
    )
    if expect_error:
        assert result.returncode != 0
        return result.stderr
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def named(documents, kind, name):
    return next(
        document
        for document in documents
        if document["kind"] == kind and document["metadata"]["name"] == name
    )


def consumer_specs(documents):
    return [
        named(documents, "Deployment", name)["spec"]["template"]["spec"]
        for name in ("landppt", "landppt-worker")
    ]


def test_argocd_runs_migrations_before_web_and_worker_rollout(helm):
    documents = render(helm, "-f", "helm/landppt/values-argocd.yaml")
    job = named(documents, "Job", "landppt-migrate")
    annotations = job["metadata"]["annotations"]
    assert annotations["argocd.argoproj.io/hook"] == "Sync"
    assert set(annotations["argocd.argoproj.io/hook-delete-policy"].split(",")) == {
        "BeforeHookCreation",
        "HookSucceeded",
    }
    assert "helm.sh/hook" not in annotations
    migration_wave = int(annotations["argocd.argoproj.io/sync-wave"])
    for kind, name in (
        ("ConfigMap", "landppt"),
        ("Secret", "landppt"),
        ("StatefulSet", "landppt-postgresql"),
    ):
        metadata = named(documents, kind, name)["metadata"]
        assert (
            int(metadata.get("annotations", {}).get("argocd.argoproj.io/sync-wave", 0))
            < migration_wave
        )
    for name in ("landppt", "landppt-worker"):
        deployment = named(documents, "Deployment", name)
        assert (
            int(deployment["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
            > migration_wave
        )
        assert (
            job["spec"]["template"]["spec"]["containers"][0]["image"]
            == deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        )
    assert job["spec"]["template"]["spec"]["containers"][0]["command"] == [
        "python",
        "-m",
        "landppt.cli",
        "migrate-and-bootstrap",
    ]
    config = named(documents, "ConfigMap", "landppt")
    assert config["data"]["LANDPPT_AUTO_MIGRATE_ON_STARTUP"] == "false"


def test_default_chart_keeps_explicit_migration_policy(helm):
    documents = render(helm)
    assert not any(
        item["kind"] == "Job" and item["metadata"]["name"] == "landppt-migrate"
        for item in documents
    )
    for name in ("landppt", "landppt-worker"):
        annotations = named(documents, "Deployment", name)["metadata"].get(
            "annotations", {}
        )
        assert "argocd.argoproj.io/sync-wave" not in annotations


@pytest.mark.parametrize(
    "overlay", [None, "values-production.yaml", "values-argocd.yaml"]
)
@pytest.mark.parametrize("network_policy", [False, True])
def test_rendered_workloads_share_the_spool_and_keep_renderer_isolated(
    helm, overlay, network_policy
):
    options = ["--set", f"networkPolicy.enabled={str(network_policy).lower()}"]
    if overlay:
        options += ["-f", f"helm/landppt/{overlay}"]
    documents = render(helm, *options)
    claim = named(documents, "PersistentVolumeClaim", "landppt-pptx-spool")
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    renderer = named(documents, "Deployment", "landppt-pptx-renderer")
    assert renderer["spec"]["strategy"]["type"] == "Recreate"
    pod = renderer["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    container = pod["containers"][0]
    assert "envFrom" not in container
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["runAsUser"] == 10001
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["resources"]["limits"]["memory"] == "1Gi"
    assert {mount["mountPath"] for mount in container["volumeMounts"]} == {
        "/spool",
        "/tmp",
    }
    for application in consumer_specs(documents):
        app = application["containers"][0]
        assert {item["name"]: item.get("value") for item in app["env"]}[
            "LANDPPT_PPTX_RENDER_SPOOL"
        ] == "/app/pptx-render-spool"
        volume = next(
            volume
            for volume in application["volumes"]
            if volume["name"] == "pptx-render-spool"
        )
        assert volume["persistentVolumeClaim"]["claimName"] == claim["metadata"]["name"]
        term = application["affinity"]["podAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ][-1]
        assert term["topologyKey"] == "kubernetes.io/hostname"
        assert (
            term["labelSelector"]["matchLabels"]
            == renderer["spec"]["template"]["metadata"]["labels"]
        )
    policy = named(documents, "NetworkPolicy", "landppt-pptx-renderer")["spec"]
    assert policy["policyTypes"] == ["Ingress", "Egress"]
    assert policy["ingress"] == policy["egress"] == []
    if network_policy:
        general = named(documents, "NetworkPolicy", "landppt")["spec"]
        assert general["podSelector"]["matchExpressions"] == [
            {
                "key": "app.kubernetes.io/component",
                "operator": "NotIn",
                "values": ["pptx-renderer"],
            }
        ]


def test_disabling_renderer_removes_its_resources_mounts_and_affinity(helm):
    documents = render(helm, "--set", "pptxRenderer.enabled=false")
    assert not any("pptx" in document["metadata"]["name"] for document in documents)
    for pod in consumer_specs(documents):
        assert "affinity" not in pod
        assert not any(
            volume["name"] == "pptx-render-spool" for volume in pod["volumes"]
        )
        assert not any(
            item["name"] == "LANDPPT_PPTX_RENDER_SPOOL"
            for item in pod["containers"][0]["env"]
        )


def test_rwx_existing_claim_and_custom_mount_support_multi_node_scheduling(helm):
    documents = render(
        helm,
        "--set",
        "pptxRenderer.spool.existingClaim=shared-render-spool",
        "--set",
        "pptxRenderer.spool.accessModes[0]=ReadWriteMany",
        "--set",
        "pptxRenderer.spool.mountPath=/app/render-jobs",
    )
    assert not any(
        document["kind"] == "PersistentVolumeClaim"
        and document["metadata"]["name"] == "landppt-pptx-spool"
        for document in documents
    )
    for pod in consumer_specs(documents):
        assert "affinity" not in pod
        app = pod["containers"][0]
        assert any(
            mount["mountPath"] == "/app/render-jobs" for mount in app["volumeMounts"]
        )
        assert any(
            volume.get("persistentVolumeClaim", {}).get("claimName")
            == "shared-render-spool"
            for volume in pod["volumes"]
        )


def test_rwo_affinity_preserves_custom_affinity_terms(helm, tmp_path):
    custom = tmp_path / "values.yaml"
    custom.write_text(
        yaml.safe_dump(
            {
                "affinity": {
                    "podAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": [
                            {
                                "topologyKey": "custom-zone",
                                "labelSelector": {"matchLabels": {"custom": "label"}},
                            }
                        ]
                    },
                    "nodeAffinity": {
                        "preferredDuringSchedulingIgnoredDuringExecution": [
                            {
                                "weight": 1,
                                "preference": {
                                    "matchExpressions": [
                                        {
                                            "key": "node-tier",
                                            "operator": "In",
                                            "values": ["compute"],
                                        }
                                    ]
                                },
                            }
                        ]
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    documents = render(helm, "-f", str(custom))
    for pod in consumer_specs(documents):
        affinity = pod["affinity"]
        assert (
            affinity["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
                0
            ]["topologyKey"]
            == "custom-zone"
        )
        assert "nodeAffinity" in affinity


def test_single_pod_only_volume_mode_is_rejected(helm):
    error = render(
        helm,
        "--set",
        "pptxRenderer.spool.accessModes[0]=ReadWriteOncePod",
        expect_error=True,
    )
    assert "ReadWriteOncePod is unsupported" in error
