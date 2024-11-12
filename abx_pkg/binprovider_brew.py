
#!/usr/bin/env python3
__package__ = "abx_pkg"

import os
import sys
import time
import platform
from typing import Optional
from pathlib import Path

from pydantic import model_validator, TypeAdapter

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs, HostBinPath, bin_abspath
from .semver import SemVer
from .binprovider import BinProvider

OS = platform.system().lower()

NEW_MACOS_DIR = Path('/opt/homebrew/bin')
OLD_MACOS_DIR = Path('/usr/local/bin')
DEFAULT_MACOS_DIR = NEW_MACOS_DIR if platform.machine() == 'arm64' else OLD_MACOS_DIR
DEFAULT_LINUX_DIR = Path('/home/linuxbrew/.linuxbrew/bin')
GUESSED_BREW_PREFIX = DEFAULT_MACOS_DIR if OS == 'darwin' else DEFAULT_LINUX_DIR

_LAST_UPDATE_CHECK = None
UPDATE_CHECK_INTERVAL = 60 * 60 * 24  # 1 day


class BrewProvider(BinProvider):
    name: BinProviderName = "brew"
    INSTALLER_BIN: BinName = "brew"
    
    PATH: PATHStr = f"{DEFAULT_LINUX_DIR}:{NEW_MACOS_DIR}:{OLD_MACOS_DIR}"
    
    brew_prefix: Path = GUESSED_BREW_PREFIX

    @model_validator(mode="after")
    def load_PATH(self):
        if not self.INSTALLER_BIN_ABSPATH:
            # brew is not availabe on this host
            self.PATH: PATHStr = ""
            return self

        PATHs = set(self.PATH.split(':'))
        
        if OS == 'darwin' and os.path.isdir(DEFAULT_MACOS_DIR) and os.access(DEFAULT_MACOS_DIR, os.R_OK):
            PATHs.add(str(DEFAULT_MACOS_DIR))
            self.brew_prefix = DEFAULT_MACOS_DIR / "bin"
        if OS != 'darwin' and os.path.isdir(DEFAULT_LINUX_DIR) and os.access(DEFAULT_LINUX_DIR, os.R_OK):
            PATHs.add(str(DEFAULT_LINUX_DIR))
            self.brew_prefix = DEFAULT_LINUX_DIR / "bin"
        
        if not PATHs:
            # if we cant autodetect the paths, run brew --prefix to get the path manually (very slow)
            self.brew_prefix = Path(self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=["--prefix"]).stdout.strip())
            PATHs.add(str(self.brew_prefix / "bin"))
        
        self.PATH = TypeAdapter(PATHStr).validate_python(':'.join(PATHs))
        return self

    def default_install_handler(self, bin_name: str, packages: Optional[InstallArgs] = None, **context) -> str:
        global _LAST_UPDATE_CHECK

        packages = packages or self.get_packages(bin_name)

        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(f"{self.__class__.__name__}.INSTALLER_BIN is not available on this host: {self.INSTALLER_BIN}")

        # print(f'[*] {self.__class__.__name__}: Installing {bin_name}: {self.INSTALLER_BIN_ABSPATH} install {packages}')

        # Attempt 1: Try installing with Pyinfra
        from .binprovider_pyinfra import PYINFRA_INSTALLED, pyinfra_package_install

        if PYINFRA_INSTALLED:
            return pyinfra_package_install((bin_name,), installer_module="operations.brew.packages")

        # Attempt 2: Try installing with Ansible
        from .binprovider_ansible import ANSIBLE_INSTALLED, ansible_package_install

        if ANSIBLE_INSTALLED:
            return ansible_package_install(bin_name, installer_module="community.general.homebrew")

        # Attempt 3: Fallback to installing manually by calling brew in shell
        
        if not _LAST_UPDATE_CHECK or (time.time() - _LAST_UPDATE_CHECK) > UPDATE_CHECK_INTERVAL:
            # only update if we haven't checked in the last day
            self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=["update"])
            _LAST_UPDATE_CHECK = time.time()
            
        proc = self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=["install", *packages])
        if proc.returncode != 0:
            print(proc.stdout.strip())
            print(proc.stderr.strip())
            raise Exception(f"{self.__class__.__name__} install got returncode {proc.returncode} while installing {packages}: {packages}")

        return proc.stderr.strip() + "\n" + proc.stdout.strip()

    def default_abspath_handler(self, bin_name: BinName | HostBinPath, **context) -> HostBinPath | None:
        # print(f'[*] {self.__class__.__name__}: Getting abspath for {bin_name}...')

        if not self.PATH:
            return None
        
        # not all brew-installed binaries are symlinked into the default bin dir (e.g. curl)
        # because it might conflict with a system binary of the same name (e.g. /usr/bin/curl)
        # so we need to check for the binary in the namespaced opt dir and Cellar paths as well
        extra_path = self.PATH.replace('/bin', '/opt/{bin_name}/bin')     # e.g. /opt/homebrew/opt/curl/bin/curl
        search_paths = f'{self.PATH}:{extra_path}'
        
        # add unlinked Cellar paths,e.g. /opt/homebrew/Cellar/curl/8.10.1/bin
        cellar_paths = ':'.join(str(path) for path in (self.brew_prefix / 'Cellar' / bin_name).glob('*/bin'))
        if cellar_paths:
            search_paths += ':' + cellar_paths
        
        abspath = bin_abspath(bin_name, PATH=search_paths)
        if abspath:
            return abspath
        
        if not self.INSTALLER_BIN_ABSPATH:
            return None
        
        # This code works but theres no need, the method above is much faster:
        
        # # try checking filesystem or using brew list to get the Cellar bin path (faster than brew info)
        # for package in (self.get_packages(str(bin_name)) or [str(bin_name)]):
        #     try:
        #         paths = self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=[
        #             'list',
        #             '--formulae',
        #             package,
        #         ], timeout=self._version_timeout, quiet=True).stdout.strip().split('\n')
        #         # /opt/homebrew/Cellar/curl/8.10.1/bin/curl
        #         # /opt/homebrew/Cellar/curl/8.10.1/bin/curl-config
        #         # /opt/homebrew/Cellar/curl/8.10.1/include/curl/ (12 files)
        #         return [line for line in paths if '/Cellar/' in line and line.endswith(f'/bin/{bin_name}')][0].strip()
        #     except Exception:
        #         pass
        
        # # fallback to using brew info to get the Cellar bin path
        # for package in (self.get_packages(str(bin_name)) or [str(bin_name)]):
        #     try:
        #         info_lines = self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=[
        #             'info',
        #             '--quiet',
        #             package,
        #         ], timeout=self._version_timeout, quiet=True).stdout.strip().split('\n')
        #         # /opt/homebrew/Cellar/curl/8.10.0 (530 files, 4MB)
        #         cellar_path = [line for line in info_lines if '/Cellar/' in line][0].rsplit(' (', 1)[0]
        #         abspath = bin_abspath(bin_name, PATH=f'{cellar_path}/bin')
        #         if abspath:
        #             return abspath
        #     except Exception:
        #         pass
        # return None
        

    def default_version_handler(self, bin_name: BinName, abspath: Optional[HostBinPath]=None, **context) -> SemVer | None:
        # print(f'[*] {self.__class__.__name__}: Getting version for {bin_name}...')

        # shortcut: if we already have the Cellar abspath, extract the version from it
        if abspath and '/Cellar/' in str(abspath):
            # /opt/homebrew/Cellar/curl/8.10.1/bin/curl -> 8.10.1
            version = str(abspath).rsplit(f'/bin/{bin_name}', 1)[0].rsplit('/', 1)[-1]
            if version:
                try:
                    return SemVer.parse(version)
                except ValueError:
                    pass

        # fallback to running $ <bin_name> --version
        try:
            version =  super().default_version_handler(bin_name, abspath=abspath, **context)
            if version:
                return SemVer.parse(version)
        except ValueError:
            pass
        
        if not self.INSTALLER_BIN_ABSPATH:
            return None
        
        # fallback to using brew list to get the package version (faster than brew info)
        for package in (self.get_packages(str(bin_name)) or [str(bin_name)]):
            try:
                paths = self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=[
                    'list',
                    '--formulae',
                    package,
                ], timeout=self._version_timeout, quiet=True).stdout.strip().split('\n')
                # /opt/homebrew/Cellar/curl/8.10.1/bin/curl
                cellar_abspath = [line for line in paths if '/Cellar/' in line and line.endswith(f'/bin/{bin_name}')][0].strip()
                # /opt/homebrew/Cellar/curl/8.10.1/bin/curl -> 8.10.1
                version = cellar_abspath.rsplit(f'/bin/{bin_name}', 1)[0].rsplit('/', 1)[-1]
                if version:
                    return SemVer.parse(version)
            except Exception:
                pass
        
        # fallback to using brew info to get the version (slowest method of all)
        packages = self.get_packages(str(bin_name)) or [str(bin_name)]
        main_package = packages[0]   # assume first package in list is the main one
        try:
            version_str = self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=[
                'info',
                '--quiet',
                main_package,
            ], quiet=True, timeout=self._version_timeout).stdout.strip().split('\n')[0]
            # ==> curl: stable 8.10.1 (bottled), HEAD [keg-only]
            return SemVer.parse(version_str)
        except Exception:
            return None
        
        return None

if __name__ == "__main__":
    # Usage:
    # ./binprovider_brew.py load yt-dlp
    # ./binprovider_brew.py install pip
    # ./binprovider_brew.py get_version pip
    # ./binprovider_brew.py get_abspath pip
    result = brew = BrewProvider()

    if len(sys.argv) > 1:
        result = func = getattr(brew, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2:
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)
