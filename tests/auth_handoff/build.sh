#!/bin/sh
set -eu
src=$(cd "$(dirname "$0")/../.." && pwd)
build=${1:-/tmp/icecast-auth-handoff}
rm -rf "$build"
mkdir -p "$build"
cd "$build"
"$src/configure" --with-curl --with-openssl \
    CFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer ${AUTH_HANDOFF_TEST_DELAY:+-DAUTH_HANDOFF_TEST_DELAY=$AUTH_HANDOFF_TEST_DELAY}" \
    LDFLAGS='-fsanitize=address'
make -j"${JOBS:-2}"
printf '%s\n' "$build/src/icecast"
