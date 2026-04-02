import tempfile
from pathlib import Path

import pytest

from abx_pkg import Binary, EnvProvider, NpmProvider, PipProvider, SemVer
from abx_pkg.exceptions import BinaryInstallError, BinaryLoadError


class TestSecurityControls:
    def test_binary_load_enforces_final_min_version(self):
        binary = Binary(
            name="python",
            binproviders=[EnvProvider(postinstall_scripts=True, min_release_age=0)],
            min_version=SemVer("999.0.0"),
            postinstall_scripts=True,
            min_release_age=0,
        )

        with pytest.raises(BinaryLoadError):
            binary.load()

    def test_pip_provider_default_security_settings_are_overridden_by_binary(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PipProvider(
                pip_venv=Path(tmpdir) / "venv",
                postinstall_scripts=False,
                min_release_age=36500,
            )
            binary = Binary(
                name="slimit",
                binproviders=[provider],
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )

    def test_npm_provider_default_security_settings_are_overridden_by_binary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = NpmProvider(
                npm_prefix=Path(tmpdir) / "npm",
                postinstall_scripts=False,
                min_release_age=36500,
            )
            binary = Binary(
                name="gifsicle",
                binproviders=[provider],
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = binary.install()
            proc = installed.exec(cmd=("--version",), quiet=True)
            assert proc.returncode == 0, proc.stderr or proc.stdout

    def test_pip_provider_default_security_settings_fail_closed_without_override(self):
        with pytest.raises(BinaryInstallError):
            Binary(
                name="slimit",
                binproviders=[
                    PipProvider(
                        postinstall_scripts=False,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=0,
            ).install()
