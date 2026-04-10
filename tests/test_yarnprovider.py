import logging
import tempfile
from pathlib import Path

import pytest

from abx_pkg import Binary, SemVer, YarnProvider
from abx_pkg.exceptions import BinaryInstallError, BinProviderInstallError


class TestYarnProvider:
    @staticmethod
    def _require_yarn_min_release_age_support(provider: YarnProvider) -> None:
        if not provider.supports_min_release_age("install"):
            pytest.skip(
                "yarn on this host does not support npmMinimalAgeGate",
            )

    @staticmethod
    def _require_yarn_postinstall_disable_support(provider: YarnProvider) -> None:
        if not provider.supports_postinstall_disable("install"):
            pytest.skip(
                "yarn on this host does not support disabling postinstall scripts",
            )

    def test_install_root_alias_installs_into_the_requested_prefix(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "yarn-root"
            provider = YarnProvider.model_validate(
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
            # The auto-initialized workspace must exist on disk.
            assert (install_root / "package.json").exists()
            assert (install_root / "node_modules" / "zx" / "package.json").exists()
            # The corepack-trap field must NOT be written, otherwise Yarn 1
            # would refuse to run on the same workspace.
            import json as _json

            pkg = _json.loads((install_root / "package.json").read_text())
            assert "packageManager" not in pkg

    def test_explicit_prefix_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = YarnProvider(
                install_root=temp_dir_path / "ambient-yarn",
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
            assert ambient_installed.loaded_abspath is not None
            assert ambient_installed.loaded_abspath.parent == ambient_provider.bin_dir

            install_root = temp_dir_path / "yarn-root"
            provider = YarnProvider(
                PATH=str(ambient_provider.bin_dir),
                install_root=install_root,
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
            assert installed.loaded_abspath != ambient_installed.loaded_abspath
            assert installed.loaded_version is not None
            assert ambient_installed.loaded_version is not None
            assert installed.loaded_version > ambient_installed.loaded_version

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = YarnProvider(
                install_root=Path(temp_dir) / "yarn",
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed, _ = test_machine.exercise_provider_lifecycle(
                provider,
                bin_name="zx",
            )
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root is not None
            assert installed.loaded_abspath.is_relative_to(provider.install_root)

    def test_provider_direct_min_version_revalidates_old_install_and_upgrades(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            yarn_prefix = Path(tmpdir) / "yarn"
            old_provider = YarnProvider(
                install_root=yarn_prefix,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"zx": {"install_args": ["zx@7.2.3"]}},
            )
            old_installed = old_provider.install("zx", min_version=SemVer("1.0.0"))
            assert old_installed is not None
            assert old_installed.loaded_version == SemVer("7.2.3")

            upgraded = YarnProvider(
                install_root=yarn_prefix,
                postinstall_scripts=True,
                min_release_age=0,
            ).install("zx", min_version=SemVer("8.8.0"))
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("8.8.0"),
            )
            assert upgraded is not None
            assert upgraded.loaded_version is not None
            assert old_installed.loaded_version is not None
            assert upgraded.loaded_version > old_installed.loaded_version

    def test_provider_defaults_and_binary_overrides_enforce_min_release_age(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = YarnProvider(
                install_root=Path(tmpdir) / "strict-yarn",
                postinstall_scripts=True,
                min_release_age=36500,
            )
            self._require_yarn_min_release_age_support(strict_provider)

            with pytest.raises(BinProviderInstallError):
                strict_provider.install("zx")
            test_machine.assert_provider_missing(strict_provider, "zx")

            # The .yarnrc.yml side effect must reflect the strict 36500 days.
            yarnrc = Path(tmpdir) / "strict-yarn" / ".yarnrc.yml"
            assert yarnrc.exists()
            assert "npmMinimalAgeGate: 36500d" in yarnrc.read_text()

            direct_override = strict_provider.install("zx", min_release_age=0)
            test_machine.assert_shallow_binary_loaded(direct_override)
            assert strict_provider.uninstall("zx", min_release_age=0)

            # After the override, the .yarnrc.yml entry must have been
            # rewritten away (no longer enforces the strict gate).
            assert "npmMinimalAgeGate" not in yarnrc.read_text()

            binary = Binary(
                name="zx",
                binproviders=[
                    YarnProvider(
                        install_root=Path(tmpdir) / "binary-yarn",
                        postinstall_scripts=True,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)

    def test_min_release_age_pins_to_older_version_when_strict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = YarnProvider(
                install_root=Path(tmpdir) / "yarn",
                postinstall_scripts=True,
                min_release_age=365,
            )
            self._require_yarn_min_release_age_support(strict_provider)
            installed = strict_provider.install("zx")
            assert installed is not None
            assert installed.loaded_version is not None
            ceiling = SemVer.parse("8.8.0")
            assert ceiling is not None
            # zx 8.8.x was published too recently to clear a 365-day gate.
            assert installed.loaded_version < ceiling
            # Side effect: the .yarnrc.yml records the gate.
            yarnrc = Path(tmpdir) / "yarn" / ".yarnrc.yml"
            assert yarnrc.exists()
            assert "npmMinimalAgeGate: 365d" in yarnrc.read_text()

    def test_provider_defaults_and_binary_overrides_enforce_postinstall_scripts(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = YarnProvider(
                install_root=Path(tmpdir) / "strict-yarn",
                postinstall_scripts=False,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"optipng": {"install_args": ["optipng-bin"]}},
            )
            self._require_yarn_postinstall_disable_support(strict_provider)

            strict_installed = strict_provider.install("optipng")
            assert strict_installed is not None
            assert strict_installed.loaded_abspath is not None
            strict_proc = strict_installed.exec(cmd=("--version",), quiet=True)
            assert strict_proc.returncode != 0, (
                "strict optipng install with postinstall_scripts=False "
                "should have left the binary broken (no vendor download)"
            )
            yarnrc = Path(tmpdir) / "strict-yarn" / ".yarnrc.yml"
            assert "enableScripts: false" in yarnrc.read_text()

            # Use a fresh prefix for the override case so we don't reuse the
            # cached package from the previous --mode skip-build run.
            override_provider = YarnProvider(
                install_root=Path(tmpdir) / "override-yarn",
                postinstall_scripts=False,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"optipng": {"install_args": ["optipng-bin"]}},
            )
            direct_override = override_provider.install(
                "optipng",
                postinstall_scripts=True,
            )
            assert direct_override is not None
            assert direct_override.loaded_abspath is not None
            override_proc = direct_override.exec(cmd=("--version",), quiet=True)
            assert override_proc.returncode == 0, (
                f"postinstall_scripts=True override should produce a working "
                f"binary, but exec returned {override_proc.returncode}: "
                f"stdout={override_proc.stdout!r} stderr={override_proc.stderr!r}"
            )

            binary = Binary(
                name="optipng",
                binproviders=[
                    YarnProvider(
                        install_root=Path(tmpdir) / "binary-yarn",
                        postinstall_scripts=False,
                        min_release_age=0,
                    ).get_provider_with_overrides(
                        overrides={"optipng": {"install_args": ["optipng-bin"]}},
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = binary.install()
            assert installed is not None
            assert installed.loaded_abspath is not None
            installed_proc = installed.exec(cmd=("--version",), quiet=True)
            assert installed_proc.returncode == 0

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="zx",
                binproviders=[
                    YarnProvider(
                        install_root=Path(temp_dir) / "yarn",
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
            provider = YarnProvider(
                install_root=Path(temp_dir) / "yarn",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="zx")
            modules_dir = Path(temp_dir) / "yarn" / "node_modules"
            if modules_dir.exists():
                assert not (modules_dir / "zx").exists()

    def test_workspace_setup_writes_node_modules_linker(self):
        # Yarn 4 defaults to PnP. The provider must write a .yarnrc.yml that
        # forces ``nodeLinker: node-modules`` so binaries land in
        # ``<workspace>/node_modules/.bin``.
        with tempfile.TemporaryDirectory() as tmpdir:
            yarn_prefix = Path(tmpdir) / "yarn"
            provider = YarnProvider(
                install_root=yarn_prefix,
                postinstall_scripts=True,
                min_release_age=0,
            )
            self._require_yarn_postinstall_disable_support(provider)
            provider.setup()
            yarnrc = yarn_prefix / ".yarnrc.yml"
            assert yarnrc.exists()
            content = yarnrc.read_text()
            assert "nodeLinker: node-modules" in content
            # The auto-init must NOT write a packageManager field, since
            # Yarn 1.22 corepack would refuse to run.
            import json as _json

            pkg = _json.loads((yarn_prefix / "package.json").read_text())
            assert "packageManager" not in pkg

    def test_supports_methods_do_not_emit_unsupported_warnings(self, caplog):
        with tempfile.TemporaryDirectory() as tmpdir:
            with caplog.at_level(logging.WARNING, logger="abx_pkg.binprovider"):
                provider = YarnProvider(
                    install_root=Path(tmpdir) / "yarn",
                    postinstall_scripts=False,
                    min_release_age=0,
                )
                installed = provider.install("zx")
                assert installed is not None
            if provider.supports_postinstall_disable("install"):
                assert "ignoring unsupported postinstall_scripts" not in caplog.text
            else:
                assert "ignoring unsupported postinstall_scripts=False" in caplog.text
            assert "ignoring unsupported min_release_age" not in caplog.text

    def test_binary_install_failure_propagates_as_BinaryInstallError(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            failing_binary = Binary(
                name="zx",
                binproviders=[
                    YarnProvider(
                        install_root=Path(tmpdir) / "yarn",
                        postinstall_scripts=True,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=36500,
            )
            failing_provider = failing_binary.binproviders[0]
            assert isinstance(failing_provider, YarnProvider)
            self._require_yarn_min_release_age_support(failing_provider)
            with pytest.raises(BinaryInstallError):
                failing_binary.install()
