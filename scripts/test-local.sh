#!/usr/bin/env sh
set -eu

# Keep the legacy entry point, with exactly the same isolation and cleanup.
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec sh "$script_dir/test.sh" "$@"
