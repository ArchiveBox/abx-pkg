#!/usr/bin/env python

import os
import sys
import shutil
import tempfile
import unittest
import subprocess
from io import StringIO
from unittest import mock
from pathlib import Path

# from rich import print

from abx_pkg import (
    BinProvider, EnvProvider, Binary, SemVer, BinProviderOverrides,
    PipProvider, NpmProvider, AptProvider, BrewProvider,
)
from abx_pkg.binprovider_ansible import AnsibleProvider
from abx_pkg.binprovider_pyinfra import PyinfraProvider

REAL_OS_STAT = os.stat
LIVE_PKG_TESTS = os.environ.get('ABX_PKG_LIVE_PKG_TESTS') == '1'


def stat_with_uid(path, uid):
    result = REAL_OS_STAT(path)
    return os.stat_result((
        result.st_mode,
        result.st_ino,
        result.st_dev,
        result.st_nlink,
        uid,
        result.st_gid,
        result.st_size,
        int(result.st_atime),
        int(result.st_mtime),
        int(result.st_ctime),
    ))


class TestSemVer(unittest.TestCase):

    def test_parsing(self):
        self.assertEqual(SemVer(None), None)
        self.assertEqual(SemVer(''), None)
        self.assertEqual(SemVer.parse(''), None)
        self.assertEqual(SemVer(1), (1, 0, 0))
        self.assertEqual(SemVer(1, 2), (1, 2, 0))
        self.assertEqual(SemVer('1.2+234234'), (1, 2, 234234))
        self.assertEqual(SemVer('1.2+beta'), (1, 2, 0))
        self.assertEqual(SemVer('1.2.4(1)+beta'), (1, 2, 4))
        self.assertEqual(SemVer('1.2+beta(3)'), (1, 2, 3))
        self.assertEqual(SemVer('1.2+6-be1ta(4)'), (1, 2, 6))
        self.assertEqual(SemVer('1.2 curl(8)beta-4'), (1, 2, 0))
        self.assertEqual(SemVer('1.2+curl(8)beta-4'), (1, 2, 8))
        self.assertEqual(SemVer((1, 2, 3)), (1, 2, 3))
        self.assertEqual(getattr(SemVer((1, 2, 3)), 'full_text'), '1.2.3')
        self.assertEqual(SemVer(('1', '2', '3')), (1, 2, 3))
        self.assertEqual(SemVer.parse('5.6.7'), (5, 6, 7))
        self.assertEqual(SemVer.parse('124.0.6367.208'), (124, 0, 6367))
        self.assertEqual(SemVer.parse('Google Chrome 124.1+234.234'), (124, 1, 234))
        self.assertEqual(SemVer.parse('Google Ch1rome 124.0.6367.208'), (124, 0, 6367))
        self.assertEqual(SemVer.parse('Google Chrome 124.0.6367.208+beta_234. 234.234.123\n123.456.324'), (124, 0, 6367))
        self.assertEqual(getattr(SemVer.parse('Google Chrome 124.0.6367.208+beta_234. 234.234.123\n123.456.324'), 'full_text'), 'Google Chrome 124.0.6367.208+beta_234. 234.234.123')
        self.assertEqual(SemVer.parse('Google Chrome'), None)


