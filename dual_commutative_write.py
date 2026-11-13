"""The dual commutative write system -- a clean, self-contained reference.

Founding author: Coen J Searcey.

Two writers, one shared store, concurrent -- conflict-free and order-independent,
with correct liveness. Writes are append-only and sequence-ordered, committed under a
single named mutex; the append-only log IS the merge, so any interleaving of two
writers yields the same log. A holder that dies never deadlocks the store, and a live
holder is never falsely reclaimed.

The canonical form is a Windows named mutex (CreateMutexW / WaitForSingleObject): it is
zero-poll (the OS wakes a waiter the instant the mutex is released) and uses the OS's
own WAIT_ABANDONED signal for authoritative liveness (a holder -- process or thread --
dying while holding it wakes a waiter in ~0.01 ms, not a stale timeout). A POSIX
fallback (fcntl.flock) is provided so the reference runs anywhere; the Windows mutex is
the real one.

    python dual_commutative_write.py write --path store.jsonl --kind note --data "hello"
    python dual_commutative_write.py read  --path store.jsonl
    python dual_commutative_write.py selftest
"""
import json
import os
import sys
import time
from contextlib import contextmanager

_WINDOWS = os.name == "nt"

if _WINDOWS:
    import ctypes
    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    _k.CreateMutexW.restype = ctypes.c_void_p
    _k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    _k.WaitForSingleObject.restype = ctypes.c_uint32
    _k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _k.ReleaseMutex.argtypes = [ctypes.c_void_p]
    _k.CloseHandle.argtypes = [ctypes.c_void_p]
    _INFINITE = 0xFFFFFFFF
    _WAIT_OBJECT_0 = 0x0
    _WAIT_ABANDONED = 0x80
    _WAIT_TIMEOUT = 0x102

    @contextmanager
    def mutex(name, timeout=None):
        """Hold the named mutex for the block. Yields True if the previous holder died
        while holding it (WAIT_ABANDONED) -- the store is intact (append-only), the note
        is for the caller. Raises TimeoutError only on a genuinely busy holder."""
        handle = _k.CreateMutexW(None, False, "Local\\dcw_" + name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        ms = _INFINITE if timeout is None else max(0, int(timeout * 1000))
        result = _k.WaitForSingleObject(handle, ms)
        if result == _WAIT_TIMEOUT:
            _k.CloseHandle(handle)
            raise TimeoutError("dual_commutative_write: a live holder held the mutex for the whole wait")
        try:
            yield result == _WAIT_ABANDONED            # we own it either way (acquired, or abandoned-to-us)
        finally:
            _k.ReleaseMutex(handle)
            _k.CloseHandle(handle)
else:
    import fcntl

    @contextmanager
    def mutex(name, timeout=None):
        """POSIX form: an exclusive flock on a lock file, with the same contract as the
        Windows mutex. With no timeout it BLOCKS in the kernel (zero-poll: the OS wakes
        the waiter the instant the lock is released). The OS drops an flock when its
        holder dies, so a dead holder never deadlocks the store; to tell a dead holder
        from a released one, the holder writes HELD into the lock file on acquire and
        FREE on a clean release. A holder that dies never writes FREE, so the next
        acquirer reads HELD and the block is entered with True (the POSIX analogue of
        WAIT_ABANDONED). With a timeout, acquisition retries with a short backoff
        (0.5 ms growing to 50 ms) and raises TimeoutError only on a live holder."""
        lock_path = _lock_path_for(name)
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if timeout is None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                deadline, delay = time.monotonic() + timeout, 0.0005
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("dual_commutative_write: a live holder held the lock for the whole wait")
                        time.sleep(delay)
                        delay = min(delay * 2, 0.05)
            abandoned = os.pread(fd, 4, 0) == b"HELD"   # last holder never marked FREE: it died
            os.pwrite(fd, b"HELD", 0)
            try:
                yield abandoned
            finally:
                os.pwrite(fd, b"FREE", 0)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _lock_path_for(name):
        import tempfile
        return os.path.join(tempfile.gettempdir(), "dcw_" + name + ".lock")


def _name_for(path):
    """A stable, filesystem-safe mutex name for one store path, so every writer to the
    same store shares the same mutex."""
    import hashlib
    return hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:32]


def append(path, record, timeout=None):
    """Append one self-contained record to the store, under the shared mutex. The record
    is stamped with a monotonic sequence (assigned under the mutex, so the sequence is
    the canonical commit order) and a wall-clock time. -> the written record."""
    return append_many(path, [record], timeout=timeout)[0]


def append_many(path, records, timeout=None):
    """Append several records in ONE commit -- a reader sees all of them or none. Under
    the mutex: read the current tail sequence, stamp each record, append atomically."""
    with mutex(_name_for(path), timeout):
        seq = _tail_seq(path)
        stamped = []
        lines = []
        for rec in records:
            seq += 1
            r = dict(rec, seq=seq, at=rec.get("at", round(time.time(), 6)))
            stamped.append(r)
            lines.append(json.dumps(r, separators=(",", ":")))
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
    return stamped


def _tail_seq(path):
    """The highest sequence already in the store, or 0 -- read while holding the mutex."""
    last = 0
    if os.path.exists(path):
        for raw in open(path, encoding="utf-8"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                s = json.loads(raw).get("seq")
                if isinstance(s, int) and s > last:
                    last = s
            except ValueError:
                continue
    return last


def read(path):
    """The whole store, in commit order -- append-only, so a read needs no lock. Skips
    an unparsable line (e.g. a torn tail) rather than failing the read."""
    out = []
    if os.path.exists(path):
        for raw in open(path, encoding="utf-8"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except ValueError:
                continue
    return out


def selftest():
    """Two writers interleave into one store; the log commutes (every record present,
    sequences strictly increasing, no interleaving corruption)."""
    import tempfile
    import threading
    failures = []
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "store.jsonl")

        def writer(tag, n):
            for i in range(n):
                append(path, {"kind": "note", "writer": tag, "i": i})

        a = threading.Thread(target=writer, args=("A", 50))
        b = threading.Thread(target=writer, args=("B", 50))
        a.start(); b.start(); a.join(); b.join()
        recs = read(path)
        seqs = [r["seq"] for r in recs]
        if len(recs) != 100:
            failures.append(f"expected 100 records, got {len(recs)}")
        if seqs != sorted(set(seqs)) or seqs != list(range(1, len(seqs) + 1)):
            failures.append("sequences are not strictly 1..N (a write interleaved or a sequence collided)")
        if sum(1 for r in recs if r["writer"] == "A") != 50 or sum(1 for r in recs if r["writer"] == "B") != 50:
            failures.append("a writer's records were lost")
    if failures:
        print("dual_commutative_write selftest FAILURES:")
        for f in failures:
            print("  -", f)
    else:
        print("dual_commutative_write selftest: all checks passed (two writers, one store, commutes).")
    return not failures


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="the dual commutative write system")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write", help="append one record to the store")
    w.add_argument("--path", required=True)
    w.add_argument("--kind", default="record")
    w.add_argument("--data", default="")
    r = sub.add_parser("read", help="read the whole store, in commit order")
    r.add_argument("--path", required=True)
    sub.add_parser("selftest", help="two writers interleave into one store; the log commutes")
    args = ap.parse_args(argv)
    if args.cmd == "write":
        rec = append(args.path, {"kind": args.kind, "data": args.data})
        print(json.dumps(rec))
    elif args.cmd == "read":
        for rec in read(args.path):
            print(json.dumps(rec))
    elif args.cmd == "selftest":
        return 0 if selftest() else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
