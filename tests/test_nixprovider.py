import tempfile
from pathlib import Path
import logging

import pytest

from abx_pkg import Binary, NixProvider, SemVer


class TestNixProvider:
    def test_install_root_alias_installs_into_the_requested_profile(self, test_machine):
        assert NixProvider().INSTALLER_BIN_ABSPATH, "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "nix-profile"
            provider = NixProvider.model_validate(
                {
                    "install_root": install_root,
                    "nix_state_dir": Path(temp_dir) / "nix-state",
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            )

            installed = provider.install("jq")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        assert NixProvider().INSTALLER_BIN_ABSPATH, "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NixProvider(
                nix_profile=Path(temp_dir) / "nix-profile",
                nix_state_dir=Path(temp_dir) / "nix-state",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_lifecycle(provider, bin_name="jq")

    def test_provider_direct_min_version_revalidates_final_installed_package(
        self,
        test_machine,
    ):
        assert NixProvider().INSTALLER_BIN_ABSPATH, "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NixProvider(
                nix_profile=Path(temp_dir) / "nix-profile",
                nix_state_dir=Path(temp_dir) / "nix-state",
                postinstall_scripts=True,
                min_release_age=0,
            )
            with pytest.raises(ValueError):
                provider.install("jq", min_version=SemVer("999.0.0"))

            loaded = provider.load("jq", quiet=True, nocache=True)
            test_machine.assert_shallow_binary_loaded(loaded)
            assert loaded is not None
            assert loaded.loaded_version is not None
            required_version = SemVer.parse("999.0.0")
            assert required_version is not None
            assert loaded.loaded_version < required_version

    def test_nix_profile_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        assert NixProvider().INSTALLER_BIN_ABSPATH, "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = NixProvider(
                nix_profile=temp_dir_path / "ambient-profile",
                nix_state_dir=temp_dir_path / "ambient-state",
                postinstall_scripts=True,
                min_release_age=0,
            )
            ambient_installed = ambient_provider.install("jq")
            assert ambient_installed is not None

            nix_profile = temp_dir_path / "nix-profile"
            provider = NixProvider(
                PATH=f"{ambient_provider.bin_dir}:{NixProvider().PATH}",
                nix_profile=nix_profile,
                nix_state_dir=temp_dir_path / "nix-state",
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = provider.install("jq")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == nix_profile
            assert provider.bin_dir == nix_profile / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert ambient_installed.loaded_abspath is not None
            assert ambient_installed.loaded_abspath.parent == ambient_provider.bin_dir

    def test_uninstall_preserves_other_profile_entries(self, test_machine):
        assert NixProvider().INSTALLER_BIN_ABSPATH, "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NixProvider(
                nix_profile=Path(temp_dir) / "nix-profile",
                nix_state_dir=Path(temp_dir) / "nix-state",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"jq": {"install_args": ["nixpkgs#jq", "nixpkgs#hello"]}},
            )

            installed = provider.install("jq")
            test_machine.assert_shallow_binary_loaded(installed)
            assert provider.load("hello", quiet=True, nocache=True) is not None

            assert provider.uninstall("jq")
            assert provider.load("jq", quiet=True, nocache=True) is None
            assert provider.load("hello", quiet=True, nocache=True) is not None

    def test_unsupported_security_controls_warn_and_continue(
        self,
        test_machine,
        caplog,
    ):
        assert NixProvider().INSTALLER_BIN_ABSPATH, "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            with caplog.at_level(logging.WARNING, logger="abx_pkg.binprovider"):
                installed = NixProvider(
                    nix_profile=Path(temp_dir) / "bad-profile",
                    nix_state_dir=Path(temp_dir) / "bad-state",
                    postinstall_scripts=False,
                    min_release_age=1,
                ).install("jq")
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text

            caplog.clear()
            binary = Binary(
                name="jq",
                binproviders=[
                    NixProvider(
                        nix_profile=Path(temp_dir) / "ok-profile",
                        nix_state_dir=Path(temp_dir) / "ok-state",
                        postinstall_scripts=False,
                        min_release_age=1,
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=1,
            )
            with caplog.at_level(logging.WARNING, logger="abx_pkg.binprovider"):
                installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        assert NixProvider().INSTALLER_BIN_ABSPATH, "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="jq",
                binproviders=[
                    NixProvider(
                        nix_profile=Path(temp_dir) / "nix-profile",
                        nix_state_dir=Path(temp_dir) / "nix-state",
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_jq(self, test_machine):
        assert NixProvider().INSTALLER_BIN_ABSPATH, "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NixProvider(
                nix_profile=Path(temp_dir) / "nix-profile",
                nix_state_dir=Path(temp_dir) / "nix-state",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="jq")
