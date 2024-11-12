#!/usr/bin/env python

__package__ = "abx_pkg"

import os
import sys
import site
import shutil
import sysconfig
import subprocess
import tempfile
from platformdirs import user_cache_path

from pathlib import Path
from typing import Optional, List, Set
from typing_extensions import Self
from pydantic import model_validator, TypeAdapter, computed_field

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs, HostBinPath, bin_abspath, bin_abspaths
from .semver import SemVer
from .binprovider import BinProvider, DEFAULT_ENV_PATH

ACTIVE_VENV = os.getenv('VIRTUAL_ENV', None)
_CACHED_GLOBAL_PIP_BIN_DIRS: Set[str] | None = None


USER_CACHE_PATH = Path(tempfile.gettempdir()) / 'pip-cache'
try:    
    user_cache_path = user_cache_path(appname='pip', appauthor='abx-pkg', ensure_exists=True)
    if os.access(user_cache_path, os.W_OK):
        USER_CACHE_PATH = user_cache_path
except Exception:
    pass


class PipProvider(BinProvider):
    name: BinProviderName = "pip"
    INSTALLER_BIN: BinName = "pip"
    
    PATH: PATHStr = ''
    
    pip_venv: Optional[Path] = None                                                         # None = system site-packages (user or global), otherwise it's a path e.g. DATA_DIR/lib/pip/venv
    
    cache_dir: Path = USER_CACHE_PATH
    cache_arg: str = f'--cache-dir={cache_dir}'
    
    pip_install_args: List[str] = ["--no-input", "--disable-pip-version-check", "--quiet"]  # extra args for pip install ... e.g. --upgrade

    _INSTALLER_BIN_ABSPATH: HostBinPath | None = None   # speed optimization only, faster to cache the abspath than to recompute it on every access

    @computed_field
    @property
    def is_valid(self) -> bool:
        """False if pip_venv is not created yet or if pip binary is not found in PATH"""
        if self.pip_venv:
            venv_pip_path = self.pip_venv / "bin" / "python"
            venv_pip_binary_exists = (os.path.isfile(venv_pip_path) and os.access(venv_pip_path, os.X_OK))
            if not venv_pip_binary_exists:
                return False
        
        return bool(self.INSTALLER_BIN_ABSPATH)

    @computed_field
    @property
    def INSTALLER_BIN_ABSPATH(self) -> HostBinPath | None:
        """Actual absolute path of the underlying package manager (e.g. /usr/local/bin/npm)"""
        if self._INSTALLER_BIN_ABSPATH:
            # return cached value if we have one
            return self._INSTALLER_BIN_ABSPATH
        
        abspath = None

        if self.pip_venv:
            assert self.INSTALLER_BIN != 'pipx', "Cannot use pipx with pip_venv"
            
            # use venv pip
            venv_pip_path = self.pip_venv / "bin" / self.INSTALLER_BIN
            if os.path.isfile(venv_pip_path) and os.access(venv_pip_path, os.R_OK) and os.access(venv_pip_path, os.X_OK):
                abspath = str(venv_pip_path)
        else:
            # use system pip
            relpath = bin_abspath(self.INSTALLER_BIN, PATH=DEFAULT_ENV_PATH) or shutil.which(self.INSTALLER_BIN)
            abspath = relpath and Path(relpath).resolve()  # find self.INSTALLER_BIN abspath using environment path
        
        if not abspath:
            # underlying package manager not found on this host, return None
            return None
        valid_abspath = TypeAdapter(HostBinPath).validate_python(abspath)
        if valid_abspath:
            # if we found a valid abspath, cache it
            self._INSTALLER_BIN_ABSPATH = valid_abspath
        return valid_abspath

    @model_validator(mode='after')
    def detect_euid_to_use(self) -> Self:
        """Detect the user (UID) to run as when executing pip (should be same as the user that owns the pip_venv dir)"""
        
        if self.euid is None:
            # try dropping to the owner of the npm prefix dir if it exists
            if self.pip_venv and os.path.isdir(self.pip_venv):
                self.euid = os.stat(self.pip_venv).st_uid

            # try dropping to the owner of the npm binary if it's not root
            installer_bin = self.INSTALLER_BIN_ABSPATH
            if installer_bin:
                self.euid = self.euid or os.stat(installer_bin).st_uid
                
            # fallback to the currently running user
            self.euid = self.euid or os.geteuid()
                    
        return self

    @model_validator(mode="after")
    def load_PATH_from_pip_sitepackages(self) -> Self:
        """Assemble PATH from pip_venv, pipx, or autodetected global python system site-packages and user site-packages"""
        global _CACHED_GLOBAL_PIP_BIN_DIRS
        PATH = self.PATH

        pip_bin_dirs = set()
        
        if self.pip_venv:
            # restrict PATH to only use venv bin path
            pip_bin_dirs = {str(self.pip_venv / "bin")}
            
        elif self.INSTALLER_BIN == "pipx":
            # restrict PATH to only use global pipx bin path
            pipx_abspath = self.INSTALLER_BIN_ABSPATH
            if pipx_abspath:
                proc = self.exec(bin_name=pipx_abspath, cmd=["environment"], quiet=True, timeout=self._version_timeout)     # run $ pipx environment
                if proc.returncode == 0:
                    PIPX_BIN_DIR = proc.stdout.strip().split("PIPX_BIN_DIR=")[-1].split("\n", 1)[0]
                    pip_bin_dirs = {PIPX_BIN_DIR}
        else:
            # autodetect global system python paths
            
            if _CACHED_GLOBAL_PIP_BIN_DIRS:
                pip_bin_dirs = _CACHED_GLOBAL_PIP_BIN_DIRS.copy()
            else:
                pip_bin_dirs = {
                    * (
                        str(Path(sitepackage_dir).parent.parent.parent / "bin")               # /opt/homebrew/opt/python@3.11/Frameworks/Python.framework/Versions/3.11/bin
                        for sitepackage_dir in site.getsitepackages()
                    ),
                    str(Path(site.getusersitepackages()).parent.parent.parent / "bin"),       # /Users/squash/Library/Python/3.9/bin
                    sysconfig.get_path("scripts"),                                            # /opt/homebrew/bin
                    str(Path(sys.executable).resolve().parent),                               # /opt/homebrew/Cellar/python@3.11/3.11.9/Frameworks/Python.framework/Versions/3.11/bin
                }
                
                # find every python installed in the system PATH and add their parent path, as that's where its corresponding pip will link global bins
                for abspath in bin_abspaths("python", PATH=DEFAULT_ENV_PATH):                 # ~/Library/Frameworks/Python.framework/Versions/3.10/bin
                    pip_bin_dirs.add(str(abspath.parent))
                for abspath in bin_abspaths("python3", PATH=DEFAULT_ENV_PATH):                # /usr/local/bin or anywhere else we see python3 in $PATH
                    pip_bin_dirs.add(str(abspath.parent))
                
                _CACHED_GLOBAL_PIP_BIN_DIRS = pip_bin_dirs.copy()
            
            # remove any active venv from PATH because we're trying to only get the global system python paths
            if ACTIVE_VENV:
                pip_bin_dirs.remove(f"{ACTIVE_VENV}/bin")

        for bin_dir in pip_bin_dirs:
            if bin_dir not in PATH:
                PATH = ":".join([*PATH.split(":"), bin_dir])
        self.PATH = TypeAdapter(PATHStr).validate_python(PATH)
        return self
    
    def setup(self):
        """create pip venv dir if needed"""
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            os.system(f'chown {self.EUID} "{self.cache_dir}" 2>/dev/null') # try to ensure cache dir is writable by EUID
            os.system(f'chmod 777 "{self.cache_dir}" 2>/dev/null')         # allow all users to share cache dir
        except Exception:
            self.cache_arg = '--no-cache-dir'
        
        if self.pip_venv:
            self._pip_setup_venv(self.pip_venv)

    def _pip_setup_venv(self, pip_venv: Path):
        pip_venv.parent.mkdir(parents=True, exist_ok=True)
                    
        # create new venv in pip_venv if it doesnt exist
        venv_pip_path = pip_venv / "bin" / "python"
        venv_pip_binary_exists = (os.path.isfile(venv_pip_path) and os.access(venv_pip_path, os.X_OK))
        if not venv_pip_binary_exists:
            import venv
            
            venv.create(
                str(pip_venv),
                system_site_packages=False,
                clear=True,
                symlinks=True,
                with_pip=True,
                upgrade_deps=True,
            )
            assert os.path.isfile(venv_pip_path) and os.access(venv_pip_path, os.X_OK), f'could not find pip inside venv after creating it: {pip_venv}'
            self.exec(bin_name=venv_pip_path, cmd=["install", self.cache_arg, "--upgrade", "pip", "setuptools"])   # setuptools is not installed by default after python >= 3.12

    def _pip_show(self, bin_name: BinName, packages: Optional[InstallArgs] = None) -> List[str]:
        pip_abspath = self.INSTALLER_BIN_ABSPATH
        if not pip_abspath:
            raise Exception(
                f"{self.__class__.__name__} install method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)"
            )
        
        packages = packages or self.get_packages(str(bin_name)) or [str(bin_name)]
        main_package = packages[0]  # assume first package in list is the main one
        output_lines = self.exec(bin_name=pip_abspath, cmd=[
            'show',
            '--no-input',
            main_package,
        ], timeout=self._version_timeout, quiet=True).stdout.strip().split('\n')
        return output_lines
    
    def _pip_install(self, packages: InstallArgs) -> subprocess.CompletedProcess:
        pip_abspath = self.INSTALLER_BIN_ABSPATH
        if not pip_abspath:
            raise Exception(
                f"{self.__class__.__name__} install method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)"
            )
        
        return self.exec(bin_name=pip_abspath, cmd=[
            'install',
            '--no-input',
            self.cache_arg,
            *self.pip_install_args,
            *packages,
        ])
        
    
    def default_install_handler(self, bin_name: str, packages: Optional[InstallArgs] = None, **context) -> str:
        if self.pip_venv:
            self.setup()
        
        packages = packages or self.get_packages(bin_name)
        
        # print(f'[*] {self.__class__.__name__}: Installing {bin_name}: {self.INSTALLER_BIN_ABSPATH} install {packages}')

        # pip install --no-input --cache-dir=<cache_dir> <extra_pip_args> <packages>
        proc = self._pip_install(packages)

        if proc.returncode != 0:
            print(proc.stdout.strip())
            print(proc.stderr.strip())
            raise Exception(f"{self.__class__.__name__}: install got returncode {proc.returncode} while installing {packages}: {packages}")

        return proc.stderr.strip() + "\n" + proc.stdout.strip()

    def default_abspath_handler(self, bin_name: BinName, **context) -> HostBinPath | None:
        
        # try searching for the bin_name in BinProvider.PATH first (fastest)
        try:
            abspath = super().default_abspath_handler(bin_name, **context)
            if abspath:
                return TypeAdapter(HostBinPath).validate_python(abspath)
        except ValueError:
            pass
        
        # fallback to using pip show to get the site-packages bin path
        output_lines = self._pip_show(bin_name)
        # For more information, please refer to <http://unlicense.org/>
        # Location: /Volumes/NVME/Users/squash/Library/Python/3.11/lib/python/site-packages
        # Requires: brotli, certifi, mutagen, pycryptodomex, requests, urllib3, websockets
        # Required-by:
        try:
            location = [line for line in output_lines if line.startswith('Location: ')][0].split('Location: ', 1)[-1]
        except IndexError:
            return None
        PATH = str(Path(location).parent.parent.parent / 'bin')
        abspath = bin_abspath(str(bin_name), PATH=PATH)
        if abspath:
            return TypeAdapter(HostBinPath).validate_python(abspath)
        else:
            return None
    
    def default_version_handler(self, bin_name: BinName, abspath: Optional[HostBinPath]=None, **context) -> SemVer | None:
        # print(f'[*] {self.__class__.__name__}: Getting version for {bin_name}...')
        
        # try running <bin_name> --version first (fastest)
        try:
            version =  super().default_version_handler(bin_name, abspath, **context)
            if version:
                return SemVer.parse(version)
        except ValueError:
            pass
        
        # fallback to using pip show to get the version (slower)
        output_lines = self._pip_show(bin_name)
        # Name: yt-dlp
        # Version: 1.3.0
        # Location: /Volumes/NVME/Users/squash/Library/Python/3.11/lib/python/site-packages
        try:
            version_str = [line for line in output_lines if line.startswith('Version: ')][0].split('Version: ', 1)[-1]
            return SemVer.parse(version_str)
        except Exception:
            return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_pip.py load yt-dlp
    # ./binprovider_pip.py install pip
    # ./binprovider_pip.py get_version pip
    # ./binprovider_pip.py get_abspath pip
    result = pip = PipProvider()

    if len(sys.argv) > 1:
        result = func = getattr(pip, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2:
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)