class TestBinProvider(unittest.TestCase):

    def test_python_env(self):
        provider = EnvProvider()

        python_bin = provider.load('python')
        self.assertEqual(python_bin, provider.load_or_install('python'))

        self.assertEqual(python_bin.loaded_version, SemVer('{}.{}.{}'.format(*sys.version_info[:3])))
        self.assertEqual(python_bin.loaded_abspath, Path(sys.executable).absolute())
        self.assertEqual(python_bin.loaded_respath, Path(sys.executable).resolve())
        self.assertTrue(python_bin.is_valid)
        self.assertTrue(python_bin.is_executable)
        self.assertFalse(python_bin.is_script)
        self.assertTrue(bool(str(python_bin)))  # easy way to make sure serializing doesnt throw an error
        self.assertEqual(str(python_bin.loaded_binprovider.INSTALLER_BINARY.abspath), str(shutil.which('which')))


    def test_bash_env(self):
        envprovider = EnvProvider()

        SYS_BASH_VERSION = subprocess.check_output('bash --version', shell=True, text=True).split('\n')[0]

        bash_bin = envprovider.load_or_install('bash')
        self.assertEqual(bash_bin.loaded_version, SemVer(SYS_BASH_VERSION))
        self.assertGreater(bash_bin.loaded_version, SemVer('3.0.0'))
        self.assertEqual(bash_bin.loaded_abspath, Path(shutil.which('bash')))
        self.assertTrue(bash_bin.is_valid)
        self.assertTrue(bash_bin.is_executable)
        self.assertFalse(bash_bin.is_script)
        self.assertTrue(bool(str(bash_bin)))  # easy way to make sure serializing doesnt throw an error

    def test_overrides(self):
        
        class TestRecord:
            called_default_abspath_getter = False
            called_default_version_getter = False
            called_default_packages_getter = False
            called_custom_install_handler = False

        def custom_version_getter():
            return '1.2.3'
        
        def custom_abspath_getter(self, bin_name, **context):
            assert self.__class__.__name__ == 'CustomProvider'
            return '/usr/bin/true'

        class CustomProvider(BinProvider):
            name: str = 'custom'

            overrides: BinProviderOverrides = {
                '*': {
                    'abspath': 'self.default_abspath_getter',     # test staticmethod referenced via dotted notation on self.
                    'packages': 'self.default_packages_getter',   # test classmethod referenced via dotted notation on self.
                    'version': 'self.default_version_getter',     # test normal method referenced via dotted notation on self.
                    'install': None,                              # test intentionally nulled handler
                },
                'somebin': {
                    'abspath': custom_abspath_getter,             # test external func that takes self, bin_name, and **context
                    'version': custom_version_getter,             # test external func that takes no args
                    'packages': ['literal', 'return', 'value'],   # test literal return value
                },
                'abc': {
                    'packages': 'self.alternate_packages_getter', # test classmethod that overrules default handler
                },
            }

            @staticmethod
            def default_abspath_getter():
                TestRecord.called_default_abspath_getter = True
                return '/bin/bash'

            @classmethod
            def default_packages_getter(cls, bin_name: str, **context):
                TestRecord.called_default_packages_getter = True
                return None
            
            def default_version_getter(self, bin_name: str, **context):
                TestRecord.called_default_version_getter = True
                return '999.999.999'

            
            @classmethod
            def alternate_packages_getter(cls, bin_name: str, **context):
                TestRecord.called_default_packages_getter = True
                return ['abc', 'def']

            def on_install(self, bin_name: str, **context):
                raise NotImplementedError('whattt')

        provider = CustomProvider()
        provider._dry_run = True

        self.assertFalse(TestRecord.called_default_abspath_getter)
        self.assertFalse(TestRecord.called_default_version_getter)
        self.assertFalse(TestRecord.called_default_packages_getter)
        self.assertFalse(TestRecord.called_custom_install_handler)

        # test default abspath getter
        self.assertEqual(provider.get_abspath('doesnotexist'), Path('/bin/bash'))
        self.assertTrue(TestRecord.called_default_abspath_getter)
        
        # test custom abspath getter
        self.assertEqual(provider.get_abspath('somebin'), Path('/usr/bin/true'))    # test that Callable getter that takes self, bin_name, **context works + result is auto-cast to Path
        
        # test default version getter
        self.assertEqual(provider.get_version('doesnotexist'), SemVer('999.999.999'))  # test that normal 'self.some_method' dot referenced getter works and result is auto-cast to SemVer
        self.assertTrue(TestRecord.called_default_version_getter)

        # test custom version getter
        self.assertEqual(provider.get_version('somebin'), SemVer('1.2.3'))         # test that remote Callable func getter that takes no args works and str result is auto-cast to SemVer
        
        # test default packages getter
        self.assertEqual(provider.get_packages('doesnotexist'), ('doesnotexist',))  # test that it fallsback to [bin_name] by default if getter returns None
        self.assertTrue(TestRecord.called_default_packages_getter)
        self.assertEqual(provider.get_packages('abc'), ('abc', 'def'))             # test that classmethod getter funcs work
        
        # test custom packages getter
        self.assertEqual(provider.get_packages('somebin'), ('literal', 'return', 'value'))  # test that literal return values in overrides work     
        
        # test install handler
        exc = None
        try:
            provider.install('doesnotexist')
        except Exception as err:
            exc = err
        self.assertIsInstance(exc, AssertionError)
        self.assertTrue('BinProvider(name=custom) has no install handler implemented for Binary(name=doesnotexist)' in str(exc))

    @mock.patch.object(BinProvider, "INSTALLER_BIN_ABSPATH", new_callable=mock.PropertyMock, return_value=Path(sys.executable))
    @mock.patch("abx_pkg.binprovider.os.stat")
    @mock.patch("abx_pkg.binprovider.os.geteuid", return_value=0)
    @mock.patch.object(BinProvider, "uid_has_passwd_entry", side_effect=lambda uid: uid == 0)
    def test_binprovider_euid_falls_back_from_unmapped_installer_owner(self, _mock_uid_has_passwd_entry, _mock_geteuid, mock_stat, _mock_installer_bin_abspath):
        class CustomProvider(BinProvider):
            name: str = 'custom'

        def fake_stat(path, *args, **kwargs):
            if Path(path) == Path(sys.executable):
                return stat_with_uid(path, 1001)
            return REAL_OS_STAT(path, *args, **kwargs)

        mock_stat.side_effect = fake_stat
        provider = CustomProvider()
        self.assertEqual(provider.EUID, 0)

    @mock.patch("abx_pkg.binprovider.subprocess.run", return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr=''))
    @mock.patch("abx_pkg.binprovider.pwd.getpwuid", side_effect=KeyError)
    @mock.patch("abx_pkg.binprovider.os.getegid", return_value=54321)
    @mock.patch("abx_pkg.binprovider.os.geteuid", return_value=12345)
    def test_exec_handles_current_uid_without_passwd_entry(self, _mock_geteuid, _mock_getegid, _mock_getpwuid, mock_run):
        provider = EnvProvider(euid=12345)

        with mock.patch.dict(os.environ, {'HOME': '/tmp/container-home', 'USER': 'container', 'LOGNAME': 'container'}, clear=False):
            proc = provider.exec(bin_name=sys.executable, cmd=['--version'], quiet=True)

        self.assertEqual(proc.returncode, 0)
        env = mock_run.call_args.kwargs['env']
        self.assertEqual(env['HOME'], '/tmp/container-home')
        self.assertEqual(env['USER'], 'container')
        self.assertEqual(env['LOGNAME'], 'container')

    @mock.patch("abx_pkg.binprovider_npm.NpmProvider._load_PATH", return_value="")
    @mock.patch("abx_pkg.binprovider.os.geteuid", return_value=0)
    def test_npm_provider_keeps_root_euid_for_global_installs(self, _mock_geteuid, _mock_load_path):
        provider = NpmProvider()
        self.assertEqual(provider.euid, 0)
        self.assertEqual(provider.EUID, 0)

    @mock.patch("abx_pkg.binprovider_npm.NpmProvider._load_PATH", return_value="")
    @mock.patch("abx_pkg.binprovider.os.geteuid", return_value=0)
    @mock.patch("abx_pkg.binprovider.os.stat")
    @mock.patch.object(BinProvider, "uid_has_passwd_entry", return_value=True)
    def test_npm_provider_prefers_prefix_owner_over_root(self, _mock_uid_has_passwd_entry, _mock_stat, _mock_geteuid, _mock_load_path):
        with tempfile.TemporaryDirectory() as temp_dir:
            prefix = Path(temp_dir)
            _mock_stat.side_effect = lambda path, *args, **kwargs: stat_with_uid(path, 1001) if Path(path) == prefix else REAL_OS_STAT(path, *args, **kwargs)
            provider = NpmProvider(npm_prefix=prefix)
            self.assertEqual(provider.euid, 1001)

    @mock.patch("abx_pkg.binprovider.os.geteuid", return_value=0)
    def test_pip_provider_keeps_root_euid_for_global_installs(self, _mock_geteuid):
        provider = PipProvider()
        self.assertEqual(provider.euid, 0)
        self.assertEqual(provider.EUID, 0)

    def test_npm_provider_respects_explicit_euid(self):
        provider = NpmProvider(euid=0)
        self.assertEqual(provider.euid, 0)
        self.assertEqual(provider.EUID, 0)


