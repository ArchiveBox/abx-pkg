#!/usr/bin/env bash

if [[ "$1" == "" ]]; then
    echo "Usage: $0 <target_version>"
    exit 1
fi

echo "Bumping to version $1"

rm -Rf dist 2>/dev/null
nano pyproject.toml
git add -p .
git commit -m "bump version to $1"
git tag -f -a "v$1" -m "v$1"
git push origin
git push origin --tags -f
uv lock
uv sync
uv build
uv publish
