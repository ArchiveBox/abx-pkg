
#!/usr/bin/env python3

__package__ = "abx_pkg"

import os
import sys
import json
import tempfile

from pathlib import Path
from typing import Optional, List
from typing_extensions import Self

from pydantic import model_validator, TypeAdapter, computed_field
from platformdirs import user_cache_path

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs, HostBinPath, bin_abspath
from .semver import SemVer
from .binprovider import BinProvider

# Cache these values globally because they never change at runtime
_CACHED_GLOBAL_NPM_PREFIX: Path | None = None
_CACHED_HOME_DIR: Path = Path('~').expanduser().absolute()


USER_CACHE_PATH = Path(tempfile.gettempdir()) / 'npm-cache'
try:    
    user_cache_path = user_cache_path(appname='npm', appauthor='abx-pkg', ensure_exists=True)
    if os.access(user_cache_path, os.W_OK):
        USER_CACHE_PATH = user_cache_path
except Exception:
    pass


class NpmProvider(BinProvider):
    name: BinProviderName = 'npm'
    INSTALLER_BIN: BinName = 'npm'

    PATH: PATHStr = ''
    
    npm_prefix: Optional[Path] = None                           # None = -g global, otherwise it's a path
    
    cache_dir: Path = USER_CACHE_PATH
    cache_arg: str = f'--cache={cache_dir}'
    
    npm_install_args: List[str] = ['--force', '--no-audit', '--no-fund', '--loglevel=error']

    _CACHED_LOCAL_NPM_PREFIX: Path | None = None

    @computed_field
    @property
    def is_valid(self) -> bool:
        """False if npm_prefix is not created yet or if npm binary is not found in PATH"""
        if self.npm_prefix:
            npm_bin_dir = self.npm_prefix / 'node_modules' / '.bin'
            npm_bin_dir_exists = (os.path.isdir(npm_bin_dir) and os.access(npm_bin_dir, os.R_OK))
            if not npm_bin_dir_exists:
                return False
        
        return bool(self.INSTALLER_BIN_ABSPATH)
    
    @model_validator(mode='after')
    def detect_euid_to_use(self) -> Self:
        """Detect the user (UID) to run as when executing npm (should be same as the user that owns the npm_prefix dir)"""
        if self.euid is None:
            # try dropping to the owner of the npm prefix dir if it exists
            if self.npm_prefix and os.path.isdir(self.npm_prefix):
                self.euid = os.stat(self.npm_prefix).st_uid

            # try dropping to the owner of the npm binary if it's not root
            installer_bin = self.INSTALLER_BIN_ABSPATH
            if installer_bin:
                self.euid = self.euid or os.stat(installer_bin).st_uid
                
            # fallback to the currently running user
            self.euid = self.euid or os.geteuid()
                    
        return self

    @model_validator(mode='after')
    def load_PATH_from_npm_prefix(self) -> Self:
        self.PATH = self._load_PATH()
        return self
    
    def _load_PATH(self) -> str:
        PATH = self.PATH
        npm_bin_dirs: set[Path] = set()
        global _CACHED_GLOBAL_NPM_PREFIX
        
        if self.npm_prefix:
            # restrict PATH to only use npm prefix
            npm_bin_dirs = {self.npm_prefix / 'node_modules/.bin'}
        
        if self.INSTALLER_BIN_ABSPATH:
            # find all local and global npm PATHs
            npm_local_dir = self._CACHED_LOCAL_NPM_PREFIX or self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=['prefix'], quiet=True).stdout.strip()
            self._CACHED_LOCAL_NPM_PREFIX = npm_local_dir

            # start at npm_local_dir and walk up to $HOME (or /), finding all npm bin dirs along the way
            search_dir = Path(npm_local_dir)
            stop_if_reached = [str(Path('/')), str(_CACHED_HOME_DIR)]
            num_hops, max_hops = 0, 6
            while num_hops < max_hops and str(search_dir) not in stop_if_reached:
                try:
                    npm_bin_dirs.add(list(search_dir.glob('node_modules/.bin'))[0])
                    break
                except (IndexError, OSError, Exception):
                    # could happen becuase we dont have permission to access the parent dir, or it's been moved, or many other weird edge cases...
                    pass
                search_dir = search_dir.parent
                num_hops += 1
            
            npm_global_dir = _CACHED_GLOBAL_NPM_PREFIX or self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=['prefix', '-g'], quiet=True).stdout.strip() + '/bin'    # /opt/homebrew/bin
            _CACHED_GLOBAL_NPM_PREFIX = npm_global_dir
            npm_bin_dirs.add(npm_global_dir)
        
        for bin_dir in npm_bin_dirs:
            if str(bin_dir) not in PATH:
                PATH = ':'.join([*PATH.split(':'), str(bin_dir)])
        return TypeAdapter(PATHStr).validate_python(PATH)

    def setup(self) -> None:
        """create npm install prefix and node_modules_dir if needed"""
        if not self.PATH or not self._CACHED_LOCAL_NPM_PREFIX:
            self.PATH = self._load_PATH()
            
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            os.system(f'chown {self.EUID} "{self.cache_dir}"')
            os.system(f'chmod 777 "{self.cache_dir}"')     # allow all users to share cache dir
        except Exception:
            self.cache_arg = '--no-cache'
        
        if self.npm_prefix:
            (self.npm_prefix / 'node_modules/.bin').mkdir(parents=True, exist_ok=True)

    def default_install_handler(self, bin_name: str, packages: Optional[InstallArgs]=None, **context) -> str:
        self.setup()
        
        packages = packages or self.get_packages(bin_name)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(f'{self.__class__.__name__} install method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)')
        
        # print(f'[*] {self.__class__.__name__}: Installing {bin_name}: {self.INSTALLER_BIN_ABSPATH} install {packages}')
        
        install_args = [*self.npm_install_args, self.cache_arg]
        if self.npm_prefix:
            install_args.append(f'--prefix={self.npm_prefix}')
        else:
            install_args.append('--global')
        
        proc = self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=[
            "install",
            *install_args,
            *packages,
        ])
        
        if proc.returncode != 0:
            print(proc.stdout.strip())
            print(proc.stderr.strip())
            raise Exception(f'{self.__class__.__name__}: install got returncode {proc.returncode} while installing {packages}: {packages}')
        
        return (proc.stderr.strip() + '\n' + proc.stdout.strip()).strip()
    
    def default_abspath_handler(self, bin_name: BinName, **context) -> HostBinPath | None:
        # print(self.__class__.__name__, 'on_get_abspath', bin_name)
        
        # try searching for the bin_name in BinProvider.PATH first (fastest)
        try:
            abspath = super().default_abspath_handler(bin_name, **context)
            if abspath:
                return TypeAdapter(HostBinPath).validate_python(abspath)
        except Exception:
            pass
        
        if not self.INSTALLER_BIN_ABSPATH:
            return None
        
        # fallback to using npm show to get alternate binary names based on the package, then try to find those in BinProvider.PATH
        try:
            packages = self.get_packages(str(bin_name)) or [str(bin_name)]
            main_package = packages[0]   # assume first package in list is the main one
            output_lines = self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=[
                'show',
                '--json',
                main_package,
            ], timeout=self._version_timeout, quiet=True).stdout.strip().split('\n')
            # { ...
            #   "version": "2.2.3",
            #   "bin": {
            #     "mercury-parser": "cli.js",
            #     "postlight-parser": "cli.js"
            #   },
            #   ...
            # }
            alt_bin_names = json.loads(output_lines[0])['bin'].keys()
            for alt_bin_name in alt_bin_names:
                abspath = bin_abspath(alt_bin_name, PATH=self.PATH)
                if abspath:
                    return TypeAdapter(HostBinPath).validate_python(abspath)
        except Exception:
            pass        
        return None
    
    def default_version_handler(self, bin_name: BinName, abspath: Optional[HostBinPath]=None, **context) -> SemVer | None:
        # print(f'[*] {self.__class__.__name__}: Getting version for {bin_name}...')
        try:
            version = super().default_version_handler(bin_name, abspath, **context)
            if version:
                return SemVer.parse(version)
        except ValueError:
            pass
        
        if not self.INSTALLER_BIN_ABSPATH:
            return None
        
        # fallback to using npm list to get the installed package version
        try:
            packages = self.get_packages(str(bin_name), **context) or [str(bin_name)]
            main_package = packages[0]  # assume first package in list is the main one
            
            # remove the package version if it exists "@postslight/parser@^1.2.3" -> "@postlight/parser"
            if main_package[0] == '@':
                package = '@' + main_package[1:].split('@', 1)[0]
            else:
                package = main_package.split('@', 1)[0]
                
            # npm list --depth=0 --json --prefix=<prefix> "@postlight/parser"
            # (dont use 'npm info @postlight/parser version', it shows *any* availabe version, not installed version)
            json_output = self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=[
                'list',
                f'--prefix={self.npm_prefix}' if self.npm_prefix else '--global',
                '--depth=0',
                '--json',
                package,
            ], timeout=self._version_timeout, quiet=True).stdout.strip()
            # {
            #   "name": "lib",
            #   "dependencies": {
            #     "@postlight/parser": {
            #       "version": "2.2.3",
            #       "overridden": false
            #     }
            #   }
            # }
            version_str = json.loads(json_output)['dependencies'][package]['version']
            return SemVer.parse(version_str)
        except Exception:
            raise
        return None

if __name__ == "__main__":
    # Usage:
    # ./binprovider_npm.py load @postlight/parser
    # ./binprovider_npm.py install @postlight/parser
    # ./binprovider_npm.py get_version @postlight/parser
    # ./binprovider_npm.py get_abspath @postlight/parser
    result = npm = NpmProvider()

    if len(sys.argv) > 1:
        result = func = getattr(npm, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2:
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)
