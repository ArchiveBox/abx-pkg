import sys
from pathlib import Path

import pytest

from abx_pkg import Binary, EnvProvider, SemVer
from abx_pkg.exceptions import BinaryUninstallError


class TestEnvProvider:
    def test_provider_direct_methods_use_real_host_binaries(self, test_machine):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=0)

        install_args = provider.get_install_args("python")
        assert install_args == ("python",)
        assert provider.get_packages("python") == install_args

        python_bin = provider.load("python")
        test_machine.assert_shallow_binary_loaded(python_bin)
        assert python_bin is not None
        assert python_bin.loaded_abspath == Path(sys.executable).absolute()
        assert python_bin.loaded_respath == Path(sys.executable).resolve()
        assert python_bin.loaded_version == SemVer(
            "{}.{}.{}".format(*sys.version_info[:3]),
        )

        installed = provider.install("python", min_version=SemVer("3.0.0"))
        updated = provider.update("python", min_version=SemVer("3.0.0"))
        loaded_or_installed = provider.load_or_install(
            "python",
            min_version=SemVer("3.0.0"),
        )

        test_machine.assert_shallow_binary_loaded(installed)
        test_machine.assert_shallow_binary_loaded(updated)
        test_machine.assert_shallow_binary_loaded(loaded_or_installed)

        assert provider.uninstall("python") is False
        test_machine.assert_shallow_binary_loaded(provider.load("python"))

    def test_provider_direct_min_version_rejection_keeps_binary_available(
        self,
        test_machine,
    ):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=0)

        with pytest.raises(ValueError):
            provider.install("python", min_version=SemVer("999.0.0"))

        test_machine.assert_shallow_binary_loaded(provider.load("python"))

    def test_binary_direct_methods_use_env_provider(self, test_machine):
        binary = Binary(
            name="python",
            binproviders=[EnvProvider(postinstall_scripts=True, min_release_age=0)],
            min_version=SemVer("3.0.0"),
            postinstall_scripts=True,
            min_release_age=0,
        )

        installed = binary.install()
        loaded = test_machine.unloaded_binary(binary).load_or_install()

        test_machine.assert_shallow_binary_loaded(installed)
        test_machine.assert_shallow_binary_loaded(loaded)
        with pytest.raises(BinaryUninstallError):
            installed.uninstall()
        test_machine.assert_shallow_binary_loaded(binary.load())

    def test_provider_dry_run_does_not_change_host_python(self, test_machine):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=0)
        test_machine.exercise_provider_dry_run(
            provider,
            bin_name="python",
            expect_present_before=True,
            stale_min_version=SemVer("999.0.0"),
        )
