import sys
import logging

import pytest

from abx_pkg import AptProvider, Binary


@pytest.mark.skipif("darwin" in sys.platform, reason="apt is not available on macOS")
@pytest.mark.root_required
class TestAptProvider:
    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("apt-get")

        provider = AptProvider(postinstall_scripts=True, min_release_age=0)
        test_machine.exercise_provider_lifecycle(
            provider,
            bin_name=test_machine.pick_missing_apt_package(),
        )

    def test_unsupported_security_controls_warn_and_continue(
        self,
        test_machine,
        caplog,
    ):
        test_machine.require_tool("apt-get")
        package = test_machine.pick_missing_apt_package()

        with caplog.at_level(logging.WARNING, logger="abx_pkg.binprovider"):
            installed = AptProvider().install(
                package,
                postinstall_scripts=False,
                min_release_age=1,
            )
        test_machine.assert_shallow_binary_loaded(installed)
        assert "ignoring unsupported min_release_age=1" in caplog.text
        assert "ignoring unsupported postinstall_scripts=False" in caplog.text

        caplog.clear()
        binary = Binary(
            name=test_machine.pick_missing_apt_package(),
            binproviders=[AptProvider()],
            postinstall_scripts=False,
            min_release_age=1,
        )
        with caplog.at_level(logging.WARNING, logger="abx_pkg.binprovider"):
            installed = binary.install()
        test_machine.assert_shallow_binary_loaded(installed)
        assert "ignoring unsupported min_release_age=1" in caplog.text
        assert "ignoring unsupported postinstall_scripts=False" in caplog.text

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("apt-get")

        binary = Binary(
            name=test_machine.pick_missing_apt_package(),
            binproviders=[
                AptProvider(postinstall_scripts=True, min_release_age=0),
            ],
            postinstall_scripts=True,
            min_release_age=0,
        )
        test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_package(self, test_machine):
        test_machine.require_tool("apt-get")
        provider = AptProvider(postinstall_scripts=True, min_release_age=0)
        test_machine.exercise_provider_dry_run(
            provider,
            bin_name=test_machine.pick_missing_apt_package(),
        )

    def test_helper_install_args_used_by_pyinfra_ansible_backends(self, test_machine):
        test_machine.require_tool("apt-get")

        primary = test_machine.pick_missing_apt_package()
        extra = "jq" if primary != "jq" else "tree"

        provider = AptProvider(
            postinstall_scripts=True,
            min_release_age=0,
        ).get_provider_with_overrides(
            overrides={primary: {"install_args": [primary, extra]}},
        )

        for pkg in (primary, extra):
            provider.uninstall(pkg, quiet=True, nocache=True)

        provider.install(primary, nocache=True)
        assert provider.load(extra, quiet=True, nocache=True) is not None

        provider.uninstall(primary, nocache=True)
        provider.uninstall(extra, quiet=True, nocache=True)
