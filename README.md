# Code Browser deployment

This repository deploys [KDAB Code Browser](https://github.com/KDAB/codebrowser) for build-aware C and C++ source browsing. It is designed to share a host source tree with tools such as OpenGrok while consuming compilation databases and generated files from separate build trees.

The boundary is intentionally narrow:

- your build system owns source checkout, configuration, compilation, generated headers, and `compile_commands.json`;
- this project records successful build views, generates static Code Browser output, and serves it with nginx;
- GitHub Actions runs fast tests for pull requests and `main` commits, and validates image builds only when packaging inputs change; only a stable GitHub Release publishes the generator image;
- each build view has an isolated KDAB index. Repositories and build views are never merged into a cross-repository symbol database.

## Host layout

A typical multi-repository workspace looks like this:

```text
/srv/workspace/
├── source/
│   ├── linux/
│   ├── qemu/
│   └── edk2/
└── build/
    ├── linux-x86_64/
    ├── linux-arm64/
    └── qemu-debug/
```

OpenGrok can mount the entire `source/` directory. Code Browser mounts the same directory read-only and additionally mounts `build/` read-only.

## Configure the deployment

Copy the environment example and set absolute host paths:

```bash
cp .env.example .env
```

```dotenv
SOURCE_ROOT=/srv/workspace/source
BUILD_ROOT=/srv/workspace/build
CODEBROWSER_PORT=8080
CODEBROWSER_IMAGE=ghcr.io/dz-sh/codebrowser:latest
```

The paths are mounted into the generator container at the same absolute locations. This preserves absolute paths already stored in `compile_commands.json`.

The `./codebrowser` wrapper automatically runs the generator with the current host UID/GID. If Compose is invoked directly by a non-1000 Linux user, also set `CODEBROWSER_UID` and `CODEBROWSER_GID` in `.env`.

## Register a successful build

Registration is the build-to-index contract. Run it only after the build and compilation database have completed successfully:

```bash
cmake -S /srv/workspace/source/my-project \
      -B /srv/workspace/build/my-project-debug \
      -DCMAKE_EXPORT_COMPILE_COMMANDS=ON

cmake --build /srv/workspace/build/my-project-debug && \
  ./codebrowser register-build \
    --source /srv/workspace/source/my-project \
    --build /srv/workspace/build/my-project-debug
```

For an in-tree build, omit `--build`:

```bash
make && ./codebrowser register-build --source /srv/workspace/source/my-project
```

The command validates `compile_commands.json` and generates or updates:

```text
config/repositories/<repository>/codebrowser.yaml
```

For a Git repository, the generated view records the current full `HEAD` as `built_commit`. The default repository ID is the source directory name; the default view ID is the build directory name. Use `--repository` or `--view` only when those defaults are ambiguous.

One repository can have several independently built views:

```yaml
version: 1
repository:
  id: linux
  source: linux
views:
  linux-x86_64:
    build:
      root: build
      path: linux-x86_64
    compilation_database: compile_commands.json
    built_commit: 58717b2a13650000000000000000000000000000
  linux-arm64:
    build:
      root: build
      path: linux-arm64
    compilation_database: compile_commands.json
    built_commit: a91c738bd1730000000000000000000000000000
```

The descriptor format is documented by [`codebrowser.schema.json`](codebrowser.schema.json). Generated descriptors are local deployment state and are ignored by Git.

## Start or reconcile

```bash
docker compose up -d
```

Or use the small convenience wrapper:

```bash
./codebrowser up
```

Every `up` runs the one-shot generator before nginx. For each configured view:

- missing source, build, or compilation database: masked;
- Git `HEAD` different from `built_commit`: masked;
- matching commit with an existing cache: reused;
- matching commit without a cache: generated and published;
- non-Git source: generated once and then reused.

Only build-consistent views appear in the public landing page. Masking removes the public symlink but retains the cached static output. OpenGrok remains free to expose every repository in the shared source tree.

Open <http://localhost:8080> after the generator finishes.

## Source changed before the build

If a Git repository advances from commit `A` to `B` while its registered build still records `A`, Code Browser masks that view. Rebuild and register it again:

```bash
cmake --build /srv/workspace/build/my-project-debug && \
  ./codebrowser register-build \
    --source /srv/workspace/source/my-project \
    --build /srv/workspace/build/my-project-debug

docker compose up -d
```

This prevents a new source tree from being indexed with stale generated headers or compile commands.

## Force regeneration

Changes within the same Git commit—such as a different build path, build configuration, generated header, or compilation database—are intentionally not auto-detected. After registering the completed build, force one view:

```bash
./codebrowser regenerate --force my-project-debug
```

Or every valid view:

```bash
./codebrowser regenerate --force-all
```

Force regeneration still requires `HEAD == built_commit`. The explicit escape hatch is:

```bash
./codebrowser regenerate \
  --force my-project-debug \
  --allow-stale-build
```

It should be used only when the user accepts that the source and build contexts do not match.

## Output

All generated content stays in the host `output/` directory:

```text
output/
├── cache/<view>/<commit-or-once>/
└── public/
    ├── index.html
    └── views/<view> -> ../../cache/<view>/<commit-or-once>
```

nginx serves only `output/public`. No Docker named volumes are created.

## Linux kernel example

Linux is one consumer of the generic compilation-database interface, not a special case in the generator. A typical out-of-tree workflow is:

```bash
make -C /srv/workspace/source/linux O=/srv/workspace/build/linux-x86_64 LLVM=1 defconfig
make -C /srv/workspace/source/linux O=/srv/workspace/build/linux-x86_64 LLVM=1 -j"$(nproc)"
python3 /srv/workspace/source/linux/scripts/clang-tools/gen_compile_commands.py \
  -d /srv/workspace/build/linux-x86_64 \
  -o /srv/workspace/build/linux-x86_64/compile_commands.json

./codebrowser register-build \
  --source /srv/workspace/source/linux \
  --build /srv/workspace/build/linux-x86_64
```

Then run `docker compose up -d`. Changing the Linux revision without rebuilding masks the view until the build is completed and registered again.

## Images and licensing

The generator image is built only by the GitHub Actions pipelines in this repository. Every pull request and `main` commit runs the fast control-plane tests. Changes to image packaging inputs additionally build the `linux/amd64` image without pushing it. Publishing a stable GitHub Release checks out the release tag and pushes the release tag, semantic-version tags, an immutable `sha-*` tag, and `latest` to GHCR. Ordinary `main` commits can never update `latest`.

Deployment and development machines do not compile KDAB Code Browser. Compose pulls `ghcr.io/dz-sh/codebrowser:latest` by default; set `CODEBROWSER_IMAGE` to a release or immutable `sha-*` tag when a pinned deployment is required.

The image builds KDAB Code Browser from the pinned upstream commit declared by `CODEBROWSER_REF` in the Dockerfile. The included upstream version requires LLVM/Clang 16 or later; the pipeline currently builds against LLVM 18 on Ubuntu 24.04 for `linux/amd64`.

KDAB Code Browser has its own licensing terms, including non-commercial restrictions in its upstream license. Review the [upstream license](https://github.com/KDAB/codebrowser/blob/master/LICENSE) before using or distributing generated output. A copy is included in the image under `/opt/codebrowser/share/licenses/KDAB-codebrowser/`.

This repository's original deployment code is licensed under Apache-2.0.
