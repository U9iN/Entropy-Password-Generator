"""
entropy_engine.py

Multi-source entropy collection and cryptographic pooling.

Architecture:
    Entropy Sources -> Entropy Collector -> Entropy Pool -> bytes out

The OS CSPRNG is the anchor/primary source - it's the only one that
counts as full-strength entropy. Timing jitter and system state are
supplementary "seasoning": real but low-quality per sample, so they
must never be trusted as 1 sample = 1 bit. Everything gets mixed
through a cryptographic extractor (HMAC-SHA512) before being consumed
downstream by the Password Engine.

psutil (network/disk I/O counters) is an optional dependency, not a
stdlib one like everything else here: if it's missing, or its counters
don't actually work on this platform, that one source is silently left
out of the collector rather than breaking anything - this module (and
everything that imports it) still works with a plain `pip install`-free
setup, just without that one extra source.
"""

import os
import time
import struct
import hashlib
import hmac
import secrets
import threading

try:
    import psutil
except ImportError:
    psutil = None


# ---------------------------------------------------------------------------
# Entropy Sources
# ---------------------------------------------------------------------------

class EntropySources:
    """Individual entropy-producing probes. Each returns raw bytes;
    quality varies wildly between methods - see module docstring."""

    @staticmethod
    def os_csprng(n: int = 32) -> bytes:
        """Primary anchor source. High quality, always available."""
        return secrets.token_bytes(n)

    @staticmethod
    def timing_jitter(samples: int = 32) -> bytes:
        """Low-order bits of high-resolution clock deltas between
        rapid successive calls. A few real bits of jitter per sample
        at best - never treat this as high-entropy on its own."""
        buf = bytearray()
        prev = time.perf_counter_ns()
        for _ in range(samples):
            now = time.perf_counter_ns()
            buf.append((now - prev) & 0xFF)
            prev = now
        return bytes(buf)

    @staticmethod
    def thread_time_jitter(samples: int = 32) -> bytes:
        """Same shape as timing_jitter, but time.thread_time_ns() (CPU
        time charged to this thread) instead of the wall clock - a
        different, scheduler-dependent kind of noise: how much actual
        CPU time this thread got between calls, not how much time
        passed.

        Some platforms update thread-CPU-time on a coarser tick than
        the wall clock, which would show up here as a run of identical
        (zero) deltas - tight-loop sampling would be spinning for
        nothing at that point. If that happens, this stops early
        rather than wasting cycles, keeping whatever real jitter it
        already collected instead of padding with zeros."""
        buf = bytearray()
        prev = time.thread_time_ns()
        zero_streak = 0
        for _ in range(samples):
            now = time.thread_time_ns()
            delta = (now - prev) & 0xFF
            buf.append(delta)
            prev = now
            zero_streak = zero_streak + 1 if delta == 0 else 0
            if zero_streak >= 8:
                break
        return bytes(buf)

    @staticmethod
    def scheduler_jitter(samples: int = 16) -> bytes:
        """Thread-switch timing noise: spins a helper thread and
        measures how unevenly the scheduler interleaves it. Costs
        real time (thread spin-up) - fine for occasional collection,
        not for tight loops."""
        buf = bytearray()
        for _ in range(samples):
            flag = threading.Event()
            start = time.perf_counter_ns()
            threading.Thread(target=flag.set).start()
            flag.wait()
            buf.append((time.perf_counter_ns() - start) & 0xFF)
        return bytes(buf)

    @staticmethod
    def system_state() -> bytes:
        """Process/OS state snapshot. Low entropy on its own - mainly
        a differentiator between runs, not a real entropy contributor."""
        times = os.times()
        return b"".join([
            os.getpid().to_bytes(4, "little"),
            threading.get_ident().to_bytes(8, "little"),
            id(object()).to_bytes(8, "little"),
            struct.pack("d", times.user),
            struct.pack("d", times.system),
        ])

    @staticmethod
    def system_io() -> bytes:
        """Snapshot of cumulative network/disk I/O counters (psutil) -
        unlike every other source here, this reflects real system-wide
        activity (other processes' network traffic, disk writes), not
        just this process's own timing or state. Requires psutil;
        EntropyCollector only registers this source when it's
        installed and actually works on this platform, so callers
        never need to check for it themselves."""
        net = psutil.net_io_counters()
        disk = psutil.disk_io_counters()
        values = [net.bytes_sent, net.bytes_recv, net.packets_sent, net.packets_recv]
        values += [disk.read_bytes, disk.write_bytes, disk.read_count, disk.write_count] if disk else [0, 0, 0, 0]
        return b"".join((v % (1 << 64)).to_bytes(8, "little") for v in values)


# ---------------------------------------------------------------------------
# Entropy Collector
# ---------------------------------------------------------------------------

def _psutil_io_works() -> bool:
    if psutil is None:
        return False
    try:
        psutil.net_io_counters()
        return True
    except Exception:
        return False


_PSUTIL_IO_AVAILABLE = _psutil_io_works()  # checked once, not on every EntropyCollector()


class EntropyCollector:
    """Gathers raw bytes from every registered source into one buffer."""

    def __init__(self):
        self.sources = [
            EntropySources.os_csprng,
            EntropySources.timing_jitter,
            EntropySources.thread_time_jitter,
            EntropySources.scheduler_jitter,
            EntropySources.system_state,
        ]
        if _PSUTIL_IO_AVAILABLE:
            self.sources.append(EntropySources.system_io)

    def collect(self) -> bytes:
        return b"".join(src() for src in self.sources)

    def collect_detailed(self):
        """Same collection, but also returns a per-source breakdown
        (name, byte count) - purely for diagnostic/transparency display,
        the pool itself only ever needs the joined bytes from collect()."""
        parts, breakdown = [], []
        for src in self.sources:
            data = src()
            parts.append(data)
            breakdown.append((src.__name__, len(data)))
        return b"".join(parts), breakdown


