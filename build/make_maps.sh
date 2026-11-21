#!/bin/sh
# Rebuild maps/maps.pkl. Run from this repository with a sqlglot checkout at
# eb11aec4 in $SQ and a Python 3.13 environment with requirements.lock in $PY.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
TESTS_JSON=${TESTS_JSON:-tests.json}   # the event's tests.json
OUT=${OUT:-/tmp/brmaps}
mkdir -p "$OUT"
cd "$SQ"
$PY -m compileall -q sqlglot tests
PYTHONPATH=.:$HERE:$HERE/.. TRACE_OUT=$OUT/fwd.pkl $PY -m pytest -q -p trace_plugin -p no:cacheprovider tests
TRACE_REVERSE=1 PYTHONPATH=.:$HERE:$HERE/.. TRACE_OUT=$OUT/rev.pkl $PY -m pytest -q -p trace_plugin -p no:cacheprovider tests || true
$PY $HERE/test_imports.py . $OUT/test_imports.json "$TESTS_JSON"
$PY $HERE/measure_local.py . "$TESTS_JSON" $OUT/local_cpu.json
$PY $HERE/static_info.py . "$TESTS_JSON" $OUT/static.json
PYTHONPATH=. $PY $HERE/build_maps.py $OUT/fwd.pkl,$OUT/rev.pkl "$TESTS_JSON" . $HERE/../maps/maps.pkl \
    $OUT/test_imports.json $OUT/local_cpu.json $OUT/static.json
xz -9 -f $HERE/../maps/maps.pkl
