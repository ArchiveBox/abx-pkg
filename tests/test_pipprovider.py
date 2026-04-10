import subprocess
import tempfile
from pathlib import Path

import pytest

from abx_pkg import Binary, PipProvider, SemVer
from abx_pkg.exceptions import BinaryInstallError


class TestPipProvider:
    def test_version_falls_back_to_pip_metadata_when_console_script_rejects_flags(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PipProvider(
                install_root=Path(tmpdir) / "venv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = provider.install("saws")

            assert installed is not None
            assert installed.loaded_abspath is not None
            assert installed.loaded_version is not None
            assert provider.INSTALLER_BIN_ABSPATH is not None

            metadata_proc = provider.exec(
                bin_name=provider.INSTALLER_BIN_ABSPATH,
                cmd=["show", "--no-input", "saws"],
                quiet=True,
                timeout=provider.version_timeout,
            )
            assert metadata_proc.returncode == 0, (
                metadata_proc.stderr or metadata_proc.stdout
            )
            metadata_version = next(
                (
                    SemVer.parse(line.split("Version: ", 1)[1])
                    for line in metadata_proc.stdout.splitlines()
                    if line.startswith("Version: ")
                ),
                None,
            )
            assert metadata_version is not None

            failing_version_cmd = subprocess.run(
                [str(installed.loaded_abspath), "--version"],
                capture_output=True,
                text=True,
            )
            assert failing_version_cmd.returncode != 0

            assert installed.loaded_version == metadata_version
            assert (
                provider.get_version(
                    "saws",
                    abspath=installed.loaded_abspath,
                    quiet=True,
                    no_cache=True,
                )
                == metadata_version
            )

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
                install_root=temp_dir_path / "ambient-venv",
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
                install_root=install_root,
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
                install_root=tmp_path / "venv",
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
                install_root=Path(temp_dir) / "venv",
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
                install_root=venv_path,
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
                install_root=venv_path,
                postinstall_scripts=True,
                min_release_age=0,
            ).install("black", min_version=SemVer("24.0.0"))
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("24.0.0"),
            )

            updated = PipProvider(
                install_root=venv_path,
                postinstall_scripts=True,
                min_release_age=0,
            ).update("black", min_version=SemVer("24.0.0"))
            test_machine.assert_shallow_binary_loaded(
                updated,
                expected_version=SemVer("24.0.0"),
            )

    def test_provider_install_no_cache_forces_reinstall_with_new_install_args(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            venv_path = Path(tmpdir) / "venv"
            old_provider = PipProvider(
                install_root=venv_path,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"black": {"install_args": ["black==23.1.0"]}},
            )
            old_installed = old_provider.install("black")
            assert old_installed is not None
            assert old_installed.loaded_version == SemVer("23.1.0")

            provider = PipProvider(
                install_root=venv_path,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"black": {"install_args": ["black==24.4.2"]}},
            )

            loaded = provider.install("black")
            test_machine.assert_shallow_binary_loaded(
                loaded,
                expected_version=SemVer("23.1.0"),
            )

            forced = provider.install("black", no_cache=True)
            test_machine.assert_shallow_binary_loaded(
                forced,
                expected_version=SemVer("24.4.2"),
            )

    def test_provider_defaults_and_binary_overrides_enforce_min_release_age(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = PipProvider(
                install_root=Path(tmpdir) / "strict-venv",
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
                        install_root=Path(tmpdir) / "binary-venv",
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
                install_root=Path(tmpdir) / "strict-venv",
                postinstall_scripts=False,
                min_release_age=0,
            )
            with pytest.raises(Exception):
                strict_provider.install("saws")
            test_machine.assert_provider_missing(strict_provider, "saws")

            direct_override = strict_provider.install(
                "saws",
                postinstall_scripts=True,
            )
            test_machine.assert_shallow_binary_loaded(
                direct_override,
                assert_version_command=False,
            )
            assert strict_provider.uninstall("saws")

            binary = Binary(
                name="saws",
                binproviders=[
                    PipProvider(
                        install_root=Path(tmpdir) / "binary-venv",
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
                name="saws",
                binproviders=[
                    PipProvider(
                        install_root=Path(tmpdir) / "failing-venv",
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
                        install_root=Path(temp_dir) / "venv",
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
                install_root=Path(temp_dir) / "venv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="black")

    def test_provider_action_args_override_provider_defaults(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = PipProvider(
                install_root=Path(temp_dir) / "venv",
                dry_run=True,
                postinstall_scripts=False,
                min_release_age=36500,
            )

            installed = provider.install(
                "black",
                dry_run=False,
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.assert_shallow_binary_loaded(installed)
