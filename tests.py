#!/usr/bin/env python

import os
import sys
import shutil
import unittest
import subprocess
from io import StringIO
from unittest import mock
from pathlib import Path

# from rich import print

from pydantic_pkgr import (
    BinProvider, EnvProvider, Binary, SemVer, BinProviderOverrides,
    PipProvider, NpmProvider, AptProvider, BrewProvider,
)


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


def flatten(xss):
    return [x for xs in xss for x in xs]

class InstallTest(unittest.TestCase):

    def install_with_binprovider(self, provider, binary):

        binary_bin = binary.load_or_install()
        provider_bin = provider.load_or_install(bin_name=binary.name)
        # print(binary_bin, binary_bin.bin_dir, binary_bin.loaded_abspath)
        # print('\n'.join(f'{provider}={path}' for provider, path in binary.loaded_abspaths.items()), '\n')
        # print()

        self.assertEqual(binary_bin.loaded_binprovider, provider_bin.loaded_binprovider)
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
        print('PIP BINPROVIDER', pipprovider.INSTALLER_BIN_ABSPATH, 'PATH=', pipprovider.PATH)
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
        has_brew = shutil.which('brew') or os.path.isfile('/home/linuxbrew/.linuxbrew/bin/brew')
        # has_apt = shutil.which('dpkg') is not None
        
        provider = BrewProvider()
        if has_brew:
            self.assertTrue(provider.PATH)
            self.assertTrue(provider.is_valid)
        else:
            print('SHOULD NOT HAVE BREW, but got', provider.INSTALLER_BIN_ABSPATH, 'PATH=', provider.PATH)
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
