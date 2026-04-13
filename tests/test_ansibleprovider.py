import shutil
import subprocess
import logging

import pytest

from abxpkg import Binary, SemVer
from abxpkg.binprovider_ansible import AnsibleProvider
from abxpkg.exceptions import BinaryInstallError


def _ansible_provider_for_host(test_machine):
    test_machine.require_tool("ansible")
    if shutil.which("apt-get"):
        provider = AnsibleProvider(
            ansible_installer_module="ansible.builtin.apt",
            postinstall_scripts=True,
            min_release_age=0,
        )
        return provider, test_machine.pick_missing_provider_binary(
            provider,
            ("tree", "rename", "jq", "tmux", "screen"),
        )
    test_machine.require_tool("brew")
    provider = AnsibleProvider(
        ansible_installer_module="community.general.homebrew",
        postinstall_scripts=True,
        min_release_age=0,
    )
    return provider, test_machine.pick_missing_provider_binary(
        provider,
        ("hello", "jq", "watch", "fzy", "tree"),
    )


class TestAnsibleProvider:
    def test_install_timeout_is_enforced_for_custom_playbook_runs(
        self,
        test_machine,
        test_machine_dependencies,
    ):
        del test_machine_dependencies
        test_machine.require_tool("ansible-playbook")

        provider = AnsibleProvider(
            ansible_installer_module="ansible.builtin.command",
            ansible_playbook_template="""
---
- name: Run a local command
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Run a local command
      {installer_module}:
        cmd: "{{{{item}}}}"
{module_extra_yaml}
      loop: {pkg_names}
""",
            postinstall_scripts=True,
            min_release_age=0,
            install_timeout=2,
        ).get_provider_with_overrides(
            overrides={"sleep": {"install_args": ["sleep 5"]}},
        )

        with pytest.raises(subprocess.TimeoutExpired):
            provider.install("sleep", no_cache=True)
        with pytest.raises(subprocess.TimeoutExpired):
            provider.update("sleep", no_cache=True)
        with pytest.raises(subprocess.TimeoutExpired):
            provider.uninstall("sleep")

    def test_provider_direct_methods_exercise_real_lifecycle(
        self,
        test_machine,
        test_machine_dependencies,
    ):
        del test_machine_dependencies
        provider, package = _ansible_provider_for_host(test_machine)

        test_machine.exercise_provider_lifecycle(provider, bin_name=package)

    def test_unsupported_security_controls_warn_and_continue(
        self,
        test_machine,
        test_machine_dependencies,
        caplog,
    ):
        del test_machine_dependencies
        provider, package = _ansible_provider_for_host(test_machine)

        cleanup_provider = AnsibleProvider(
            ansible_installer_module=provider.ansible_installer_module,
            postinstall_scripts=True,
            min_release_age=0,
        )
        try:
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = AnsibleProvider(
                    ansible_installer_module=provider.ansible_installer_module,
                ).install(
                    package,
                    postinstall_scripts=False,
                    min_release_age=1,
                )
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text

            caplog.clear()
            binary = Binary(
                name=package,
                binproviders=[
                    AnsibleProvider(
                        ansible_installer_module=provider.ansible_installer_module,
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=1,
            )
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text
        finally:
            cleanup_provider.uninstall(package, quiet=True, no_cache=True)

    def test_min_version_enforced_in_provider_and_binary_paths(
        self,
        test_machine,
        test_machine_dependencies,
    ):
        del test_machine_dependencies
        provider, package = _ansible_provider_for_host(test_machine)
        cleanup_provider = AnsibleProvider(
            ansible_installer_module=provider.ansible_installer_module,
            postinstall_scripts=True,
            min_release_age=0,
        )
        try:
            installed = provider.install(
                package,
                postinstall_scripts=True,
                min_release_age=0,
                no_cache=True,
            )
            test_machine.assert_shallow_binary_loaded(installed)

            with pytest.raises(ValueError):
                provider.update(
                    package,
                    postinstall_scripts=True,
                    min_release_age=0,
                    min_version=SemVer("999.0.0"),
                    no_cache=True,
                )

            too_new = Binary(
                name=package,
                binproviders=[provider],
                postinstall_scripts=True,
                min_release_age=0,
                min_version=SemVer("999.0.0"),
            )
            with pytest.raises(BinaryInstallError):
                too_new.install(no_cache=True)
        finally:
            cleanup_provider.uninstall(package, quiet=True, no_cache=True)

    def test_binary_direct_methods_exercise_real_lifecycle(
        self,
        test_machine,
        test_machine_dependencies,
    ):
        del test_machine_dependencies
        provider, package = _ansible_provider_for_host(test_machine)
        binary = Binary(
            name=package,
            binproviders=[provider],
            postinstall_scripts=True,
            min_release_age=0,
        )
        test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_package(
        self,
        test_machine,
        test_machine_dependencies,
    ):
        del test_machine_dependencies
        provider, package = _ansible_provider_for_host(test_machine)
        test_machine.exercise_provider_dry_run(provider, bin_name=package)
