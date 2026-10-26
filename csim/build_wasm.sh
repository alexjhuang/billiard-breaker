#!/bin/sh
# Build web/bbm.wasm from csim/bbm.c with the wasi-sdk (https://github.com/WebAssembly/wasi-sdk/releases):
#   WASI_SDK=/path/to/wasi-sdk-34.0-x86_64-linux sh csim/build_wasm.sh
# The module is self-contained (no imports); web/bbm.wasm.src records the source it was built from.
set -e
cd "$(dirname "$0")/.."
: "${WASI_SDK:?set WASI_SDK to the wasi-sdk directory}"
EXPORTS=world_new,world_free,world_add,world_run,world_run_trace,world_count,world_broken,world_time,world_set_time,world_cells,malloc,free
"$WASI_SDK/bin/wasm32-wasip1-clang" -O3 -DBBM_NO_THREADS -mexec-model=reactor \
    $(echo ",$EXPORTS" | sed 's/,/ -Wl,--export=/g') -o web/bbm.wasm csim/bbm.c
sha1sum csim/bbm.c | cut -c1-40 > web/bbm.wasm.src
ls -l web/bbm.wasm
