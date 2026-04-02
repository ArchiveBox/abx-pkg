import tempfile
from pathlib import Path

import pytest

from abx_pkg import Binary, GoGetProvider, SemVer
from abx_pkg.exceptions import BinaryInstallError


class TestGoGetProvider:
    def test_default_install_args_fail_closed_for_bare_binary_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = GoGetProvider.model_validate(
                {
                    "install_root": Path(temp_dir) / "go-root",
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            )

            with pytest.raises(ValueError):
                provider.get_install_args("shfmt", quiet=False)

    def test_module_path_name_installs_without_overrides(self, test_machine):
        test_machine.require_tool("go")

        with tempfile.TemporaryDirectory() as temp_dir:
            module_path = "mvdan.cc/sh/v3/cmd/shfmt"
            provider = GoGetProvider.model_validate(
                {
                    "install_root": Path(temp_dir) / "go-root",
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            )

            installed = provider.install(module_path)

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.name == "shfmt"
            assert provider.load(module_path, quiet=True, nocache=True) is not None
            assert provider.uninstall(module_path)
            assert provider.load(module_path, quiet=True, nocache=True) is None

    def test_install_root_and_bin_dir_aliases_install_into_the_requested_paths(
        self,
        test_machine,
    ):
        test_machine.require_tool("go")

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "go-root"
            bin_dir = Path(temp_dir) / "custom-bin"
            provider = GoGetProvider.model_validate(
                {
                    "install_root": install_root,
                    "bin_dir": bin_dir,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )

            installed = provider.install("shfmt")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == bin_dir
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_install_root_without_explicit_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        test_machine.require_tool("go")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = GoGetProvider.model_validate(
                {
                    "install_root": temp_dir_path / "ambient-go",
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@v3.7.0"],
                    },
                },
            )
            ambient_installed = ambient_provider.install(
                "shfmt",
                min_version=SemVer("1.0.0"),
            )
            assert ambient_installed is not None

            install_root = temp_dir_path / "go-root"
            provider = GoGetProvider.model_validate(
                {
                    "PATH": str(ambient_provider.bin_dir),
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )

            installed = provider.install("shfmt", min_version=SemVer("3.8.0"))

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert installed.loaded_version is not None
            assert ambient_installed.loaded_version is not None
            assert installed.loaded_version > ambient_installed.loaded_version

    def test_explicit_go_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        test_machine.require_tool("go")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = GoGetProvider(
                gobin=temp_dir_path / "ambient-go/bin",
                gopath=temp_dir_path / "ambient-go",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@v3.7.0"],
                    },
                },
            )
            ambient_installed = ambient_provider.install(
                "shfmt",
                min_version=SemVer("1.0.0"),
            )
            assert ambient_installed is not None

            gobin = temp_dir_path / "go/bin"
            gopath = temp_dir_path / "go"
            provider = GoGetProvider(
                PATH=str(ambient_provider.bin_dir),
                gobin=gobin,
                gopath=gopath,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )

            installed = provider.install("shfmt", min_version=SemVer("3.8.0"))

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == gopath
            assert provider.bin_dir == gobin
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert installed.loaded_version is not None
            assert ambient_installed.loaded_version is not None
            assert installed.loaded_version > ambient_installed.loaded_version

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("go")

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = GoGetProvider(
                gobin=Path(temp_dir) / "go/bin",
                gopath=Path(temp_dir) / "go",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )
            test_machine.exercise_provider_lifecycle(provider, bin_name="shfmt")

    def test_provider_direct_min_version_revalidates_old_install_and_upgrades(
        self,
        test_machine,
    ):
        test_machine.require_tool("go")

        with tempfile.TemporaryDirectory() as temp_dir:
            gobin = Path(temp_dir) / "go/bin"
            gopath = Path(temp_dir) / "go"
            old_provider = GoGetProvider(
                gobin=gobin,
                gopath=gopath,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@v3.7.0"],
                    },
                },
            )
            old_installed = old_provider.install("shfmt")
            assert old_installed is not None
            assert old_installed.loaded_version == SemVer("3.7.0")

            provider = GoGetProvider(
                gobin=gobin,
                gopath=gopath,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )
            upgraded = provider.load_or_install("shfmt", min_version=SemVer("3.8.0"))
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("3.8.0"),
            )

            updated = provider.update("shfmt", min_version=SemVer("3.8.0"))
            test_machine.assert_shallow_binary_loaded(
                updated,
                expected_version=SemVer("3.8.0"),
            )

    def test_unsupported_security_controls_fail_closed_and_binary_override_wins(
        self,
        test_machine,
    ):
        test_machine.require_tool("go")

        with tempfile.TemporaryDirectory() as temp_dir:
            with pytest.raises(RuntimeError):
                GoGetProvider(
                    gobin=Path(temp_dir) / "bad-go/bin",
                    gopath=Path(temp_dir) / "bad-go",
                    postinstall_scripts=False,
                    min_release_age=0,
                ).get_provider_with_overrides(
                    overrides={
                        "shfmt": {
                            "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                        },
                    },
                ).install("shfmt")

            with pytest.raises(RuntimeError):
                GoGetProvider(
                    gobin=Path(temp_dir) / "bad-age-go/bin",
                    gopath=Path(temp_dir) / "bad-age-go",
                    postinstall_scripts=True,
                    min_release_age=1,
                ).get_provider_with_overrides(
                    overrides={
                        "shfmt": {
                            "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                        },
                    },
                ).install("shfmt")

            binary = Binary(
                name="shfmt",
                binproviders=[
                    GoGetProvider(
                        gobin=Path(temp_dir) / "ok-go/bin",
                        gopath=Path(temp_dir) / "ok-go",
                        postinstall_scripts=False,
                        min_release_age=1,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                overrides={
                    "goget": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)

            failing_binary = Binary(
                name="shfmt",
                binproviders=[
                    GoGetProvider(
                        gobin=Path(temp_dir) / "failing-go/bin",
                        gopath=Path(temp_dir) / "failing-go",
                        postinstall_scripts=False,
                        min_release_age=1,
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=1,
                overrides={
                    "goget": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )
            with pytest.raises(BinaryInstallError):
                failing_binary.install()

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("go")

        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="shfmt",
                binproviders=[
                    GoGetProvider(
                        gobin=Path(temp_dir) / "go/bin",
                        gopath=Path(temp_dir) / "go",
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                overrides={
                    "goget": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )
            test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_shfmt(self, test_machine):
        test_machine.require_tool("go")

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = GoGetProvider(
                gobin=Path(temp_dir) / "go/bin",
                gopath=Path(temp_dir) / "go",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="shfmt")