class TestForwardRefs(unittest.TestCase):

    def test_subclass_without_overrides_import(self):
        class CustomProvider(BinProvider):
            name: str = 'custom'

        provider = CustomProvider()
        self.assertEqual(provider.name, 'custom')


class TestBinary(unittest.TestCase):

    def test_python_bin(self):
        envprovider = EnvProvider()

        python_bin = Binary(name='python', binproviders=[envprovider])

        self.assertIsNone(python_bin.loaded_binprovider)
        self.assertIsNone(python_bin.loaded_abspath)
        self.assertIsNone(python_bin.loaded_version)

        python_bin = python_bin.load()

        shallow_bin = envprovider.load_or_install('python')
        assert shallow_bin and python_bin.loaded_binprovider
        self.assertEqual(python_bin.loaded_binprovider, shallow_bin.loaded_binprovider)
        self.assertEqual(python_bin.loaded_abspath, shallow_bin.loaded_abspath)
        self.assertEqual(python_bin.loaded_version, shallow_bin.loaded_version)
        self.assertEqual(python_bin.loaded_sha256, shallow_bin.loaded_sha256)

        self.assertEqual(python_bin.loaded_version, SemVer('{}.{}.{}'.format(*sys.version_info[:3])))
        self.assertEqual(python_bin.loaded_abspath, Path(sys.executable).absolute())
        self.assertEqual(python_bin.loaded_respath, Path(sys.executable).resolve())
        self.assertTrue(python_bin.is_valid)
        self.assertTrue(python_bin.is_executable)
        self.assertFalse(python_bin.is_script)
        self.assertTrue(bool(str(python_bin)))  # easy way to make sure serializing doesnt throw an error

    def test_min_version_accepts_string(self):
        binary = Binary(name='python', abspath=sys.executable, version='1.2.3', min_version='1.2.0')

        self.assertEqual(binary.min_version, SemVer('1.2.0'))
        self.assertTrue(binary.is_valid)

    def test_min_version_invalidates_lower_loaded_version(self):
        binary = Binary(name='python', abspath=sys.executable, version='1.2.3', min_version=SemVer('1.2.4'))

        self.assertEqual(binary.min_version, SemVer('1.2.4'))
        self.assertFalse(binary.is_valid)

    def test_min_version_allows_equal_loaded_version(self):
        binary = Binary(name='python', abspath=sys.executable, version='1.2.3', min_version='1.2.3')

        self.assertTrue(binary.is_valid)

    def test_update_uses_matching_provider_and_returns_loaded_binary(self):
        provider = EnvProvider()
        updated_bin = provider.load('python')
        assert updated_bin is not None

        binary = Binary(name='python', binproviders=[provider])
        with mock.patch.object(EnvProvider, 'update', return_value=updated_bin, create=True) as mock_update:
            result = binary.update(binproviders=[provider.name])

        mock_update.assert_called_once_with('python')
        self.assertEqual(result.loaded_binprovider, provider)
        self.assertEqual(result.loaded_abspath, updated_bin.loaded_abspath)
        self.assertEqual(result.loaded_version, updated_bin.loaded_version)
        self.assertEqual(result.loaded_sha256, updated_bin.loaded_sha256)

    def test_uninstall_clears_loaded_fields(self):
        provider = EnvProvider()
        binary = Binary(
            name='python',
            binproviders=[provider],
            binprovider=provider,
            abspath=sys.executable,
            version='1.2.3',
            sha256='unknown',
        )

        with mock.patch.object(EnvProvider, 'uninstall', return_value=True, create=True) as mock_uninstall:
            result = binary.uninstall(binproviders=[provider.name])

        mock_uninstall.assert_called_once_with('python')
        self.assertIsNone(result.loaded_binprovider)
        self.assertIsNone(result.loaded_abspath)
        self.assertIsNone(result.loaded_version)
        self.assertIsNone(result.loaded_sha256)
        self.assertEqual(result.binproviders_supported, [provider])
        self.assertFalse(result.is_valid)


