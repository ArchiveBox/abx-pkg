import tempfile
from pathlib import Path

import pytest

from abx_pkg import Binary, BinProvider, EnvProvider, PipProvider, SemVer
from abx_pkg.exceptions import (
    BinaryInstallError,
    BinaryLoadError,
    BinaryLoadOrInstallError,
    BinaryUninstallError,
    BinaryUpdateError,
)


class TestBinary:
    def test_short_aliases_match_loaded_field_names(self):
        binary = Binary(
            name="python",
            binproviders=[
                EnvProvider(postinstall_scripts=True, min_release_age=0),
            ],
        ).load(nocache=True)

        assert binary.binproviders
        assert binary.binprovider == binary.loaded_binprovider
        assert binary.abspath == binary.loaded_abspath
        assert binary.abspaths == binary.loaded_abspaths
        assert binary.version == binary.loaded_version
        assert binary.sha256 == binary.loaded_sha256

    def test_get_binprovider_applies_overrides_and_provider_filtering(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            pip_provider = PipProvider(
                pip_venv=Path(tmpdir) / "venv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            binary = Binary(
                name="black",
                binproviders=[EnvProvider(), pip_provider],
                overrides={"pip": {"install_args": ["black"]}},
                postinstall_scripts=True,
                min_release_age=0,
            )

            overridden_provider = binary.get_binprovider("pip")
            assert overridden_provider.get_install_args("black") == ("black",)
            with pytest.raises(KeyError):
                binary.get_binprovider("brew")

            installed = binary.install()
            assert installed.loaded_binprovider is not None
            assert installed.loaded_binprovider.name == "pip"

            with pytest.raises(BinaryLoadError):
                test_machine.unloaded_binary(binary).load(
                    binproviders=["env"],
                    nocache=True,
                )
            loaded = test_machine.unloaded_binary(binary).load(
                binproviders=["pip"],
                nocache=True,
            )
            test_machine.assert_shallow_binary_loaded(loaded)

    def test_min_version_rejection_paths_raise_public_errors(self):
        binary = Binary(
            name="python",
            binproviders=[
                EnvProvider(postinstall_scripts=True, min_release_age=0),
            ],
            min_version=SemVer("999.0.0"),
            postinstall_scripts=True,
            min_release_age=0,
        )

        with pytest.raises(BinaryLoadError):
            binary.load()
        with pytest.raises(BinaryLoadOrInstallError):
            binary.load_or_install()
        with pytest.raises(BinaryInstallError):
            binary.install()
        with pytest.raises(BinaryUpdateError):
            binary.update()
        with pytest.raises(BinaryUninstallError):
            binary.uninstall()

    def test_load_or_install_and_update_upgrade_real_installed_version(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            venv_path = Path(tmpdir) / "venv"
            old_binary = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        pip_venv=venv_path,
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                overrides={"pip": {"install_args": ["black==23.1.0"]}},
            )
            old_installed = old_binary.install()
            assert old_installed.loaded_version is not None
            required_version = SemVer.parse("24.0.0")
            assert required_version is not None
            assert tuple(old_installed.loaded_version) < tuple(required_version)

            upgraded = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        pip_venv=venv_path,
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                min_version=SemVer("24.0.0"),
            ).load_or_install()
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("24.0.0"),
            )

            updated = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        pip_venv=venv_path,
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                min_version=SemVer("24.0.0"),
            ).update()
            test_machine.assert_shallow_binary_loaded(
                updated,
                expected_version=SemVer("24.0.0"),
            )

            removed = updated.uninstall()
            assert removed.loaded_abspath is None

    def test_empty_binprovider_filter_returns_binary_unchanged(self):
        binary = Binary(
            name="python",
            binproviders=[
                EnvProvider(postinstall_scripts=True, min_release_age=0),
            ],
            postinstall_scripts=True,
            min_release_age=0,
        )

        assert binary.install(binproviders=[]) == binary
        assert binary.load(binproviders=[]) == binary
        assert binary.load_or_install(binproviders=[]) == binary
        assert binary.update(binproviders=[]) == binary
        assert binary.uninstall(binproviders=[]) == binary

    def test_binary_params_override_provider_defaults_and_binary_overrides_win(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PipProvider(
                pip_venv=Path(tmpdir) / "venv",
                postinstall_scripts=False,
                min_release_age=36500,
            ).get_provider_with_overrides(
                overrides={"black": {"install_args": ["black"]}},
            )
            binary = Binary(
                name="black",
                binproviders=[provider],
                postinstall_scripts=True,
                min_release_age=0,
                overrides={"pip": {"install_args": ["black==23.1.0"]}},
            )

            resolved_provider = binary.get_binprovider("pip")
            assert resolved_provider.get_install_args("black") == ("black==23.1.0",)

            installed = binary.install()
            assert installed.loaded_version == SemVer("23.1.0")

            upgraded = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        pip_venv=Path(tmpdir) / "venv",
                        postinstall_scripts=False,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                min_version=SemVer("24.0.0"),
            ).load_or_install()
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("24.0.0"),
            )

    def test_binary_install_works_with_provider_install_root_alias(self, test_machine):
        with tempfile.TemporaryDirectory() as tmpdir:
            install_root = Path(tmpdir) / "pip-root"
            providers: list[BinProvider] = [
                PipProvider.model_validate(
                    {
                        "install_root": install_root,
                        "postinstall_scripts": True,
                        "min_release_age": 0,
                    },
                ),
            ]
            binary = Binary(
                name="black",
                binproviders=providers,
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = binary.install()

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed.loaded_abspath is not None
            provider = binary.get_binprovider("pip")
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_binary_dry_run_passes_through_to_provider_without_installing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PipProvider(
                pip_venv=Path(tmpdir) / "venv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            binary = Binary(
                name="black",
                binproviders=[provider],
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = binary.install(dry_run=True)
            assert installed.loaded_version == SemVer("999.999.999")
            assert provider.load("black", quiet=True, nocache=True) is None

            loaded_or_installed = binary.load_or_install(dry_run=True)
            assert loaded_or_installed.loaded_version == SemVer("999.999.999")
            assert provider.load("black", quiet=True, nocache=True) is None

            updated = binary.update(dry_run=True)
            assert updated.loaded_version == SemVer("999.999.999")
            assert provider.load("black", quiet=True, nocache=True) is None

            removed = binary.uninstall(dry_run=True)
            assert removed.loaded_abspath is None
            assert provider.load("black", quiet=True, nocache=True) is None

    def test_binary_dry_run_load_or_install_does_not_update_stale_existing_binary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            venv_path = Path(tmpdir) / "venv"
            old_binary = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        pip_venv=venv_path,
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                overrides={"pip": {"install_args": ["black==23.1.0"]}},
            )
            old_installed = old_binary.install()
            assert old_installed.loaded_version == SemVer("23.1.0")

            binary = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        pip_venv=venv_path,
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                min_version=SemVer("24.0.0"),
            )
            dry_loaded_or_installed = binary.load_or_install(dry_run=True)
            assert dry_loaded_or_installed.loaded_version == SemVer("999.999.999")

            loaded_after_dry_run = binary.get_binprovider("pip").load(
                "black",
                quiet=True,
                nocache=True,
            )
            assert loaded_after_dry_run is not None
            assert loaded_after_dry_run.loaded_version == SemVer("23.1.0")

    def test_binary_action_args_override_binary_and_provider_defaults(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PipProvider(
                pip_venv=Path(tmpdir) / "venv",
                dry_run=True,
                postinstall_scripts=False,
                min_release_age=36500,
            )
            binary = Binary(
                name="black",
                binproviders=[provider],
                postinstall_scripts=False,
                min_release_age=36500,
            )

            installed = binary.install(
                dry_run=False,
                postinstall_scripts=True,
                min_release_age=0,
            )

            test_machine.assert_shallow_binary_loaded(installed)
