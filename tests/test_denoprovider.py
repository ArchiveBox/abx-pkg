import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from abx_pkg import Binary, DenoProvider, SemVer


def _deno_supports_age_gate() -> bool:
    deno = shutil.which("deno")
    if not deno:
        return False
    try:
        proc = subprocess.run(
            [deno, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    threshold = SemVer.parse("2.5.0")
    if threshold is None:
        return False
    for token in (proc.stdout or proc.stderr).split():
        version = SemVer.parse(token)
        if version is not None:
            return version >= threshold
    return False


requires_deno = pytest.mark.skipif(
    shutil.which("deno") is None,
    reason="deno is not installed on this host",
)


@requires_deno
class TestDenoProvider:
    def test_install_root_alias_installs_into_the_requested_prefix(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "deno-root"
            provider = DenoProvider.model_validate(
                {
                    "install_root": install_root,
                    "deno_dir": Path(temp_dir) / "deno-cache",
                    "postinstall_scripts": False,
                    "min_release_age": 0,
                },
            )

            installed = provider.install("cowsay")

            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = DenoProvider(
                deno_root=Path(temp_dir) / "deno",
                deno_dir=Path(temp_dir) / "deno-cache",
                postinstall_scripts=False,
                min_release_age=0,
            )
            test_machine.exercise_provider_lifecycle(
                provider,
                bin_name="cowsay",
                assert_version_command=False,
            )

    def test_provider_defaults_and_binary_overrides_enforce_min_release_age(
        self,
        test_machine,
        caplog,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = DenoProvider(
                deno_root=Path(tmpdir) / "strict-deno",
                deno_dir=Path(tmpdir) / "strict-cache",
                postinstall_scripts=False,
                min_release_age=36500,
            )
            if strict_provider.supports_min_release_age("install"):
                with pytest.raises(Exception):
                    strict_provider.install("cowsay")
                test_machine.assert_provider_missing(strict_provider, "cowsay")
            else:
                direct_default = strict_provider.install("cowsay")
                test_machine.assert_shallow_binary_loaded(
                    direct_default,
                    assert_version_command=False,
                )
                assert (
                    "ignoring unsupported min_release_age=36500.0 for provider deno"
                    in caplog.text
                )
                assert strict_provider.uninstall("cowsay")

            direct_override = strict_provider.install(
                "cowsay",
                min_release_age=0,
            )
            test_machine.assert_shallow_binary_loaded(
                direct_override,
                assert_version_command=False,
            )
            assert strict_provider.uninstall("cowsay", min_release_age=0)

            binary = Binary(
                name="cowsay",
                binproviders=[
                    DenoProvider(
                        deno_root=Path(tmpdir) / "binary-deno",
                        deno_dir=Path(tmpdir) / "binary-cache",
                        postinstall_scripts=False,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=0,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )

    @pytest.mark.skipif(
        not _deno_supports_age_gate(),
        reason="deno 2.5+ required for --minimum-dependency-age",
    )
    def test_min_release_age_extreme_value_blocks_install(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = DenoProvider(
                deno_root=Path(tmpdir) / "deno",
                deno_dir=Path(tmpdir) / "cache",
                postinstall_scripts=False,
                min_release_age=36500,  # 100 years
            )
            with pytest.raises(Exception):
                strict_provider.install("cowsay")

    def test_postinstall_scripts_default_off_does_not_block_simple_packages(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = DenoProvider(
                deno_root=Path(tmpdir) / "deno",
                deno_dir=Path(tmpdir) / "cache",
                postinstall_scripts=False,
                min_release_age=0,
            )
            installed = provider.install("cowsay")
            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="cowsay",
                binproviders=[
                    DenoProvider(
                        deno_root=Path(temp_dir) / "deno",
                        deno_dir=Path(temp_dir) / "cache",
                        postinstall_scripts=False,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=0,
            )
            test_machine.exercise_binary_lifecycle(
                binary,
                assert_version_command=False,
            )

    def test_provider_dry_run_does_not_install_cowsay(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = DenoProvider(
                deno_root=Path(temp_dir) / "deno",
                deno_dir=Path(temp_dir) / "cache",
                postinstall_scripts=False,
                min_release_age=0,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="cowsay")
