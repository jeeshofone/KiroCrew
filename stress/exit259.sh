#!/usr/bin/env bash
# Stress the 259 drain test under -n 4 with CPU spinners, as a variant.
# usage: exit259.sh <label> <rounds>
set -u
label=$1
rounds=$2
copies=24
for i in $(seq 1 $copies); do
  cp test/test_runtime_cleanup_windows.py "test/test_stress259_${i}.py"
done
nproc=$(python -c "import os; print(os.cpu_count())")
spinners=()
for _ in $(seq 1 "$nproc"); do
  python -c "while True: pass" &
  spinners+=($!)
done
pass=0
fail=0
for r in $(seq 1 "$rounds"); do
  python -m pytest -p no:cacheprovider -n 4 --max-worker-restart=0 -q -o addopts= \
    -k "exiting_259" test/test_stress259_*.py >"stress-${label}-${r}.log" 2>&1
  p=$(grep -oE '[0-9]+ passed' "stress-${label}-${r}.log" | grep -oE '[0-9]+' | tail -1)
  f=$(grep -oE '[0-9]+ failed' "stress-${label}-${r}.log" | grep -oE '[0-9]+' | tail -1)
  pass=$((pass + ${p:-0}))
  fail=$((fail + ${f:-0}))
  grep -E "^(FAILED|ERROR)" "stress-${label}-${r}.log" | sed 's/^/  /' | sort | uniq -c | head -5
done
kill "${spinners[@]}" 2>/dev/null
rm -f test/test_stress259_*.py
echo "RESULT ${label}: passed=${pass} failed=${fail} (cpus=${nproc}, spinners=${nproc}, rounds=${rounds}, copies=${copies})" | tee -a stress-summary.txt
