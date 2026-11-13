"""A holder killed while it holds the mutex must not deadlock the store, the next
writer must be told the previous holder died, and the log must stay intact.

    python test_abandoned.py
"""
import os
import subprocess
import sys
import tempfile
import time

import dual_commutative_write as dcw

HOLDER = """
import sys, time
import dual_commutative_write as dcw
path = sys.argv[1]
with dcw.mutex(dcw._name_for(path)):
    print('held', flush=True)
    time.sleep(60)
"""


def main():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "store.jsonl")
        dcw.append(path, {"kind": "note", "data": "before"})

        p = subprocess.Popen([sys.executable, "-c", HOLDER, path],
                             stdout=subprocess.PIPE, text=True,
                             cwd=os.path.dirname(os.path.abspath(__file__)))
        assert p.stdout.readline().strip() == "held", "holder never acquired the mutex"
        p.kill()          # SIGKILL on POSIX, TerminateProcess on Windows: no clean release
        p.wait()

        t0 = time.monotonic()
        with dcw.mutex(dcw._name_for(path), timeout=5) as abandoned:
            waited = time.monotonic() - t0
        assert abandoned is True, "next writer was not told the holder died"

        dcw.append(path, {"kind": "note", "data": "after"}, timeout=5)
        seqs = [r["seq"] for r in dcw.read(path)]
        assert seqs == [1, 2], f"log damaged: {seqs}"

        with dcw.mutex(dcw._name_for(path), timeout=5) as abandoned:
            pass
        assert abandoned is False, "a clean release was reported as abandoned"
    print(f"test_abandoned: ok (waited {waited * 1000:.1f} ms after the kill)")


if __name__ == "__main__":
    main()
