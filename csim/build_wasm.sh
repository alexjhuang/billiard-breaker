#!/bin/sh
# Build web/bbm.wasm (one thread) and web/bbmt.wasm (threads: shared memory, needs a
# cross-origin-isolated page) from csim/bbm.c with the wasi-sdk
# (https://github.com/WebAssembly/wasi-sdk/releases):
#   WASI_SDK=/path/to/wasi-sdk-34.0-x86_64-linux sh csim/build_wasm.sh
# web/bbm.wasm.src records the source they were built from (web.py --export warns when stale).
set -e
cd "$(dirname "$0")/.."
: "${WASI_SDK:?set WASI_SDK to the wasi-sdk directory}"
EXPORTS=world_new,world_free,world_add,world_run,world_run_trace,world_count,world_broken,world_time,world_set_time,world_cells,malloc,free
EXP=$(echo ",$EXPORTS" | sed 's/,/ -Wl,--export=/g')
"$WASI_SDK/bin/wasm32-wasip1-clang" -O3 -DBBM_NO_THREADS -mexec-model=reactor $EXP -o web/bbm.wasm csim/bbm.c
# the memory is imported from web/wasm.js: 64 MiB initially (must match), 1 GiB at most
"$WASI_SDK/bin/wasm32-wasip1-threads-clang" -O3 -pthread -mexec-model=reactor $EXP -Wl,--export=world_run_tiles \
    -Wl,--import-memory,--shared-memory,--initial-memory=67108864,--max-memory=1073741824 -o web/bbmt.wasm csim/bbm.c
sha1sum csim/bbm.c | cut -c1-40 > web/bbm.wasm.src
ls -l web/bbm.wasm web/bbmt.wasm
