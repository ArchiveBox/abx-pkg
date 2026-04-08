import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from abx_pkg import Binary, SemVer, YarnProvider


def _yarn_supports_age_gate() -> bool:
    yarn = shutil.which("yarn")
    if not yarn:
        return False
    try:
        proc = subprocess.run(
            [yarn, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    version = SemVer.parse((proc.stdout or proc.stderr).strip())
    threshold = SemVer.parse("4.10.0")
    if version is None or threshold is None:
        return False
    return version >= threshold


requires_yarn = pytest.mark.skipif(
    shutil.which("yarn") is None,
    reason="yarn is not installed on this host",
)


@requires_yarn
class TestYarnProvider:
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

    def test_explicit_prefix_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = YarnProvider(
                yarn_prefix=temp_dir_path / "ambient-yarn",
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

            install_root = temp_dir_path / "yarn-root"
            provider = YarnProvider(
                PATH=str(ambient_provider.bin_dir),
                yarn_prefix=install_root,
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

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = YarnProvider(
                yarn_prefix=Path(temp_dir) / "yarn",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_lifecycle(provider, bin_name="zx")

    def test_provider_direct_min_version_revalidates_old_install_and_upgrades(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            yarn_prefix = Path(tmpdir) / "yarn"
            old_provider = YarnProvider(
                yarn_prefix=yarn_prefix,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"zx": {"install_args": ["zx@7.2.3"]}},
            )
            old_installed = old_provider.install("zx", min_version=SemVer("1.0.0"))
            assert old_installed is not None
            assert old_installed.loaded_version == SemVer("7.2.3")

            upgraded = YarnProvider(
                yarn_prefix=yarn_prefix,
                postinstall_scripts=True,
                min_release_age=0,
            ).install("zx", min_version=SemVer("8.8.0"))
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("8.8.0"),
            )

    def test_provider_defaults_and_binary_overrides_enforce_min_release_age(
        self,
        test_machine,
        caplog,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = YarnProvider(
                yarn_prefix=Path(tmpdir) / "strict-yarn",
                postinstall_scripts=True,
                min_release_age=36500,
            )
            if strict_provider.supports_min_release_age("install"):
                with pytest.raises(Exception):
                    strict_provider.install("zx")
                test_machine.assert_provider_missing(strict_provider, "zx")
            else:
                direct_default = strict_provider.install("zx")
                test_machine.assert_shallow_binary_loaded(direct_default)
                assert (
                    "ignoring unsupported min_release_age=36500.0 for provider yarn"
                    in caplog.text
                )
                assert strict_provider.uninstall("zx")

            direct_override = strict_provider.install("zx", min_release_age=0)
            test_machine.assert_shallow_binary_loaded(direct_override)
            assert strict_provider.uninstall("zx", min_release_age=0)

            binary = Binary(
                name="zx",
                binproviders=[
                    YarnProvider(
                        yarn_prefix=Path(tmpdir) / "binary-yarn",
                        postinstall_scripts=True,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)

    @pytest.mark.skipif(
        not _yarn_supports_age_gate(),
        reason="yarn 4.10+ required for npmMinimalAgeGate",
    )
    def test_min_release_age_pins_to_older_version_when_strict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = YarnProvider(
                yarn_prefix=Path(tmpdir) / "yarn",
                postinstall_scripts=True,
                min_release_age=365,
            )
            installed = strict_provider.install("zx")
            assert installed is not None
            assert installed.loaded_version is not None
            # zx 8.8.5 was released too recently to satisfy 365d
            ceiling = SemVer.parse("8.8.0")
            assert ceiling is not None
            assert installed.loaded_version < ceiling

    def test_provider_defaults_and_binary_overrides_enforce_postinstall_scripts(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = YarnProvider(
                yarn_prefix=Path(tmpdir) / "strict-yarn",
                postinstall_scripts=False,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"optipng": {"install_args": ["optipng-bin"]}},
            )
            strict_installed = strict_provider.install("optipng")
            assert strict_installed is not None
            assert strict_installed.loaded_abspath is not None
            if strict_provider.supports_postinstall_disable("install"):
                # On Yarn 2+, --mode skip-build / enableScripts: false actually
                # blocks the postinstall, so the optipng-bin wrapper has no
                # vendor binary to spawn and exits non-zero.
                strict_proc = strict_installed.exec(cmd=("--version",), quiet=True)
                assert strict_proc.returncode != 0

            direct_override = strict_provider.install(
                "optipng",
                postinstall_scripts=True,
            )
            assert direct_override is not None
            assert direct_override.loaded_abspath is not None

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="zx",
                binproviders=[
                    YarnProvider(
                        yarn_prefix=Path(temp_dir) / "yarn",
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
                yarn_prefix=Path(temp_dir) / "yarn",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="zx")
