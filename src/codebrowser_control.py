#!/usr/bin/env python3
"""Control plane for build-consistent KDAB Code Browser views."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Repository:
    id: str
    source: Path


@dataclass(frozen=True)
class View:
    id: str
    repository: Repository
    build: Path
    compilation_database: Path
    built_commit: str | None


@dataclass(frozen=True)
class Settings:
    source_root: Path
    build_root: Path
    config_root: Path
    output_root: Path
    generator: Path
    indexgenerator: Path
    data_dir: Path

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            source_root=required_env_path("SOURCE_ROOT"),
            build_root=required_env_path("BUILD_ROOT"),
            config_root=env_path("CONFIG_ROOT", "/config/repositories"),
            output_root=env_path("OUTPUT_ROOT", "/output"),
            generator=env_path("CODEBROWSER_GENERATOR", "/opt/codebrowser/bin/codebrowser_generator"),
            indexgenerator=env_path(
                "CODEBROWSER_INDEXGENERATOR", "/opt/codebrowser/bin/codebrowser_indexgenerator"
            ),
            data_dir=env_path("CODEBROWSER_DATA_DIR", "/opt/codebrowser/share/woboq/data"),
        )


def env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser().resolve()


def required_env_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is required")
    return Path(value).expanduser().resolve()


def validate_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ConfigError(f"{label} must match {ID_RE.pattern}: {value!r}")
    return value


def safe_relative(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"{label} must stay within its configured root: {value!r}")
    return path


def require_within(path: Path, root: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ConfigError(f"{label} is outside {root}: {resolved}") from exc
    return resolved


def git_commit(source: Path) -> str | None:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={source}", "-C", str(source), "rev-parse", "--verify", "HEAD"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        return None
    commit = result.stdout.strip()
    return commit if re.fullmatch(r"[0-9a-fA-F]{40,64}", commit) else None


def atomic_yaml(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yaml.safe_dump(document, stream, sort_keys=False, allow_unicode=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return document


def validate_compilation_database(compdb: Path, source: Path, build: Path) -> None:
    try:
        entries = json.loads(compdb.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid compilation database {compdb}: {exc}") from exc
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"compilation database is empty or not an array: {compdb}")

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            raise ConfigError(f"compilation database entry {index} has no file: {compdb}")
        directory = entry.get("directory", str(compdb.parent))
        if not isinstance(directory, str):
            raise ConfigError(f"compilation database entry {index} has an invalid directory")
        directory_path = Path(directory)
        if not directory_path.is_absolute():
            directory_path = compdb.parent / directory_path
        file_path = Path(entry["file"])
        if not file_path.is_absolute():
            file_path = directory_path / file_path
        resolved = file_path.resolve()
        if not is_within(resolved, source) and not is_within(resolved, build):
            raise ConfigError(
                f"translation unit escapes the repository/build boundary: {resolved}"
            )


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def load_views(settings: Settings) -> list[View]:
    views: list[View] = []
    repository_ids: set[str] = set()
    view_ids: set[str] = set()

    for config_path in sorted(settings.config_root.glob("*/codebrowser.yaml")):
        document = read_yaml(config_path)
        if document.get("version") != 1:
            raise ConfigError(f"unsupported version in {config_path}; expected 1")

        repository_data = document.get("repository")
        if not isinstance(repository_data, dict):
            raise ConfigError(f"repository must be a mapping in {config_path}")
        repository_id = validate_id(repository_data.get("id"), "repository.id")
        if repository_id in repository_ids:
            raise ConfigError(f"duplicate repository id: {repository_id}")
        repository_ids.add(repository_id)
        if config_path.parent.name != repository_id:
            raise ConfigError(
                f"repository id {repository_id!r} must match directory {config_path.parent.name!r}"
            )

        source_relative = safe_relative(repository_data.get("source"), "repository.source")
        source = require_within(settings.source_root / source_relative, settings.source_root, "source")
        repository = Repository(repository_id, source)

        view_data = document.get("views")
        if not isinstance(view_data, dict) or not view_data:
            raise ConfigError(f"views must be a non-empty mapping in {config_path}")
        for view_id_raw, data in view_data.items():
            view_id = validate_id(view_id_raw, "view id")
            if view_id in view_ids:
                raise ConfigError(f"duplicate view id: {view_id}")
            view_ids.add(view_id)
            if not isinstance(data, dict):
                raise ConfigError(f"view {view_id} must be a mapping")

            build_data = data.get("build")
            if not isinstance(build_data, dict):
                raise ConfigError(f"view {view_id}.build must be a mapping")
            root_kind = build_data.get("root")
            if root_kind not in {"source", "build"}:
                raise ConfigError(f"view {view_id}.build.root must be source or build")
            build_relative = safe_relative(build_data.get("path"), f"view {view_id}.build.path")
            if root_kind == "source":
                build_base = source
            else:
                build_base = settings.build_root
            build = require_within(build_base / build_relative, build_base, f"view {view_id} build")

            compdb_relative = safe_relative(
                data.get("compilation_database", "compile_commands.json"),
                f"view {view_id}.compilation_database",
            )
            if compdb_relative.name != "compile_commands.json":
                raise ConfigError(
                    f"view {view_id}.compilation_database must be named compile_commands.json"
                )
            compdb = require_within(build / compdb_relative, build, f"view {view_id} compilation database")
            built_commit = data.get("built_commit")
            if built_commit is not None and (
                not isinstance(built_commit, str)
                or not re.fullmatch(r"[0-9a-fA-F]{40,64}", built_commit)
            ):
                raise ConfigError(f"view {view_id}.built_commit is invalid")

            views.append(
                View(
                    id=view_id,
                    repository=repository,
                    build=build,
                    compilation_database=compdb,
                    built_commit=built_commit,
                )
            )
    return views


def command_register_build(args: argparse.Namespace, settings: Settings) -> int:
    source = require_within(Path(args.source), settings.source_root, "source")
    build = Path(args.build or args.source).expanduser().resolve()
    if not source.is_dir():
        raise ConfigError(f"source directory does not exist: {source}")
    if not build.is_dir():
        raise ConfigError(f"build directory does not exist: {build}")

    if is_within(build, source):
        build_root = "source"
        build_relative = build.relative_to(source)
    elif is_within(build, settings.build_root):
        build_root = "build"
        build_relative = build.relative_to(settings.build_root)
    else:
        raise ConfigError(
            f"build must be within the source repository or BUILD_ROOT ({settings.build_root}): {build}"
        )

    compdb = Path(args.compilation_database) if args.compilation_database else build / "compile_commands.json"
    if not compdb.is_absolute():
        compdb = build / compdb
    compdb = require_within(compdb, build, "compilation database")
    if compdb.name != "compile_commands.json":
        raise ConfigError("compilation database must be named compile_commands.json")
    if not compdb.is_file():
        raise ConfigError(f"compilation database does not exist: {compdb}")
    validate_compilation_database(compdb, source, build)

    repository_id = validate_id(args.repository or slug(source.name), "repository id")
    default_view = repository_id if build == source else slug(build.name)
    view_id = validate_id(args.view or default_view, "view id")
    source_relative = source.relative_to(settings.source_root)
    compdb_relative = compdb.relative_to(build)
    commit = git_commit(source)

    config_path = settings.config_root / repository_id / "codebrowser.yaml"
    if config_path.exists():
        document = read_yaml(config_path)
        repository_data = document.get("repository")
        if not isinstance(repository_data, dict):
            raise ConfigError(f"invalid repository block in {config_path}")
        if repository_data.get("id") != repository_id or repository_data.get("source") != str(source_relative):
            raise ConfigError(f"existing repository identity does not match {source}: {config_path}")
        if not isinstance(document.get("views"), dict):
            raise ConfigError(f"invalid views block in {config_path}")
    else:
        document = {
            "version": 1,
            "repository": {"id": repository_id, "source": str(source_relative)},
            "views": {},
        }

    view_document: dict[str, Any] = {
        "build": {"root": build_root, "path": str(build_relative)},
        "compilation_database": str(compdb_relative),
    }
    if commit:
        view_document["built_commit"] = commit
    document["views"][view_id] = view_document
    atomic_yaml(config_path, document)

    revision = commit or "non-git"
    print(f"registered {repository_id}/{view_id} at {revision}")
    print(config_path)
    return 0


def slug(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    if not result or not ID_RE.fullmatch(result):
        raise ConfigError(f"cannot derive a safe identifier from {value!r}")
    return result


def command_reconcile(args: argparse.Namespace, settings: Settings) -> int:
    return reconcile_views(
        settings,
        selected=None,
        forced=set(args.force or []),
        force_all=args.force_all,
        allow_stale_build=args.allow_stale_build,
        fail_on_selected_invalid=False,
    )


def command_generate(args: argparse.Namespace, settings: Settings) -> int:
    selected = set(args.views) if args.views else None
    return reconcile_views(
        settings,
        selected=selected,
        forced=selected if args.force and selected is not None else set(),
        force_all=args.force and selected is None,
        allow_stale_build=args.allow_stale_build,
        fail_on_selected_invalid=selected is not None,
    )


def reconcile_views(
    settings: Settings,
    *,
    selected: set[str] | None,
    forced: set[str],
    force_all: bool,
    allow_stale_build: bool,
    fail_on_selected_invalid: bool,
) -> int:
    views = load_views(settings)
    configured = {view.id for view in views}
    requested = forced | (selected or set())
    unknown = requested - configured
    if unknown:
        raise ConfigError(f"unknown view(s): {', '.join(sorted(unknown))}")
    if allow_stale_build and not (forced or force_all):
        raise ConfigError("--allow-stale-build requires --force")

    public_views = settings.output_root / "public" / "views"
    cache_root = settings.output_root / "cache"
    public_views.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)
    mask_unconfigured(public_views, configured)

    valid: dict[str, tuple[View, str]] = {}
    selected_failed = False
    for view in views:
        force = force_all or view.id in forced
        try:
            revision = validate_view(view, allow_stale_build=allow_stale_build and force)
        except ConfigError as exc:
            mask_view(public_views, view.id)
            print(f"MASK {view.id}: {exc}", file=sys.stderr)
            if fail_on_selected_invalid and selected is not None and view.id in selected:
                selected_failed = True
            continue

        valid[view.id] = (view, revision)
        target = cache_root / view.id / revision
        if (public_views / view.id).is_symlink() and not is_published_view(
            public_views, view.id, target
        ):
            mask_view(public_views, view.id)

    generator_failed = False
    for view in views:
        if selected is not None and view.id not in selected:
            continue
        valid_view = valid.get(view.id)
        if valid_view is None:
            continue
        _, revision = valid_view
        force = force_all or view.id in forced
        try:
            reconcile_view(
                view,
                revision,
                settings,
                cache_root,
                public_views,
                force=force,
            )
        except ConfigError as exc:
            mask_view(public_views, view.id)
            print(f"MASK {view.id}: {exc}", file=sys.stderr)
            if fail_on_selected_invalid:
                selected_failed = True
            continue
        except subprocess.CalledProcessError as exc:
            generator_failed = True
            mask_view(public_views, view.id)
            print(f"MASK {view.id}: generator exited with status {exc.returncode}", file=sys.stderr)
            continue

    published = [
        (view, revision)
        for view, revision in valid.values()
        if is_published_view(public_views, view.id, cache_root / view.id / revision)
    ]
    write_landing_page(settings.output_root / "public" / "index.html", published)
    print(f"published {len(published)} of {len(views)} configured view(s)")
    return 1 if generator_failed or selected_failed else 0


def validate_view(view: View, *, allow_stale_build: bool) -> str:
    if not view.repository.source.is_dir():
        raise ConfigError(f"source directory is missing: {view.repository.source}")
    if not view.build.is_dir():
        raise ConfigError(f"build directory is missing: {view.build}")
    if not view.compilation_database.is_file():
        raise ConfigError(f"compilation database is missing: {view.compilation_database}")
    validate_compilation_database(view.compilation_database, view.repository.source, view.build)

    source_commit = git_commit(view.repository.source)
    if source_commit:
        if not view.built_commit:
            raise ConfigError("Git view has no built_commit; run register-build after a successful build")
        if source_commit != view.built_commit and not allow_stale_build:
            raise ConfigError(
                f"source/build mismatch (source {source_commit[:12]}, build {view.built_commit[:12]})"
            )
        return source_commit
    return "once"


def reconcile_view(
    view: View,
    revision: str,
    settings: Settings,
    cache_root: Path,
    public_views: Path,
    *,
    force: bool,
) -> None:
    target = cache_root / view.id / revision
    if force or not target.is_dir():
        generate_view(view, revision, target, settings)
        print(f"GENERATE {view.id}@{revision[:12]}")
    else:
        refresh_static_assets(settings.data_dir, target / "data")
        print(f"REUSE {view.id}@{revision[:12]}")
    publish_view(public_views, view.id, target)


def is_published_view(public_views: Path, view_id: str, target: Path) -> bool:
    link = public_views / view_id
    if not link.is_symlink() or not target.is_dir():
        return False
    try:
        return link.resolve() == target.resolve()
    except RuntimeError:
        return False


def refresh_static_assets(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise ConfigError(f"Code Browser data directory is missing: {source}")
    shutil.copytree(source, destination, dirs_exist_ok=True)


def generate_view(view: View, revision: str, target: Path, settings: Settings) -> None:
    for executable in (settings.generator, settings.indexgenerator):
        if not executable.is_file():
            raise ConfigError(f"required executable is missing: {executable}")
    if not settings.data_dir.is_dir():
        raise ConfigError(f"Code Browser data directory is missing: {settings.data_dir}")

    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".tmp-{uuid.uuid4().hex}"
    browser_output = staging / "browser"
    browser_output.mkdir(parents=True)
    project_spec = f"{view.repository.id}:{view.repository.source}:{revision}"
    if ":" in str(view.repository.source):
        raise ConfigError("KDAB project paths cannot contain ':'")

    try:
        subprocess.run(
            [
                str(settings.generator),
                "-b",
                str(view.compilation_database.parent),
                "-a",
                "-o",
                str(browser_output),
                "-p",
                project_spec,
            ],
            check=True,
        )
        subprocess.run(
            [str(settings.indexgenerator), str(browser_output), "-p", project_spec],
            check=True,
        )
        shutil.copytree(settings.data_dir, staging / "data")
        write_view_redirect(staging / "index.html", view.repository.id)
        (staging / ".codebrowser.json").write_text(
            json.dumps(
                {
                    "view": view.id,
                    "repository": view.repository.id,
                    "revision": revision,
                    "source": str(view.repository.source),
                    "build": str(view.build),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        previous = None
        if target.exists():
            previous = parent / f".old-{uuid.uuid4().hex}"
            os.replace(target, previous)
        os.replace(staging, target)
        if previous:
            shutil.rmtree(previous)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def write_view_redirect(path: Path, repository_id: str) -> None:
    destination = f"browser/{repository_id}/"
    path.write_text(
        "<!doctype html>\n"
        '<meta charset="utf-8">\n'
        f'<meta http-equiv="refresh" content="0; url={html.escape(destination, quote=True)}">\n'
        f'<a href="{html.escape(destination, quote=True)}">Open {html.escape(repository_id)}</a>\n',
        encoding="utf-8",
    )


def publish_view(public_views: Path, view_id: str, target: Path) -> None:
    link = public_views / view_id
    temporary = public_views / f".{view_id}.{uuid.uuid4().hex}"
    relative_target = os.path.relpath(target, public_views)
    os.symlink(relative_target, temporary)
    os.replace(temporary, link)


def mask_view(public_views: Path, view_id: str) -> None:
    path = public_views / view_id
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        raise ConfigError(f"refusing to mask non-symlink public path: {path}")


def mask_unconfigured(public_views: Path, configured: set[str]) -> None:
    for path in public_views.iterdir():
        if path.name not in configured:
            if path.is_symlink():
                path.unlink()
            else:
                raise ConfigError(f"unexpected non-symlink in public views: {path}")


def write_landing_page(path: Path, published: list[tuple[View, str]]) -> None:
    items = "\n".join(
        f'<li><a href="views/{html.escape(view.id, quote=True)}/">{html.escape(view.id)}</a> '
        f'<small>{html.escape(revision[:12])}</small></li>'
        for view, revision in sorted(published, key=lambda item: item[0].id)
    )
    if not items:
        items = "<li>No build-consistent views are currently available.</li>"
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Code Browser</title>
  <style>
    body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 52rem; margin: 4rem auto; padding: 0 1rem; }}
    li {{ margin: .5rem 0; }}
    small {{ color: #666; font-family: ui-monospace, monospace; }}
  </style>
</head>
<body>
  <h1>Code Browser</h1>
  <ul>
    {items}
  </ul>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    temporary.write_text(document, encoding="utf-8")
    os.replace(temporary, path)


def command_list(settings: Settings) -> int:
    views = load_views(settings)
    public_views = settings.output_root / "public" / "views"
    cache_root = settings.output_root / "cache"
    rows: list[tuple[str, str, str, str, str]] = []

    for view in views:
        revision_label = view.built_commit[:12] if view.built_commit else "non-git"
        if (
            not view.repository.source.is_dir()
            or not view.build.is_dir()
            or not view.compilation_database.is_file()
        ):
            state = "missing-input"
        else:
            source_commit = git_commit(view.repository.source)
            if source_commit and source_commit != view.built_commit:
                state = "source-build-mismatch"
            else:
                revision = source_commit or "once"
                target = cache_root / view.id / revision
                if is_published_view(public_views, view.id, target):
                    state = "published"
                elif target.is_dir():
                    state = "cached"
                else:
                    state = "not-generated"
        rows.append(
            (view.id, view.repository.id, revision_label, state, str(view.build))
        )

    headers = ("VIEW", "REPOSITORY", "REVISION", "STATE", "BUILD")
    widths = [
        max([len(headers[index]), *(len(row[index]) for row in rows)])
        for index in range(len(headers) - 1)
    ]

    def format_row(row: tuple[str, str, str, str, str]) -> str:
        return "  ".join(
            [row[index].ljust(widths[index]) for index in range(len(widths))]
            + [row[-1]]
        )

    print(format_row(headers))
    for row in rows:
        print(format_row(row))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser(
        "register-build", help="record a successfully completed build as a per-repository YAML view"
    )
    register.add_argument("--source", required=True, help="absolute repository source path")
    register.add_argument("--build", help="absolute build path; defaults to --source")
    register.add_argument("--compilation-database", help="path relative to the build directory")
    register.add_argument("--repository", help="repository id; defaults to the source directory name")
    register.add_argument("--view", help="view id; defaults to the build directory name")

    subparsers.add_parser("list", help="list configured views and their publication state")

    generate = subparsers.add_parser(
        "generate", help="reconcile all views or only the named views"
    )
    generate.add_argument("views", nargs="*", metavar="VIEW", help="view to reconcile; defaults to all")
    generate.add_argument(
        "--force", action="store_true", help="regenerate instead of reusing the current revision cache"
    )
    generate.add_argument(
        "--allow-stale-build",
        action="store_true",
        help="allow forced generation when source HEAD does not match built_commit",
    )

    reconcile = subparsers.add_parser("reconcile", help="publish every build-consistent configured view")
    reconcile.add_argument("--force", action="append", metavar="VIEW", help="regenerate one view")
    reconcile.add_argument("--force-all", action="store_true", help="regenerate every valid view")
    reconcile.add_argument(
        "--allow-stale-build",
        action="store_true",
        help="allow forced generation when source HEAD does not match built_commit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        settings = Settings.from_environment()
        if args.command == "register-build":
            return command_register_build(args, settings)
        if args.command == "list":
            return command_list(settings)
        if args.command == "generate":
            return command_generate(args, settings)
        if args.command == "reconcile":
            return command_reconcile(args, settings)
        raise AssertionError(args.command)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
