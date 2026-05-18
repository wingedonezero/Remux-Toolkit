#!/usr/bin/env bash
#
# Build the libmkv_shim shared library, output next to source.
#
# Depends on Debian packages:  libebml-dev  libmatroska-dev
# Headers expected at:  /usr/include/{ebml,matroska}/
#
# Usage:  build.sh  [debug]
#   - debug:  build with -O0 -g for gdb-friendly diagnostics
#

set -euo pipefail
cd "$(dirname "$0")"

CXX="${CXX:-g++}"
MODE="${1:-release}"

if [[ "$MODE" == "debug" ]]; then
    OPT="-O0 -g"
else
    OPT="-O2"
fi

CXXFLAGS="${CXXFLAGS:--std=c++17 -fPIC -shared $OPT -Wall -Wextra -Wno-deprecated-declarations}"

# Header pre-check — print a clear message if dev packages aren't installed.
required=(/usr/include/ebml/EbmlHead.h /usr/include/matroska/KaxSegment.h)
missing=()
for h in "${required[@]}"; do
    [[ -f "$h" ]] || missing+=("$h")
done
if [[ ${#missing[@]} -gt 0 ]]; then
    echo "ERROR: missing C++ header(s):"
    for h in "${missing[@]}"; do echo "    $h"; done
    echo
    echo "Install the dev packages:"
    echo "    sudo apt install libebml-dev libmatroska-dev"
    exit 2
fi

OUT="libmkv_shim.so"
SRC="libmkv_shim.cpp"

echo "Building $OUT from $SRC (mode=$MODE) ..."
# shellcheck disable=SC2086
$CXX $CXXFLAGS "$SRC" -o "$OUT" -lebml -lmatroska
echo "Built: $(pwd)/$OUT ($(stat -c '%s' "$OUT") bytes)"
