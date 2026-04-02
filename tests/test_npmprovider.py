import tempfile
from pathlib import Path

import pytest

from abx_pkg import Binary, NpmProvider, SemVer


class TestNpmProvider:
    def test_install_args_win_for_ignore_scripts_and_min_release_age(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            npm_prefix = Path(temp_dir) / "npm"
            provider = NpmProvider(
                npm_prefix=npm_prefix,
                postinstall_scripts=True,
                min_release_age=36500,
            ).get_provider_with_overrides(
                overrides={
                    "gifsicle": {
                        "install_args": [
                            "gifsicle",
                            "--ignore-scripts",
                            "--min-release-age=0",
                        ],
                    },
                },
            )

            installed = provider.install("gifsicle")

            assert installed is not None
            proc = installed.exec(cmd=("--version",), quiet=True)
            assert proc.returncode != 0

            if (
                provider.INSTALLER_BIN_ABSPATH
                and provider.INSTALLER_BIN_ABSPATH.name == "pnpm"
            ):
                workspace_config = npm_prefix / "pnpm-workspace.yaml"
                if workspace_config.exists():
                    assert "minimumReleaseAge" not in workspace_config.read_text()

    def test_install_root_alias_installs_into_the_requested_prefix(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "npm-root"
            provider = NpmProvider.model_validate(
                {
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            )

            installed = provider.install("zx")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "node_modules" / ".bin"
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_explicit_prefix_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = NpmProvider(
                npm_prefix=temp_dir_path / "ambient-npm",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"zx": {"install_args": ["zx@7.2.3"]}},
            )
            ambient_installed = ambient_provider.install(
                "zx",
                min_version=SemVer("1.0.0"),
            )
            assert ambient_installed is not None

            install_root = temp_dir_path / "npm-root"
            provider = NpmProvider(
                PATH=str(ambient_provider.bin_dir),
                npm_prefix=install_root,
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = provider.install("zx", min_version=SemVer("8.8.0"))

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "node_modules" / ".bin"
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert installed.loaded_version is not None
            assert ambient_installed.loaded_version is not None
            assert installed.loaded_version > ambient_installed.loaded_version

    def test_setup_falls_back_to_no_cache_when_cache_dir_is_not_a_directory(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            cache_file = tmp_path / "npm-cache-file"
            cache_file.write_text("not-a-directory", encoding="utf-8")

            provider = NpmProvider(
                npm_prefix=tmp_path / "npm",
                cache_dir=cache_file,
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = provider.install("zx")
            assert provider.cache_arg == "--no-cache"
            test_machine.assert_shallow_binary_loaded(installed)

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NpmProvider(
                npm_prefix=Path(temp_dir) / "npm",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_lifecycle(provider, bin_name="zx")

    def test_provider_direct_min_version_revalidates_old_install_and_upgrades(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            npm_prefix = Path(tmpdir) / "npm"
            old_provider = NpmProvider(
                npm_prefix=npm_prefix,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"zx": {"install_args": ["zx@7.2.3"]}},
            )
            old_installed = old_provider.install("zx", min_version=SemVer("1.0.0"))
            assert old_installed is not None
            assert old_installed.loaded_version == SemVer("7.2.3")

            upgraded = NpmProvider(
                npm_prefix=npm_prefix,
                postinstall_scripts=True,
                min_release_age=0,
            ).install("zx", min_version=SemVer("8.8.0"))
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("8.8.0"),
            )

            with pytest.raises(Exception):
                NpmProvider(
                    npm_prefix=npm_prefix,
                    postinstall_scripts=True,
                    min_release_age=0,
                ).update("zx", min_version=SemVer("999.0.0"))

    def test_provider_defaults_and_binary_overrides_enforce_min_release_age(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = NpmProvider(
                npm_prefix=Path(tmpdir) / "strict-npm",
                postinstall_scripts=True,
                min_release_age=36500,
            )
            with pytest.raises(Exception):
                strict_provider.install("zx")
            test_machine.assert_provider_missing(strict_provider, "zx")

            direct_override = strict_provider.install("zx", min_release_age=0)
            test_machine.assert_shallow_binary_loaded(direct_override)
            assert strict_provider.uninstall("zx", min_release_age=0)

            binary = Binary(
                name="zx",
                binproviders=[
                    NpmProvider(
                        npm_prefix=Path(tmpdir) / "binary-npm",
                        postinstall_scripts=True,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)

    def test_provider_defaults_and_binary_overrides_enforce_postinstall_scripts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = NpmProvider(
                npm_prefix=Path(tmpdir) / "strict-npm",
                postinstall_scripts=False,
                min_release_age=0,
            )
            strict_installed = strict_provider.install("gifsicle")
            assert strict_installed is not None
            assert strict_installed.loaded_abspath is not None
            strict_proc = strict_installed.exec(cmd=("--version",), quiet=True)
            assert strict_proc.returncode != 0

            direct_override = strict_provider.install(
                "gifsicle",
                postinstall_scripts=True,
            )
            assert direct_override is not None
            proc = direct_override.exec(cmd=("--version",), quiet=True)
            assert proc.returncode == 0, proc.stderr or proc.stdout
            assert strict_provider.uninstall("gifsicle", postinstall_scripts=True)

            binary = Binary(
                name="gifsicle",
                binproviders=[
                    NpmProvider(
                        npm_prefix=Path(tmpdir) / "binary-npm",
                        postinstall_scripts=False,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = binary.install()
            assert installed is not None
            proc = installed.exec(cmd=("--version",), quiet=True)
            assert proc.returncode == 0, proc.stderr or proc.stdout

            failing_binary = Binary(
                name="gifsicle",
                binproviders=[
                    NpmProvider(
                        npm_prefix=Path(tmpdir) / "failing-npm",
                        postinstall_scripts=False,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=0,
            )
            failing_installed = failing_binary.install()
            assert failing_installed is not None
            failing_proc = failing_installed.exec(cmd=("--version",), quiet=True)
            assert failing_proc.returncode != 0

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="zx",
                binproviders=[
                    NpmProvider(
                        npm_prefix=Path(temp_dir) / "npm",
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_zx(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NpmProvider(
                npm_prefix=Path(temp_dir) / "npm",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="zx")
