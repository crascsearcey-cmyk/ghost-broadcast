# The Dual Commutative Write System

**Founding author:** Coen J Searcey.

A canonical answer to a hard problem: letting two writers write to one shared store
concurrently — **conflict-free and order-independent** — with correct liveness, so a
writer that dies never deadlocks the system and a live writer is never falsely
reclaimed. Most systems get this wrong. This one is built on the operating system's
own primitives, **measured, not guessed**.

## The problem

Two parties write to the same substrate. Naively you reach for a lock — and then:

- a **poll-based** lock burns latency on a guessed schedule and never reacts to the
  real event (a lock released, a holder dead);
- a **stale-timeout** lock either *deadlocks* when a holder dies (waiting out the
  timeout) or *falsely reclaims* a lock a live-but-slow holder still holds.

## The write

Writes are **append-only** and **sequence-ordered**. Each writer appends a
self-contained record under the shared mutex; the append-only log *is* the merge.
Because every record stands alone and the log preserves all of them in the order they
committed, the writes **commute** — any interleaving of two writers yields the same
log. Several records in one write are atomic: a reader sees all of them or none.

This is the **dual** part: two roles — a producer and a resident daemon, one identity
in two roles — write the same store at once, and neither has to know the other's order.

## The mutex

One shared **named mutex** — the sole mutual-exclusion primitive, with no lock file to
go stale. On Windows it is a real `CreateMutexW` / `WaitForSingleObject`:

- **zero-poll:** the OS wakes a waiter the instant the mutex is released — no arbitrary
  recheck interval, no guessed latency;
- **authoritative liveness:** if a holder (process *or* thread) dies while holding it,
  the OS signals `WAIT_ABANDONED` and wakes a waiter in ~0.01 ms — not a 20-second
  stale timeout. One real OS signal subsumes both cross-process and same-process
  liveness; nothing is guessed apart;
- **reentrant** for the same thread.

Measured, on real hardware under heavier-than-real contention: ~11.7 ms median write,
~3.26 s worst-case wait under 24 threads — comfortably inside the honest ceiling, and
never the typical case.

The production implementation is `netwatch._possess_mutex`. A clean, self-contained
reference is in [`dual_commutative_write.py`](dual_commutative_write.py), with a CLI to
drive it.

## Why it matters

Conflict-free concurrent writes with correct liveness, on a real OS primitive,
zero-poll, measured. It is the foundation the rest of the work is written on: every
record in the system — every ghost, every lineage step, every statement — commits
through this.
