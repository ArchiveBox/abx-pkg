import os
import shutil
import subprocess

import pytest

from abx_pkg import Binary, SemVer
from abx_pkg.binprovider_ansible import AnsibleProvider
from abx_pkg.exceptions import BinaryInstallError, BinaryLoadOrInstallError


def _ansible_provider_for_host(test_machine):
    test_machine.require_tool("ansible")
    if shutil.which("apt-get") and os.geteuid() == 0:
        provider = AnsibleProvider(
            ansible_installer_module="ansible.builtin.apt",
            postinstall_scripts=True,
            min_release_age=0,
        )
        return provider, test_machine.pick_missing_provider_binary(
            provider,
            ("tree", "rename", "jq"),
        )
    test_machine.require_tool("brew")
    provider = AnsibleProvider(
        ansible_installer_module="community.general.homebrew",
        postinstall_scripts=True,
        min_release_age=0,
    )
    return provider, test_machine.pick_missing_provider_binary(
        provider,
        ("watch", "fzy", "jq"),
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
            provider.install("sleep")
        with pytest.raises(subprocess.TimeoutExpired):
            provider.update("sleep")
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

    def test_unsupported_security_controls_fail_closed_and_binary_override_wins(
        self,
        test_machine,
        test_machine_dependencies,
    ):
        del test_machine_dependencies
        provider, package = _ansible_provider_for_host(test_machine)

        with pytest.raises(RuntimeError):
            AnsibleProvider(
                ansible_installer_module=provider.ansible_installer_module,
            ).install(
                package,
            )

        cleanup_provider = AnsibleProvider(
            ansible_installer_module=provider.ansible_installer_module,
            postinstall_scripts=True,
            min_release_age=0,
        )
        try:
            binary = Binary(
                name=package,
                binproviders=[
                    AnsibleProvider(
                        ansible_installer_module=provider.ansible_installer_module,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)

            failing_binary = Binary(
                name=package,
                binproviders=[
                    AnsibleProvider(
                        ansible_installer_module=provider.ansible_installer_module,
                    ),
                ],
            )
            with pytest.raises(BinaryInstallError):
                failing_binary.install()
        finally:
            cleanup_provider.uninstall(package, quiet=True, nocache=True)

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
                nocache=True,
            )
            test_machine.assert_shallow_binary_loaded(installed)

            with pytest.raises(ValueError):
                provider.update(
                    package,
                    postinstall_scripts=True,
                    min_release_age=0,
                    min_version=SemVer("999.0.0"),
                    nocache=True,
                )

            too_new = Binary(
                name=package,
                binproviders=[provider],
                postinstall_scripts=True,
                min_release_age=0,
                min_version=SemVer("999.0.0"),
            )
            with pytest.raises(BinaryLoadOrInstallError):
                too_new.load_or_install(nocache=True)
        finally:
            cleanup_provider.uninstall(package, quiet=True, nocache=True)

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
