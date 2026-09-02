#!/bin/bash
set -o errexit -o nounset
trap 'echo "Exit status $? at line $LINENO from: $BASH_COMMAND"' ERR

VERSION=$1
VER_REGEX="v[0-9]+\.[0-9]+\.[0-9]+"
if [[ ! "$VERSION" =~ $VER_REGEX ]]; then
    echo Invalid version
    exit 1
fi

./build.sh

git tag -a "$VERSION" -m "$VERSION"
git push origin
git push origin --tags
