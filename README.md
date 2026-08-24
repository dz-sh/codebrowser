# Code Browser

Browse already-built C and C++ projects with [KDAB Code Browser](https://github.com/KDAB/codebrowser).

Your normal build stays unchanged. Code Browser reads the source tree, the build tree, and its `compile_commands.json`, then serves the generated index at `http://localhost:8080`. The same source tree can also be shared with OpenGrok.

## Before you start

You need:

- Docker with Docker Compose;
- one or more source trees;
- a successful build with a valid `compile_commands.json`.

Code Browser does not compile your project. Generate the compilation database through your existing build system, for example with CMake's `CMAKE_EXPORT_COMPILE_COMMANDS`, Ninja's `compdb`, Bear, or a project-provided helper.

## Quick start

### 1. Configure your workspace

Copy the example configuration:

```bash
cp .env.example .env
```

Edit `.env` so that `SOURCE_ROOT` contains your source repositories and `BUILD_ROOT` contains their build directories. Use absolute paths:

```dotenv
SOURCE_ROOT=/srv/workspace/source
BUILD_ROOT=/srv/workspace/build
CODEBROWSER_PORT=8080
```

For example:

```text
/srv/workspace/source/my-project
/srv/workspace/build/my-project-debug/compile_commands.json
```

### 2. Register a completed build

For an out-of-tree build:

```bash
./codebrowser register-build \
  --source /srv/workspace/source/my-project \
  --build /srv/workspace/build/my-project-debug
```

For an in-tree build, omit `--build`:

```bash
./codebrowser register-build \
  --source /srv/workspace/source/my-project
```

Registration checks the compilation database and creates the repository configuration automatically. Run it only after the build has completed successfully.

### 3. Start Code Browser

```bash
./codebrowser up
```

Open <http://localhost:8080>. To stop the service:

```bash
./codebrowser down
```

## Add repositories or build variants

Repeat `register-build` for every repository or build variant you want to browse:

```bash
./codebrowser register-build \
  --source /srv/workspace/source/linux \
  --build /srv/workspace/build/linux-x86_64

./codebrowser register-build \
  --source /srv/workspace/source/linux \
  --build /srv/workspace/build/linux-arm64

./codebrowser register-build \
  --source /srv/workspace/source/qemu \
  --build /srv/workspace/build/qemu-debug

./codebrowser up
```

Each build variant gets its own index. Symbols are not combined across repositories or build variants.

Names are inferred from the source and build directory names. If two entries would have the same name, assign one explicitly:

```bash
./codebrowser register-build \
  --source /srv/workspace/source/linux \
  --build /srv/workspace/build/linux-x86_64 \
  --view linux-x86_64
```

## Keep an index current

| Situation | What to do |
| --- | --- |
| The Git commit changed | Rebuild the project, run `register-build` again, then run `./codebrowser up`. |
| Build options changed without a new commit | Register the completed build, then run `./codebrowser regenerate --force VIEW`. |
| A non-Git source tree changed | Rebuild it, then run `./codebrowser regenerate --force VIEW`. |
| Nothing changed | Run `./codebrowser up`; the existing index is reused. |

If a Git source tree moves to a new commit before its build is registered, its stale view is hidden instead of being regenerated with mismatched build files.

## Linux kernel example

The Linux kernel is handled like any other out-of-tree build. After building it and generating its compilation database:

```bash
make -C /srv/workspace/source/linux \
  O=/srv/workspace/build/linux-x86_64 \
  LLVM=1 defconfig

make -C /srv/workspace/source/linux \
  O=/srv/workspace/build/linux-x86_64 \
  LLVM=1 -j"$(nproc)"

python3 /srv/workspace/source/linux/scripts/clang-tools/gen_compile_commands.py \
  -d /srv/workspace/build/linux-x86_64 \
  -o /srv/workspace/build/linux-x86_64/compile_commands.json

./codebrowser register-build \
  --source /srv/workspace/source/linux \
  --build /srv/workspace/build/linux-x86_64

./codebrowser up
```

## Local data

Repository configuration is generated under `config/repositories/`. Generated indexes are stored under `output/`. Both are ordinary host directories; Docker named volumes are not used.

The default image is `ghcr.io/dz-sh/codebrowser:latest`. Set `CODEBROWSER_IMAGE` in `.env` if you want to pin a specific release.

## License

KDAB Code Browser has separate licensing terms, including restrictions on commercial use. Review the [upstream license](https://github.com/KDAB/codebrowser/blob/master/LICENSE) before using it.

The deployment code in this repository is licensed under Apache-2.0.