# ---------------------------------------------------------------------------
# Entropy Pool
# ---------------------------------------------------------------------------

class EntropyPool:
    """Cryptographic extractor: mixes raw, uneven-quality input into
    uniform output. Extract via HMAC-SHA512, then expand to any length
    via SHAKE256 (XOF)."""

    def __init__(self, salt: bytes = None):
        # Random per-instance salt (drawn from the OS CSPRNG itself)
        # rather than a hardcoded constant, so no two installations
        # share the same extractor key.
        self.salt = salt or secrets.token_bytes(16)
        self.collector = EntropyCollector()

    def _extract(self, raw: bytes) -> bytes:
        return hmac.new(self.salt, raw, hashlib.sha512).digest()

    def get_bytes(self, n: int) -> bytes:
        """Arbitrary-length uniform output, freshly collected each call."""
        raw = self.collector.collect()
        seed = self._extract(raw)
        return hashlib.shake_256(seed).digest(n)


# ---------------------------------------------------------------------------
# Entropy Daemon
# ---------------------------------------------------------------------------

class EntropyDaemon:
    """Background continuous accumulator - adapted from Jira Desktop
    Monitor's monitoring_thread: same continuous-loop-with-interruptible-
    wait shape (threading.Event.wait() instead of time.sleep(), so
    stop() returns instantly instead of waiting out the last sleep).

    Every `interval` seconds it stirs a fresh sample into a running
    accumulator, so get_bytes() just reads what's already mixed in
    instead of paying for a fresh scheduler_jitter() thread spin-up
    on every call."""

    def __init__(self, pool: EntropyPool = None, interval: float = 10):
        self.pool = pool or EntropyPool()
        self.interval = interval
        self._stop_ev = threading.Event()
        self._lock = threading.Lock()
        self._accum = self.pool._extract(self.pool.collector.collect())
        self._last_breakdown = []
        self._thread = None

    def _stir_once(self):
        sample, breakdown = self.pool.collector.collect_detailed()
        with self._lock:
            # The fresh sample above isn't the whole story: every stir
            # also folds in whatever accum already held (so state
            # genuinely accumulates over time, not just the latest
            # sample) and the pool's salt keys the HMAC throughout -
            # both real inputs, neither coming from collect_detailed(),
            # so both are added here rather than left invisible.
            breakdown = breakdown + [
                ("previous_accum", len(self._accum)),
                ("pool_salt (HMAC key)", len(self.pool.salt)),
            ]
            self._accum = self.pool._extract(self._accum + sample)
            self._last_breakdown = breakdown

    def get_last_breakdown(self):
        """Per-source (name, byte_count) list from the most recent stir,
        including the carried-forward previous state and the pool's
        salt - the complete input picture, not just the fresh sample -
        for a GUI to show what entropy_engine.py actually touched,
        without the GUI needing to know anything about the sources
        (or the extractor's internals) itself."""
        with self._lock:
            return list(self._last_breakdown)

    def _cycle(self):
        # Same shape as monitoring_thread: loop while not stopped, do
        # the work, then an interruptible wait - never a blocking
        # time.sleep() that would delay shutdown.
        while not self._stop_ev.is_set():
            self._stir_once()
            self._stop_ev.wait(self.interval)

    def force_stir(self):
        """Mix in one fresh sample right now, without waiting for the
        next scheduled interval - e.g. for an on-demand 'Regenerate'
        button. Same mixing step _cycle() uses, just triggered
        synchronously by the caller instead of the background timer."""
        self._stir_once()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_ev.clear()
        self._thread = threading.Thread(
            target=self._cycle, daemon=True, name="entropy-daemon"
        )
        self._thread.start()

    def stop(self):
        self._stop_ev.set()
        if self._thread:
            self._thread.join(timeout=2)

    def peek(self, n: int) -> bytes:
        """Non-mutating read for display/diagnostics (the GUI's pool
        preview) - unlike get_bytes(), this does NOT ratchet state, so
        repeated calls between stirs keep returning the same bytes,
        which is exactly what lets the preview only change when a real
        stir happens. Never feed this into a password: no backward
        secrecy here - that property is specifically what get_bytes()
        provides."""
        with self._lock:
            seed = self._accum
        return hashlib.shake_256(seed).digest(n)

    def get_bytes(self, n: int) -> bytes:
        """Draw from the continuously-stirred accumulator, ratcheting
        state forward on every call (Fortuna-style): a single XOF call
        produces both the requested output and a fresh replacement
        seed, then the old seed is discarded. Two reads in a row now
        differ even with no stir in between, and - since SHAKE256 is
        one-way - compromising the accum after the fact doesn't let
        you reconstruct the seed that produced an earlier read, so
        past outputs stay safe (backward secrecy)."""
        with self._lock:
            seed = self._accum
            stream = hashlib.shake_256(seed).digest(n + 64)
            output, self._accum = stream[:n], stream[n:]
        return output


if __name__ == "__main__":
    pool = EntropyPool()
    print(pool.get_bytes(32).hex())
    print(pool.get_bytes(16).hex())

    daemon = EntropyDaemon(interval=1)
    daemon.start()
    print("daemon cycle 1:", daemon.get_bytes(16).hex())
    time.sleep(1.5)
    print("daemon cycle 2:", daemon.get_bytes(16).hex())
    daemon.stop()