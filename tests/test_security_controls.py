import logging
import tempfile
from pathlib import Path

import pytest

from abx_pkg import (
    Binary,
    BashProvider,
    EnvProvider,
    NpmProvider,
    PipProvider,
    SemVer,
)
from abx_pkg.exceptions import BinaryInstallError, BinaryLoadError


class TestSecurityControls:
    def test_env_defaults_only_apply_to_supported_providers(self, monkeypatch):
        monkeypatch.setenv("ABX_PKG_MIN_RELEASE_AGE", "13")
        monkeypatch.setenv("ABX_PKG_POSTINSTALL_SCRIPTS", "true")

        assert PipProvider().min_release_age == 13
        assert PipProvider().postinstall_scripts is True
        assert NpmProvider().min_release_age == 13
        assert NpmProvider().postinstall_scripts is True
        assert EnvProvider().min_release_age is None
        assert EnvProvider().postinstall_scripts is None
        assert BashProvider().min_release_age is None
        assert BashProvider().postinstall_scripts is None

    def test_env_provider_defaults_do_not_fail_closed(self, test_machine):
        installed = EnvProvider().install("python")
        test_machine.assert_shallow_binary_loaded(installed)

    def test_unsupported_provider_security_options_warn_and_continue(
        self,
        caplog,
        test_machine,
    ):
        with caplog.at_level(logging.WARNING, logger="abx_pkg.binprovider"):
            installed = EnvProvider().install(
                "python",
                postinstall_scripts=False,
                min_release_age=7,
            )

        test_machine.assert_shallow_binary_loaded(installed)
        assert "ignoring unsupported min_release_age=7" in caplog.text
        assert "ignoring unsupported postinstall_scripts=False" in caplog.text

    def test_binary_defaults_do_not_break_unsupported_provider(self):
        binary = Binary(name="python", binproviders=[EnvProvider()])
        installed = binary.install()

        assert installed.loaded_binprovider is not None
        assert installed.loaded_abspath is not None
        assert installed.loaded_version is not None

    def test_binary_load_enforces_final_min_version(self):
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
                name="saws",
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
            ).get_provider_with_overrides(
                overrides={"optipng": {"install_args": ["optipng-bin"]}},
            )
            binary = Binary(
                name="optipng",
                binproviders=[provider],
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = binary.install()
            assert installed is not None
            assert installed.loaded_abspath is not None

    def test_pip_provider_default_security_settings_fail_closed_without_override(self):
        with pytest.raises(BinaryInstallError):
            Binary(
                name="saws",
                binproviders=[
                    PipProvider(
                        postinstall_scripts=False,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=0,
            ).install()
