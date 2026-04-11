import subprocess
import tempfile
from pathlib import Path
import sys

import pytest

from abx_pkg import (
    EnvProvider,
    NpmProvider,
    PipProvider,
    PnpmProvider,
    SemVer,
    UvProvider,
    YarnProvider,
)


class TestBinProvider:
    @pytest.mark.parametrize(
        ("provider_cls", "installer_bin"),
        (
            (PipProvider, "pip"),
            (NpmProvider, "npm"),
            (PnpmProvider, "pnpm"),
            (UvProvider, "uv"),
            (YarnProvider, "yarn"),
        ),
    )
    def test_installer_binary_abspath_resolves_without_recursing(
        self,
        test_machine,
        provider_cls,
        installer_bin,
    ):
        test_machine.require_tool(installer_bin)
        provider = provider_cls(postinstall_scripts=True, min_release_age=0)

        abspath = provider.get_abspath(installer_bin, quiet=True, no_cache=True)
        installer = provider.INSTALLER_BINARY(no_cache=True)

        assert abspath is not None
        assert installer.loaded_abspath is not None
        assert abspath == installer.loaded_abspath

    def test_base_public_getters_resolve_real_host_python(self, test_machine):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=0)

        assert provider.get_install_args("python") == ("python",)
        assert provider.get_packages("python") == ("python",)
        loaded_python = provider.load("python")
        assert loaded_python is not None
        assert provider.get_abspath("python") == loaded_python.loaded_abspath
        assert provider.get_version("python") == SemVer.parse(
            "{}.{}.{}".format(*sys.version_info[:3]),
        )
        assert provider.get_sha256("python") == loaded_python.loaded_sha256

        loaded_or_installed = provider.install(
            "python",
            min_version=SemVer("3.0.0"),
        )
        test_machine.assert_shallow_binary_loaded(loaded_or_installed)

    def test_get_provider_with_overrides_changes_real_install_behavior(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_provider = PipProvider(
                install_root=Path(tmpdir) / "venv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            overridden = base_provider.get_provider_with_overrides(
                overrides={"black": {"install_args": ["black==23.1.0"]}},
            )

            assert base_provider.get_install_args("black") == ("black",)
            assert overridden.get_install_args("black") == ("black==23.1.0",)

            installed = overridden.install("black")
            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_version == SemVer("23.1.0")

    def test_exec_uses_provider_PATH_for_nested_subprocesses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PipProvider(
                install_root=Path(tmpdir) / "venv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = provider.install("black")
            assert installed is not None

            proc = provider.exec(
                sys.executable,
                cmd=[
                    "-c",
                    "import subprocess, sys; proc = subprocess.run(['black', '--version'], capture_output=True, text=True); sys.stdout.write((proc.stdout or proc.stderr).strip()); sys.exit(proc.returncode)",
                ],
                quiet=True,
            )

            assert proc.returncode == 0, proc.stderr or proc.stdout
            assert installed.loaded_version is not None
            assert str(installed.loaded_version) in proc.stdout

    def test_exec_prefers_provider_PATH_over_explicit_env_PATH(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            ambient_provider = PipProvider(
                install_root=tmpdir_path / "ambient-venv",
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

            provider = PipProvider(
                install_root=tmpdir_path / "provider-venv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = provider.install("black", min_version=SemVer("24.0.0"))
            assert installed is not None
            proc = provider.exec(
                sys.executable,
                cmd=[
                    "-c",
                    "import subprocess, sys; proc = subprocess.run(['black', '--version'], capture_output=True, text=True); sys.stdout.write((proc.stdout or proc.stderr).strip()); sys.exit(proc.returncode)",
                ],
                env={"PATH": str(ambient_provider.bin_dir)},
                quiet=True,
            )

            assert proc.returncode == 0, proc.stderr or proc.stdout
            assert ambient_installed.loaded_version is not None
            assert installed.loaded_version is not None
            assert str(installed.loaded_version) in proc.stdout
            assert str(ambient_installed.loaded_version) not in proc.stdout

    def test_exec_timeout_is_enforced_for_real_commands(self):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=0)

        with pytest.raises(subprocess.TimeoutExpired):
            provider.exec(
                sys.executable,
                cmd=["-c", "import time; time.sleep(5)"],
                timeout=2,
                quiet=True,
            )
