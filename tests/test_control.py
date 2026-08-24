from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def load_control() -> ModuleType:
    spec = importlib.util.spec_from_file_location("codebrowser_control", ROOT / "src/codebrowser_control.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


control = load_control()


class ControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "source"
        self.build_root = self.root / "build"
        self.config_root = self.root / "config"
        self.output_root = self.root / "output"
        self.data_dir = self.root / "data"
        for path in (
            self.source_root,
            self.build_root,
            self.config_root,
            self.output_root,
            self.data_dir,
        ):
            path.mkdir()
        (self.data_dir / "codebrowser.css").write_text("body {}", encoding="utf-8")

        self.generator = self.root / "fake-generator"
        self.indexgenerator = self.root / "fake-indexgenerator"
        self.generator.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = -o ]; then shift; out=$1; fi\n"
            "  if [ \"$1\" = -p ]; then shift; project=${1%%:*}; fi\n"
            "  shift\n"
            "done\n"
            "mkdir -p \"$out/$project\"\n"
            "printf '<html>project</html>\\n' > \"$out/$project/index.html\"\n",
            encoding="utf-8",
        )
        self.indexgenerator.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.generator.chmod(0o755)
        self.indexgenerator.chmod(0o755)

        os.environ.update(
            {
                "SOURCE_ROOT": str(self.source_root),
                "BUILD_ROOT": str(self.build_root),
                "CONFIG_ROOT": str(self.config_root),
                "OUTPUT_ROOT": str(self.output_root),
                "CODEBROWSER_GENERATOR": str(self.generator),
                "CODEBROWSER_INDEXGENERATOR": str(self.indexgenerator),
                "CODEBROWSER_DATA_DIR": str(self.data_dir),
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_git_project(self) -> tuple[Path, Path]:
        source = self.source_root / "demo"
        build = self.build_root / "demo-debug"
        source.mkdir()
        build.mkdir()
        (source / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "add", "main.c"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "initial",
            ],
            check=True,
        )
        self.write_compdb(source, build)
        return source, build

    @staticmethod
    def write_compdb(source: Path, build: Path) -> None:
        (build / "compile_commands.json").write_text(
            json.dumps(
                [
                    {
                        "directory": str(build),
                        "file": str(source / "main.c"),
                        "command": f"cc -c {source / 'main.c'}",
                    }
                ]
            ),
            encoding="utf-8",
        )

    def test_git_view_is_published_then_masked_when_source_advances(self) -> None:
        source, build = self.make_git_project()

        self.assertEqual(
            control.main(["register-build", "--source", str(source), "--build", str(build)]),
            0,
        )
        self.assertEqual(control.main(["reconcile"]), 0)

        commit_a = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        public_link = self.output_root / "public/views/demo-debug"
        self.assertTrue(public_link.is_symlink())
        self.assertTrue((self.output_root / f"cache/demo-debug/{commit_a}/browser/demo/index.html").is_file())

        (source / "main.c").write_text("int main(void) { return 1; }\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "main.c"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "second",
            ],
            check=True,
        )

        self.assertEqual(control.main(["reconcile"]), 0)
        self.assertFalse(public_link.exists())
        self.assertFalse(public_link.is_symlink())

        self.assertEqual(
            control.main(["register-build", "--source", str(source), "--build", str(build)]),
            0,
        )
        self.assertEqual(control.main(["reconcile"]), 0)
        self.assertTrue(public_link.is_symlink())

    def test_non_git_view_is_generated_once(self) -> None:
        source = self.source_root / "vendor"
        build = self.build_root / "vendor-build"
        source.mkdir()
        build.mkdir()
        (source / "main.c").write_text("int value;\n", encoding="utf-8")
        self.write_compdb(source, build)

        self.assertEqual(
            control.main(["register-build", "--source", str(source), "--build", str(build)]),
            0,
        )
        self.assertEqual(control.main(["reconcile"]), 0)
        target = self.output_root / "cache/vendor-build/once"
        generated_at = target.stat().st_mtime_ns
        self.assertEqual(control.main(["reconcile"]), 0)
        self.assertEqual(target.stat().st_mtime_ns, generated_at)

    def test_generator_failure_returns_nonzero_and_masks_view(self) -> None:
        source, build = self.make_git_project()
        self.generator.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")

        self.assertEqual(
            control.main(["register-build", "--source", str(source), "--build", str(build)]),
            0,
        )
        self.assertEqual(control.main(["reconcile"]), 1)
        public_link = self.output_root / "public/views/demo-debug"
        self.assertFalse(public_link.exists())
        self.assertFalse(public_link.is_symlink())

    def test_in_tree_build_uses_source_root(self) -> None:
        source = self.source_root / "in-tree"
        source.mkdir()
        (source / "main.c").write_text("int value;\n", encoding="utf-8")
        self.write_compdb(source, source)

        self.assertEqual(control.main(["register-build", "--source", str(source)]), 0)
        document = control.read_yaml(self.config_root / "in-tree/codebrowser.yaml")
        self.assertEqual(document["views"]["in-tree"]["build"], {"root": "source", "path": "."})
        self.assertEqual(control.main(["reconcile"]), 0)
        self.assertTrue((self.output_root / "public/views/in-tree").is_symlink())

    def test_compilation_database_cannot_cross_view_boundary(self) -> None:
        source, build = self.make_git_project()
        outside = self.root / "other.c"
        outside.write_text("int outside;\n", encoding="utf-8")
        (build / "compile_commands.json").write_text(
            json.dumps([{"directory": str(build), "file": str(outside), "command": "cc -c other.c"}]),
            encoding="utf-8",
        )

        self.assertEqual(
            control.main(["register-build", "--source", str(source), "--build", str(build)]),
            2,
        )


if __name__ == "__main__":
    unittest.main()