class TestUpdateAndUninstall(unittest.TestCase):

    @mock.patch.object(BinProvider, 'INSTALLER_BIN_ABSPATH', new_callable=mock.PropertyMock, return_value=Path('/usr/local/bin/brew'))
    @mock.patch('abx_pkg.binprovider_brew.BrewProvider.load_PATH', lambda self: self)
    @mock.patch('abx_pkg.binprovider_pyinfra.PYINFRA_INSTALLED', False)
    @mock.patch('abx_pkg.binprovider_ansible.ANSIBLE_INSTALLED', False)
    def test_brew_provider_update_uses_upgrade_command(self, _mock_installer_bin_abspath):
        provider = BrewProvider()
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr='')

        with (
            mock.patch('abx_pkg.binprovider_brew.time.time', return_value=0),
            mock.patch.object(BrewProvider, 'exec', side_effect=[proc, proc]) as mock_exec,
            mock.patch.object(BrewProvider, 'get_abspath', return_value=Path(sys.executable)),
            mock.patch.object(BrewProvider, 'get_version', return_value=SemVer('3.11.0')),
            mock.patch.object(BrewProvider, 'get_sha256', return_value='unknown'),
        ):
            provider.update('python')

        self.assertEqual(mock_exec.call_args_list[0].kwargs['cmd'], ['update'])
        self.assertEqual(mock_exec.call_args_list[1].kwargs['cmd'], ['upgrade', 'python'])

    @mock.patch.object(BinProvider, 'INSTALLER_BIN_ABSPATH', new_callable=mock.PropertyMock, return_value=Path('/usr/local/bin/brew'))
    @mock.patch('abx_pkg.binprovider_brew.BrewProvider.load_PATH', lambda self: self)
    @mock.patch('abx_pkg.binprovider_pyinfra.PYINFRA_INSTALLED', False)
    @mock.patch('abx_pkg.binprovider_ansible.ANSIBLE_INSTALLED', False)
    def test_brew_provider_uninstall_uses_uninstall_command(self, _mock_installer_bin_abspath):
        provider = BrewProvider()
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr='')

        with mock.patch.object(BrewProvider, 'exec', return_value=proc) as mock_exec:
            result = provider.uninstall('python')

        self.assertTrue(result)
        self.assertEqual(mock_exec.call_args.kwargs['cmd'], ['uninstall', 'python'])

    @mock.patch.object(BinProvider, 'INSTALLER_BIN_ABSPATH', new_callable=mock.PropertyMock, return_value=Path('/usr/local/bin/npm'))
    @mock.patch('abx_pkg.binprovider_npm.NpmProvider._load_PATH', return_value='')
    def test_npm_provider_update_uses_update_command(self, _mock_load_path, _mock_installer_bin_abspath):
        provider = NpmProvider(euid=os.geteuid())
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr='')

        with (
            mock.patch.object(NpmProvider, 'exec', return_value=proc) as mock_exec,
            mock.patch.object(NpmProvider, 'get_abspath', return_value=Path(sys.executable)),
            mock.patch.object(NpmProvider, 'get_version', return_value=SemVer('3.11.0')),
            mock.patch.object(NpmProvider, 'get_sha256', return_value='unknown'),
        ):
            provider.update('python')

        self.assertEqual(mock_exec.call_args.kwargs['cmd'], ['update', '--force', '--no-audit', '--no-fund', '--loglevel=error', provider.cache_arg, '--global', 'python'])

    @mock.patch.object(BinProvider, 'INSTALLER_BIN_ABSPATH', new_callable=mock.PropertyMock, return_value=Path('/usr/local/bin/npm'))
    @mock.patch('abx_pkg.binprovider_npm.NpmProvider._load_PATH', return_value='')
    def test_npm_provider_uninstall_uses_uninstall_command(self, _mock_load_path, _mock_installer_bin_abspath):
        provider = NpmProvider(euid=os.geteuid())
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr='')

        with mock.patch.object(NpmProvider, 'exec', return_value=proc) as mock_exec:
            result = provider.uninstall('python')

        self.assertTrue(result)
        self.assertEqual(mock_exec.call_args.kwargs['cmd'], ['uninstall', '--force', '--no-audit', '--no-fund', '--loglevel=error', provider.cache_arg, '--global', 'python'])

    @mock.patch.object(BinProvider, 'INSTALLER_BIN_ABSPATH', new_callable=mock.PropertyMock, return_value=Path('/usr/local/bin/pip'))
    @mock.patch('abx_pkg.binprovider_pip.PipProvider.load_PATH_from_pip_sitepackages', lambda self: self)
    def test_pip_provider_update_uses_install_upgrade(self, _mock_installer_bin_abspath):
        provider = PipProvider(euid=os.geteuid())
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr='')

        with (
            mock.patch.object(PipProvider, 'exec', return_value=proc) as mock_exec,
            mock.patch.object(PipProvider, 'get_abspath', return_value=Path(sys.executable)),
            mock.patch.object(PipProvider, 'get_version', return_value=SemVer('3.11.0')),
            mock.patch.object(PipProvider, 'get_sha256', return_value='unknown'),
        ):
            provider.update('python')

        self.assertEqual(mock_exec.call_args.kwargs['cmd'], ['install', '--no-input', provider.cache_arg, *provider.pip_install_args, '--upgrade', 'python'])

    @mock.patch.object(BinProvider, 'INSTALLER_BIN_ABSPATH', new_callable=mock.PropertyMock, return_value=Path('/usr/local/bin/pip'))
    @mock.patch('abx_pkg.binprovider_pip.PipProvider.load_PATH_from_pip_sitepackages', lambda self: self)
    def test_pip_provider_uninstall_uses_uninstall_command(self, _mock_installer_bin_abspath):
        provider = PipProvider(euid=os.geteuid())
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr='')

        with mock.patch.object(PipProvider, 'exec', return_value=proc) as mock_exec:
            result = provider.uninstall('python')

        self.assertTrue(result)
        self.assertEqual(mock_exec.call_args.kwargs['cmd'], ['uninstall', '--yes', 'python'])

    @mock.patch.object(BinProvider, 'INSTALLER_BIN_ABSPATH', new_callable=mock.PropertyMock, return_value=Path('/usr/bin/apt-get'))
    @mock.patch('abx_pkg.binprovider_apt.shutil.which', side_effect=lambda name: '/usr/bin/dpkg' if name == 'dpkg' else '/usr/bin/apt-get')
    @mock.patch('abx_pkg.binprovider_pyinfra.PYINFRA_INSTALLED', False)
    @mock.patch('abx_pkg.binprovider_ansible.ANSIBLE_INSTALLED', False)
    def test_apt_provider_update_uses_only_upgrade(self, _mock_installer_bin_abspath, _mock_which):
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr='')

        with (
            mock.patch('abx_pkg.binprovider_apt.time.time', return_value=0),
            mock.patch.object(AptProvider, 'exec', side_effect=[proc, proc, proc]) as mock_exec,
            mock.patch.object(AptProvider, 'get_abspath', return_value=Path(sys.executable)),
            mock.patch.object(AptProvider, 'get_version', return_value=SemVer('3.11.0')),
            mock.patch.object(AptProvider, 'get_sha256', return_value='unknown'),
        ):
            provider = AptProvider()
            provider.update('python')

        self.assertEqual(mock_exec.call_args_list[1].kwargs['cmd'], ['update', '-qq'])
        self.assertEqual(mock_exec.call_args_list[2].kwargs['cmd'], ['install', '--only-upgrade', '-y', '-qq', '--no-install-recommends', 'python'])

    @mock.patch.object(BinProvider, 'INSTALLER_BIN_ABSPATH', new_callable=mock.PropertyMock, return_value=Path('/usr/bin/apt-get'))
    @mock.patch('abx_pkg.binprovider_apt.shutil.which', side_effect=lambda name: '/usr/bin/dpkg' if name == 'dpkg' else '/usr/bin/apt-get')
    @mock.patch('abx_pkg.binprovider_pyinfra.PYINFRA_INSTALLED', False)
    @mock.patch('abx_pkg.binprovider_ansible.ANSIBLE_INSTALLED', False)
    def test_apt_provider_uninstall_uses_remove(self, _mock_installer_bin_abspath, _mock_which):
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr='')

        with mock.patch.object(AptProvider, 'exec', side_effect=[proc, proc]) as mock_exec:
            provider = AptProvider()
            result = provider.uninstall('python')

        self.assertTrue(result)
        self.assertEqual(mock_exec.call_args_list[1].kwargs['cmd'], ['remove', '-y', '-qq', 'python'])

    @mock.patch('abx_pkg.binprovider_pyinfra.pyinfra_package_install', return_value='updated')
    @mock.patch.object(PyinfraProvider, 'get_abspath', return_value=Path(sys.executable))
    @mock.patch.object(PyinfraProvider, 'get_version', return_value=SemVer('3.11.0'))
    @mock.patch.object(PyinfraProvider, 'get_sha256', return_value='unknown')
    def test_pyinfra_provider_update_uses_latest_state(self, _mock_sha256, _mock_version, _mock_abspath, mock_pyinfra_install):
        provider = PyinfraProvider(pyinfra_installer_module='operations.server.packages')

        provider.update('python')

        mock_pyinfra_install.assert_called_once_with(
            pkg_names=('python',),
            installer_module='operations.server.packages',
            installer_extra_kwargs={'latest': True},
        )

    @mock.patch('abx_pkg.binprovider_pyinfra.pyinfra_package_install', return_value='removed')
    def test_pyinfra_provider_uninstall_uses_absent_state(self, mock_pyinfra_install):
        provider = PyinfraProvider(pyinfra_installer_module='operations.server.packages')

        result = provider.uninstall('python')

        self.assertTrue(result)
        mock_pyinfra_install.assert_called_once_with(
            pkg_names=('python',),
            installer_module='operations.server.packages',
            installer_extra_kwargs={'present': False},
        )

    @mock.patch.object(BinProvider, 'INSTALLER_BIN_ABSPATH', new_callable=mock.PropertyMock, return_value=Path('/usr/local/bin/ansible'))
    @mock.patch('abx_pkg.binprovider_ansible.ansible_package_install', return_value='updated')
    @mock.patch.object(AnsibleProvider, 'get_abspath', return_value=Path(sys.executable))
    @mock.patch.object(AnsibleProvider, 'get_version', return_value=SemVer('3.11.0'))
    @mock.patch.object(AnsibleProvider, 'get_sha256', return_value='unknown')
    def test_ansible_provider_update_uses_latest_state(self, _mock_sha256, _mock_version, _mock_abspath, mock_ansible_install, _mock_installer_bin_abspath):
        provider = AnsibleProvider(ansible_installer_module='ansible.builtin.package')

        provider.update('python')

        mock_ansible_install.assert_called_once_with(
            pkg_names=('python',),
            quiet=True,
            playbook_template=provider.ansible_playbook_template,
            installer_module='ansible.builtin.package',
            state='latest',
        )

    @mock.patch.object(BinProvider, 'INSTALLER_BIN_ABSPATH', new_callable=mock.PropertyMock, return_value=Path('/usr/local/bin/ansible'))
    @mock.patch('abx_pkg.binprovider_ansible.ansible_package_install', return_value='removed')
    def test_ansible_provider_uninstall_uses_absent_state(self, mock_ansible_install, _mock_installer_bin_abspath):
        provider = AnsibleProvider(ansible_installer_module='ansible.builtin.package')

        result = provider.uninstall('python')

        self.assertTrue(result)
        mock_ansible_install.assert_called_once_with(
            pkg_names=('python',),
            quiet=True,
            playbook_template=provider.ansible_playbook_template,
            installer_module='ansible.builtin.package',
            state='absent',
        )


