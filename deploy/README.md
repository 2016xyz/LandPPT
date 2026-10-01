# PPTX renderer delivery and deployment

The renderer uses a shared job directory rather than an HTTP service. Application
containers write requests to `LANDPPT_PPTX_RENDER_SPOOL`; the isolated LibreOffice
container reads the same volume at `/spool`. It receives no application secrets.

## CI/CD

`docker-build.yml` builds `landppt` and `landppt-pptx-renderer` as separate matrix
entries. Renderer source changes trigger this workflow. PRs and manual runs with
`push_image=false` build without pushing. GHCR is always used for publication;
Docker Hub is also used when its username and token secrets are configured.
The two build caches have separate scopes. Both build and release workflows
require the reusable deployment validation workflow to pass first.

After **both** images are published, a separate job updates the application and
renderer entries in `helm/landppt/values-argocd.yaml` to the same immutable
`git-<commit>` tag. Failed builds do not update deployment tags. This job runs
only for `main` or `master`, and preserves other values and comments. Configure
the repository to allow the Actions bot to commit to the deployment branch.

`docker-release.yml` publishes both images with matching version tags for
`linux/amd64` and `linux/arm64`. `deploy-validate.yml` tests the rendered chart,
image-tag updater and Compose configuration on relevant changes.

## Docker Compose

Production Compose uses the published renderer image. Select corresponding
application and renderer tags, for example:

```sh
export LANDPPT_IMAGE="ghcr.io/sligter/landppt:git-<commit>"
export LANDPPT_PPTX_RENDERER_IMAGE="ghcr.io/sligter/landppt-pptx-renderer:git-<commit>"
docker compose pull
docker compose up -d
```

Development Compose continues to build `scripts/pptx-renderer` locally. Both
configurations initialize the shared volume for UID/GID 10001 and keep the
renderer network disabled, filesystem read-only, capabilities dropped and
temporary storage bounded. The standalone Compose file remains available for a
locally running Python application.

## Helm and Argo CD

The Argo CD overlay runs `python -m landppt.cli migrate-and-bootstrap` as a
`Sync` hook in wave 1. ConfigMaps, Secrets, PostgreSQL and other dependencies
remain in wave 0; Web and Worker Deployments run in wave 2. A failed migration
blocks rollout. `BeforeHookCreation,HookSucceeded` recreates the named Job on
each sync and removes successful Jobs, avoiding immutable Job update errors.
Application startup migration remains disabled so replicas do not race schema
updates. Use a full application sync; selective resource sync skips hooks.
See [Argo CD sync phases and waves](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-waves/).

Upgrading databases from before template packages requires migration `019`,
which adds `global_master_templates.template_kind` and package tables while
preserving existing templates. `create_all` alone does not add this column to
an existing table. If it is missing, both ordinary template queries and package
queries fail. To repair a deployment before its next full sync, run the existing
migration command from an application container:

```sh
kubectl -n landppt exec deployment/landppt -- python -m landppt.cli migrate
```

The command is idempotent. Diagnose failed syncs using the retained migration
Job logs (`kubectl -n landppt logs job/landppt-migrate`).

`pptxRenderer.enabled` defaults to true. The chart creates a renderer Deployment,
a dedicated spool PVC and an ingress/egress deny-all NetworkPolicy. Web and
Worker mount the same claim and receive the spool path explicitly. The renderer
uses the application's UID/GID so it can access private per-job directories.
Only `/spool` and its bounded `/tmp` are writable. No Service is required.

The current Argo CD overlay uses single-node local-path storage with
`ReadWriteOnce`. Web and Worker have required node-level affinity to the renderer,
so all consumers can mount that claim. This also applies when application data
persistence is disabled. It constrains scheduling and scaling to that node.

For a multi-node deployment, use a storage class supporting `ReadWriteMany`:

```yaml
pptxRenderer:
  spool:
    storageClass: your-rwx-storage-class
    accessModes: [ReadWriteMany]
```

An existing shared claim can be selected with `pptxRenderer.spool.existingClaim`;
set `accessModes` to match the real claim. `ReadWriteOncePod` is rejected because
the volume is shared by several Pods. When using existing node-bound application
PVCs, ensure they and the renderer spool are schedulable on the same node, or use
RWX for the spool. Disabling the renderer removes its resources, spool mounts,
environment variables and additional affinity.

Network isolation requires a CNI that enforces Kubernetes NetworkPolicy. The
application's optional general allow policy excludes the renderer, since
NetworkPolicy allows are additive. The dedicated deny policy is rendered even
when the general policy is disabled. Keep registry credentials in
`imagePullSecrets` when GHCR packages are private.

The initial renderer `latest` tag in `values-argocd.yaml` bootstraps the first CI
publication. That first build must finish before the new renderer can start;
subsequent CI updates pin both images to a matching commit. Chart changes alone
are reconciled by Argo CD and do not rebuild the images.
