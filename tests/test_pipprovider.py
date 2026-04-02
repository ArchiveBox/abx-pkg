import tempfile
from pathlib import Path

import pytest

from abx_pkg import Binary, PipProvider, SemVer
from abx_pkg.exceptions import BinaryInstallError


class TestPipProvider:
    def test_explicit_exclude_newer_flag_overrides_strict_provider_default(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            venv_path = Path(tmpdir) / "venv"
            bootstrap_provider = PipProvider(
                pip_venv=venv_path,
                postinstall_scripts=True,
                min_release_age=0,
            )
            bootstrap_provider.setup(
                postinstall_scripts=True,
                min_release_age=0,
                min_version=None,
            )

            provider = PipProvider(
                pip_venv=venv_path,
                postinstall_scripts=True,
                min_release_age=36500,
            ).get_provider_with_overrides(
                overrides={
                    "black": {
                        "install_args": [
                            "black",
                            "--exclude-newer=2100-01-01T00:00:00Z",
                        ],
                    },
                },
            )

            installed = provider.install("black")

            test_machine.assert_shallow_binary_loaded(installed)
            assert bootstrap_provider.uninstall("black")

    def test_install_root_alias_installs_into_the_requested_venv(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "pip-root"
            provider = PipProvider.model_validate(
                {
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            )

            installed = provider.install("black")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_explicit_venv_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = PipProvider(
                pip_venv=temp_dir_path / "ambient-venv",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"black": {"install_args": ["black==23.1.0"]}},
            )
            ambient_installed = ambient_provider.install(
                "black",
                min_version=SemVer("1.0.0"),
            )
            assert ambient_installed is not None

            install_root = temp_dir_path / "pip-root"
            provider = PipProvider(
                PATH=str(ambient_provider.bin_dir),
                pip_venv=install_root,
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = provider.install("black", min_version=SemVer("24.0.0"))

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
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
            cache_file = tmp_path / "pip-cache-file"
            cache_file.write_text("not-a-directory", encoding="utf-8")

            provider = PipProvider(
                pip_venv=tmp_path / "venv",
                cache_dir=cache_file,
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = provider.install("black")
            assert provider.cache_arg == "--no-cache-dir"
            test_machine.assert_shallow_binary_loaded(installed)

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = PipProvider(
                pip_venv=Path(temp_dir) / "venv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_lifecycle(provider, bin_name="black")

    def test_provider_direct_min_version_revalidates_old_install_and_upgrades(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            venv_path = Path(tmpdir) / "venv"
            old_provider = PipProvider(
                pip_venv=venv_path,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"black": {"install_args": ["black==23.1.0"]}},
            )
            old_installed = old_provider.install("black", min_version=SemVer("1.0.0"))
            assert old_installed is not None
            assert old_installed.loaded_version is not None
            required_version = SemVer.parse("24.0.0")
            assert required_version is not None
            assert tuple(old_installed.loaded_version) < tuple(required_version)

            upgraded = PipProvider(
                pip_venv=venv_path,
                postinstall_scripts=True,
                min_release_age=0,
            ).load_or_install("black", min_version=SemVer("24.0.0"))
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("24.0.0"),
            )

            updated = PipProvider(
                pip_venv=venv_path,
                postinstall_scripts=True,
                min_release_age=0,
            ).update("black", min_version=SemVer("24.0.0"))
            test_machine.assert_shallow_binary_loaded(
                updated,
                expected_version=SemVer("24.0.0"),
            )

    def test_provider_defaults_and_binary_overrides_enforce_min_release_age(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = PipProvider(
                pip_venv=Path(tmpdir) / "strict-venv",
                postinstall_scripts=True,
                min_release_age=36500,
            )
            with pytest.raises(Exception):
                strict_provider.install("black")
            test_machine.assert_provider_missing(strict_provider, "black")

            direct_override = strict_provider.install("black", min_release_age=0)
            test_machine.assert_shallow_binary_loaded(direct_override)
            assert strict_provider.uninstall("black")

            binary = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        pip_venv=Path(tmpdir) / "binary-venv",
                        postinstall_scripts=True,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)

    def test_provider_defaults_and_binary_overrides_enforce_postinstall_scripts(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = PipProvider(
                pip_venv=Path(tmpdir) / "strict-venv",
                postinstall_scripts=False,
                min_release_age=0,
            )
            with pytest.raises(Exception):
                strict_provider.install("slimit")
            test_machine.assert_provider_missing(strict_provider, "slimit")

            direct_override = strict_provider.install(
                "slimit",
                postinstall_scripts=True,
            )
            test_machine.assert_shallow_binary_loaded(
                direct_override,
                assert_version_command=False,
            )
            assert strict_provider.uninstall("slimit")

            binary = Binary(
                name="slimit",
                binproviders=[
                    PipProvider(
                        pip_venv=Path(tmpdir) / "binary-venv",
                        postinstall_scripts=False,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )

            failing_binary = Binary(
                name="slimit",
                binproviders=[
                    PipProvider(
                        pip_venv=Path(tmpdir) / "failing-venv",
                        postinstall_scripts=False,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=0,
            )
            with pytest.raises(BinaryInstallError):
                failing_binary.install()

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        pip_venv=Path(temp_dir) / "venv",
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_black(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = PipProvider(
                pip_venv=Path(temp_dir) / "venv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="black")
