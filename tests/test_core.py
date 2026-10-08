import unittest

from circuit_break import CircuitBreaker, CircuitState, CircuitBreakerError


class FakeClock:
    """Deterministic monotonic-ish clock.

    Every call to ``now()`` returns the current tick; ``advance`` bumps it.
    We never compare floats in the tests, so integer ticks keep arithmetic
    exact.
    """
    def __init__(self):
        self._t = 0

    def __call__(self):
        return self._t

    def advance(self, seconds):
        self._t += seconds


class AlwaysFails(Exception):
    pass


class TestCircuitBreaker(unittest.TestCase):
    def test_starts_closed(self):
        cb = CircuitBreaker()
        self.assertIs(cb.state, CircuitState.CLOSED)

    def test_passes_through_on_success(self):
        cb = CircuitBreaker()
        seen = []
        result = cb.call(lambda: seen.append("ok") or 42)
        self.assertEqual(result, 42)
        self.assertEqual(seen, ["ok"])

    def test_failure_count_resets_on_success(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(2):
            with self.assertRaises(AlwaysFails):
                cb.call(self._raising)
        # success flushes the window
        self.assertEqual(cb.call(lambda: 1), 1)
        # two more failures must not trip a threshold of 3
        for _ in range(2):
            with self.assertRaises(AlwaysFails):
                cb.call(self._raising)
        self.assertIs(cb.state, CircuitState.CLOSED)

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            with self.assertRaises(AlwaysFails):
                cb.call(self._raising)
        self.assertIs(cb.state, CircuitState.OPEN)

    def test_open_rejects_without_calling(self):
        cb = CircuitBreaker(failure_threshold=1)
        with self.assertRaises(AlwaysFails):
            cb.call(self._raising)
        self.assertIs(cb.state, CircuitState.OPEN)

        called = []
        with self.assertRaises(CircuitBreakerError):
            cb.call(lambda: called.append("nope"))
        self.assertEqual(called, [])

    def test_open_to_half_open_after_timeout(self):
        clock = FakeClock()
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=10.0, clock=clock)
        with self.assertRaises(AlwaysFails):
            cb.call(self._raising)
        self.assertIs(cb.state, CircuitState.OPEN)

        clock.advance(9)
        self.assertIs(cb.state, CircuitState.OPEN)
        clock.advance(1)
        self.assertIs(cb.state, CircuitState.HALF_OPEN)

    def test_half_open_probe_success_closes(self):
        clock = FakeClock()
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=5.0, clock=clock)
        with self.assertRaises(AlwaysFails):
            cb.call(self._raising)
        clock.advance(5)
        self.assertIs(cb.state, CircuitState.HALF_OPEN)

        self.assertEqual(cb.call(lambda: "ok"), "ok")
        self.assertIs(cb.state, CircuitState.CLOSED)
        self.assertEqual(cb._failures, 0)

    def test_half_open_probe_failure_reopens(self):
        clock = FakeClock()
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=5.0, clock=clock)
        with self.assertRaises(AlwaysFails):
            cb.call(self._raising)
        clock.advance(5)
        self.assertIs(cb.state, CircuitState.HALF_OPEN)

        with self.assertRaises(AlwaysFails):
            cb.call(self._raising)
        self.assertIs(cb.state, CircuitState.OPEN)
        # and the recovery timer restarted
        clock.advance(4)
        self.assertIs(cb.state, CircuitState.OPEN)
        clock.advance(1)
        self.assertIs(cb.state, CircuitState.HALF_OPEN)

    def test_half_open_concurrent_probe_rejected(self):
        """The single awkward edge: while a probe is in flight, a
        second call must be rejected without touching the function.

        We model 'in flight' by blocking on a lock inside the probe.
        """
        import threading
        clock = FakeClock()
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=5.0, clock=clock)
        with self.assertRaises(AlwaysFails):
            cb.call(self._raising)
        clock.advance(5)
        self.assertIs(cb.state, CircuitState.HALF_OPEN)

        probe_started = threading.Event()
        probe_hold = threading.Event()
        results = []

        def probe():
            probe_started.set()
            probe_hold.wait(timeout=1.0)
            return "ok"

        def run_probe():
            results.append(cb.call(probe))

        t = threading.Thread(target=run_probe)
        t.start()
        probe_started.wait(timeout=1.0)
        # While the probe is mid-flight, a second call is rejected.
        with self.assertRaises(CircuitBreakerError):
            cb.call(lambda: "should-not-run")
        # release the probe
        probe_hold.set()
        t.join(timeout=1.0)
        self.assertEqual(results, ["ok"])
        self.assertIs(cb.state, CircuitState.CLOSED)

    def test_reset_clears_state(self):
        clock = FakeClock()
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=5.0, clock=clock)
        for _ in range(2):
            with self.assertRaises(AlwaysFails):
                cb.call(self._raising)
        self.assertIs(cb.state, CircuitState.OPEN)
        cb.reset()
        self.assertIs(cb.state, CircuitState.CLOSED)
        self.assertEqual(cb._failures, 0)

    def test_decorator_preserves_metadata(self):
        cb = CircuitBreaker(failure_threshold=3)

        @cb
        def add(a, b):
            """sum two ints"""
            return a + b

        self.assertEqual(add.__name__, "add")
        self.assertEqual(add.__doc__, "sum two ints")
        self.assertEqual(add(2, 3), 5)
        self.assertIs(cb.state, CircuitState.CLOSED)

    def test_decorator_records_failures(self):
        cb = CircuitBreaker(failure_threshold=2)

        @cb
        def boom():
            raise AlwaysFails()

        for _ in range(2):
            with self.assertRaises(AlwaysFails):
                boom()
        self.assertIs(cb.state, CircuitState.OPEN)

    def test_last_exception_kept(self):
        cb = CircuitBreaker(failure_threshold=2)
        with self.assertRaises(AlwaysFails):
            cb.call(self._raising)
        self.assertIsInstance(cb.last_exception, AlwaysFails)

    def test_rejects_invalid_threshold(self):
        with self.assertRaises(ValueError):
            CircuitBreaker(failure_threshold=0)
        with self.assertRaises(ValueError):
            CircuitBreaker(failure_threshold=-1)

    def test_rejects_negative_timeout(self):
        with self.assertRaises(ValueError):
            CircuitBreaker(recovery_timeout=-1)

    def test_default_clock_is_monotonic(self):
        import time
        cb = CircuitBreaker()
        self.assertIs(cb._clock, time.monotonic)

    def test_zero_timeout_allows_immediate_half_open(self):
        """recovery_timeout=0 means the instant a failure trips the
        breaker, the next call is a probe. An unusual but valid
        configuration worth pinning down.
        """
        clock = FakeClock()
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0, clock=clock)
        with self.assertRaises(AlwaysFails):
            cb.call(self._raising)
        # no advance needed
        self.assertIs(cb.state, CircuitState.HALF_OPEN)
        self.assertEqual(cb.call(lambda: "ok"), "ok")
        self.assertIs(cb.state, CircuitState.CLOSED)

    def test_open_message_contains_name(self):
        cb = CircuitBreaker(failure_threshold=1, name="orders")
        with self.assertRaises(AlwaysFails):
            cb.call(self._raising)
        try:
            cb.call(lambda: None)
        except CircuitBreakerError as e:
            self.assertIn("orders", str(e))
        else:
            self.fail("expected CircuitBreakerError")

    @staticmethod
    def _raising(*args, **kwargs):
        raise AlwaysFails()


if __name__ == "__main__":
    unittest.main()