def flatten(xss):
    return [x for xs in xss for x in xs]


def brew_formula_is_installed(package: str) -> bool:
    brew = shutil.which('brew')
    if not brew:
        return False
    return subprocess.run([brew, 'list', '--formula', package], capture_output=True, text=True).returncode == 0


def apt_package_is_installed(package: str) -> bool:
    dpkg = shutil.which('dpkg')
    if not dpkg:
        return False
    return subprocess.run([dpkg, '-s', package], capture_output=True, text=True).returncode == 0


class LiveUpdateAndUninstallTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not LIVE_PKG_TESTS:
            raise unittest.SkipTest('Set ABX_PKG_LIVE_PKG_TESTS=1 to run destructive live package-manager tests')

    def assert_binary_lifecycle(self, binary: Binary):
        provider = binary.binproviders_supported[0]
        self.assertIsNone(provider.load(binary.name, quiet=True, nocache=True))

        try:
            binary = binary.install()
            self.assertTrue(binary.is_valid)
            self.assertEqual(binary.loaded_binprovider, provider)
            self.assertIsNotNone(binary.loaded_abspath)
            self.assertIsNotNone(binary.loaded_version)

            updated_binary = binary.update()
            self.assertTrue(updated_binary.is_valid)
            self.assertEqual(updated_binary.loaded_binprovider, provider)
            self.assertIsNotNone(updated_binary.loaded_abspath)
            self.assertIsNotNone(updated_binary.loaded_version)

            removed_binary = updated_binary.uninstall()
            self.assertFalse(removed_binary.is_valid)
            self.assertIsNone(removed_binary.loaded_binprovider)
            self.assertIsNone(removed_binary.loaded_abspath)
            self.assertIsNone(removed_binary.loaded_version)
            self.assertIsNone(removed_binary.loaded_sha256)
        finally:
            provider.uninstall(binary.name, quiet=True, nocache=True)

        self.assertIsNone(provider.load(binary.name, quiet=True, nocache=True))

    def pick_missing_brew_formula(self) -> str:
        provider = BrewProvider()
        for formula in ('fzy', 'entr', 'renameutils', 'watch', 'hello'):
            if brew_formula_is_installed(formula):
                continue
            if provider.load(formula, quiet=True, nocache=True) is not None:
                continue
            return formula
        raise unittest.SkipTest('No safe missing brew formula candidates were available for a live lifecycle test')

    def pick_missing_apt_package(self) -> str:
        provider = AptProvider()
        for package in ('tree', 'jq', 'whois', 'rename', 'mlocate'):
            if apt_package_is_installed(package):
                continue
            if provider.load(package, quiet=True, nocache=True) is not None:
                continue
            return package
        raise unittest.SkipTest('No safe missing apt package candidates were available for a live lifecycle test')

    def test_pip_provider_live_update_and_uninstall(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = PipProvider(pip_venv=Path(temp_dir) / 'venv')
            binary = Binary(name='black', binproviders=[provider])
            self.assert_binary_lifecycle(binary)

    def test_npm_provider_live_update_and_uninstall(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NpmProvider(npm_prefix=Path(temp_dir) / 'npm')
            binary = Binary(name='esbuild', binproviders=[provider])
            self.assert_binary_lifecycle(binary)

    def test_brew_provider_live_update_and_uninstall(self):
        if not shutil.which('brew'):
            raise unittest.SkipTest('brew is not available on this host')

        provider = BrewProvider()
        binary = Binary(name=self.pick_missing_brew_formula(), binproviders=[provider])
        self.assert_binary_lifecycle(binary)

    def test_pyinfra_provider_live_update_and_uninstall(self):
        if not shutil.which('pyinfra'):
            raise unittest.SkipTest('pyinfra is not available on this host')

        if 'linux' in sys.platform and shutil.which('apt-get'):
            if os.geteuid() != 0:
                raise unittest.SkipTest('pyinfra apt lifecycle tests require root on Linux')
            provider = PyinfraProvider(pyinfra_installer_module='operations.apt.packages')
            binary = Binary(name=self.pick_missing_apt_package(), binproviders=[provider])
        elif shutil.which('brew'):
            provider = PyinfraProvider(pyinfra_installer_module='operations.brew.packages')
            binary = Binary(name=self.pick_missing_brew_formula(), binproviders=[provider])
        else:
            raise unittest.SkipTest('Neither apt nor brew is available on this host')

        self.assert_binary_lifecycle(binary)

    def test_ansible_provider_live_update_and_uninstall(self):
        if not shutil.which('ansible'):
            raise unittest.SkipTest('ansible is not available on this host')

        if 'linux' in sys.platform and shutil.which('apt-get'):
            if os.geteuid() != 0:
                raise unittest.SkipTest('ansible apt lifecycle tests require root on Linux')
            provider = AnsibleProvider(ansible_installer_module='ansible.builtin.apt')
            binary = Binary(name=self.pick_missing_apt_package(), binproviders=[provider])
        elif shutil.which('brew'):
            provider = AnsibleProvider(ansible_installer_module='community.general.homebrew')
            binary = Binary(name=self.pick_missing_brew_formula(), binproviders=[provider])
        else:
            raise unittest.SkipTest('Neither apt nor brew is available on this host')

        self.assert_binary_lifecycle(binary)

    def test_apt_provider_live_update_and_uninstall(self):
        if 'linux' not in sys.platform:
            raise unittest.SkipTest('apt live lifecycle tests only run on Linux hosts')
        if not shutil.which('apt-get'):
            raise unittest.SkipTest('apt-get is not available on this host')
        if os.geteuid() != 0:
            raise unittest.SkipTest('apt lifecycle tests require root on Linux')

        provider = AptProvider()
        binary = Binary(name=self.pick_missing_apt_package(), binproviders=[provider])
        self.assert_binary_lifecycle(binary)


class InstallTest(unittest.TestCase):

    def install_with_binprovider(self, provider, binary):

        binary_bin = binary.load_or_install()
        provider_bin = provider.load_or_install(bin_name=binary.name)
        # print(binary_bin, binary_bin.bin_dir, binary_bin.loaded_abspath)
        # print('\n'.join(f'{provider}={path}' for provider, path in binary.loaded_abspaths.items()), '\n')
        # print()
        try:
            self.assertEqual(binary_bin.loaded_binprovider, provider_bin.loaded_binprovider)
        except AssertionError:
            print('binary_bin', dict(binary_bin.loaded_binprovider))
            print('provider_bin', dict(provider_bin.loaded_binprovider))
            raise
        self.assertEqual(binary_bin.loaded_abspath, provider_bin.loaded_abspath)
        self.assertEqual(binary_bin.loaded_version, provider_bin.loaded_version)
        self.assertEqual(binary_bin.loaded_sha256, provider_bin.loaded_sha256)

        self.assertIn(binary_bin.loaded_abspath, flatten(binary_bin.loaded_abspaths.values()))
        self.assertIn(str(binary_bin.bin_dir), flatten(PATH.split(':') for PATH in binary_bin.loaded_bin_dirs.values()))

        PATH = provider.PATH
        bin_abspath = shutil.which(binary.name, path=PATH)
        assert bin_abspath, f'Could not find {binary.name} in PATH={PATH}'
        VERSION = SemVer.parse(subprocess.check_output(f'{bin_abspath} --version', shell=True, text=True))
        ABSPATH = Path(bin_abspath).absolute().resolve()

        self.assertEqual(binary_bin.loaded_version, VERSION)
        self.assertIn(binary_bin.loaded_abspath, provider.get_abspaths(binary_bin.name))
        self.assertEqual(binary_bin.loaded_respath, ABSPATH)
        self.assertTrue(binary_bin.is_valid)
        self.assertTrue(binary_bin.is_executable)
        self.assertFalse(binary_bin.is_script)
        self.assertTrue(bool(str(binary_bin)))  # easy way to make sure serializing doesnt throw an error
        # print(provider.PATH)
        # print()
        # print()
        # print(binary.bin_filename, binary.bin_dir, binary.loaded_abspaths)
        # print()
        # print()
        # print(provider.name, 'PATH=', provider.PATH, 'ABSPATHS=', provider.get_abspaths(bin_name=binary_bin.name))
        return provider_bin

    def test_env_provider(self):
        provider = EnvProvider()
        binary = Binary(name='wget', binproviders=[provider]).load()
        self.install_with_binprovider(provider, binary)

    def test_pip_provider(self):
        # pipprovider = PipProvider()
        pipprovider = PipProvider(pip_venv=os.environ.get('VIRTUAL_ENV', None))
        # print('PIP BINPROVIDER', pipprovider.INSTALLER_BIN_ABSPATH, 'PATH=', pipprovider.PATH)
        binary = Binary(name='yt-dlp', binproviders=[pipprovider])
        self.install_with_binprovider(pipprovider, binary)

    def test_npm_provider(self):
        npmprovider = NpmProvider()
        # print(provider.PATH)
        binary = Binary(name='tsx', binproviders=[npmprovider])
        self.install_with_binprovider(npmprovider, binary)
    
    @mock.patch("sys.stderr")    
    @mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr=''))
    def test_dry_run_doesnt_exec(self, mock_run, _mock_stderr):
        pipprovider = PipProvider().get_provider_with_overrides(dry_run=True)
        pipprovider.install(bin_name='doesnotexist')
        mock_run.assert_not_called()
        
    @mock.patch("sys.stderr", new_callable=StringIO)
    def test_dry_run_prints_stderr(self, mock_stderr):
        pipprovider = PipProvider()
        binary = Binary(name='doesnotexist', binproviders=[pipprovider])
        binary.install(dry_run=True)
            
        self.assertIn('DRY RUN', mock_stderr.getvalue())

    def test_brew_provider(self):
        # print(provider.PATH)
        os.environ['HOMEBREW_NO_AUTO_UPDATE'] = 'True'
        os.environ['HOMEBREW_NO_INSTALL_CLEANUP'] = 'True'
        os.environ['HOMEBREW_NO_ENV_HINTS'] = 'True'

        is_on_windows = sys.platform.lower().startswith('win') or os.name == 'nt'
        is_on_macos = 'darwin' in sys.platform.lower()
        is_on_linux = 'linux' in sys.platform.lower()
        has_brew = shutil.which('brew')
        # has_apt = shutil.which('dpkg') is not None
        
        provider = BrewProvider()
        if has_brew:
            self.assertTrue(provider.PATH)
            self.assertTrue(provider.is_valid)
        else:
            # print('SHOULD NOT HAVE BREW, but got', provider.INSTALLER_BIN_ABSPATH, 'PATH=', provider.PATH)
            self.assertFalse(provider.is_valid)

        exception = None
        result = None
        try:
            binary = Binary(name='wget', binproviders=[provider])
            result = self.install_with_binprovider(provider, binary)
        except Exception as err:
            exception = err


        if is_on_macos or (is_on_linux and has_brew):
            self.assertTrue(has_brew)
            if exception:
                raise exception
            self.assertIsNone(exception)
            self.assertTrue(result)
        elif is_on_windows or (is_on_linux and not has_brew):
            self.assertFalse(has_brew)
            self.assertIsInstance(exception, Exception)
            self.assertFalse(result)
        else:
            raise exception


    def test_apt_provider(self):
        is_on_windows = sys.platform.startswith('win') or os.name == 'nt'
        is_on_macos = 'darwin' in sys.platform
        is_on_linux = 'linux' in sys.platform
        # has_brew = shutil.which('brew') is not None
        has_apt = shutil.which('apt-get') is not None


        exception = None
        result = None
        provider = AptProvider()
        if has_apt:
            self.assertTrue(provider.PATH)
        else:
            self.assertFalse(provider.PATH)
        try:
            # print(provider.PATH)
            binary = Binary(name='wget', binproviders=[provider])
            result = self.install_with_binprovider(provider, binary)
        except Exception as err:
            exception = err


        if is_on_linux:
            self.assertTrue(has_apt)
            if exception:
                raise exception
            self.assertIsNone(exception)
            self.assertTrue(result)
        elif is_on_windows or is_on_macos:
            self.assertFalse(has_apt)
            self.assertIsInstance(exception, Exception)
            self.assertFalse(result)
        else:
            raise exception


if __name__ == '__main__':
    unittest.main()
