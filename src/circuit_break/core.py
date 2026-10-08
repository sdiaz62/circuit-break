import time
from enum import Enum
from functools import wraps


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerError(Exception):
    """Raised when the breaker rejects a call while OPEN.

    We deliberately do NOT wrap the last underlying failure here —
    that would conflate 'rejected before attempt' with 'attempted
    and failed', which callers usually want to distinguish. The last
    exception is kept on ``CircuitBreaker.last_exception`` for callers
    that need it.
    """


class CircuitBreaker:
    """A circuit breaker with half-open probing.

    Design decisions, written down so nobody has to reverse-engineer
    the tests to learn them:

    * **Stable failure counts.** A single successful call in CLOSED
      resets the failure count to zero. We chose full reset over
      decay-on-success because it is easy to reason about: either
      the recent window is healthy or it isn't.
    * **One canonical probe.** In HALF_OPEN exactly one in-flight call
      is allowed. A second ``call`` while that probe is pending is
      rejected as if the breaker were OPEN — without this, a recovery
      probe would let a stampede through the instant it succeeded.
    * **Time is injectable.** The constructor takes a ``clock`` callable
      returning epoch seconds, defaulting to ``time.monotonic``. Tests
      pass an integer counter so transitions are deterministic and
      never depend on wall-clock drift.
    * **No exception filtering.** Any exception raised by the wrapped
      callable counts as a failure. The brief didn't ask for
      ``ignore_exceptions``; a configurable allowlist would invite
      callers to misconfigure themselves out of the safety the
      breaker exists to provide.
    """

    def __init__(
        self,
        failure_threshold=5,
        recovery_timeout=30.0,
        clock=None,
        name=None,
    ):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if recovery_timeout < 0:
            raise ValueError("recovery_timeout must be >= 0")

        self.failure_threshold = failure_threshold
        self.recovery_timeout = float(recovery_timeout)
        # We use monotonic by default so that system clock adjustments
        # (NTP steps, manual changes) don't flip an OPEN breaker early
        # or hold it open forever.
        self._clock = clock if clock is not None else time.monotonic
        self.name = name or "circuit_breaker"

        self._state = CircuitState.CLOSED
        self._failures = 0
        self._last_failure_time = None
        self._probe_in_flight = False
        self.last_exception = None

    @property
    def state(self):
        """Current state, resolving a due OPEN into HALF_OPEN lazily.

        Resolving here (rather than only inside ``call``) means
        observability tools reading ``.state`` see HALF_OPEN as soon
        as the timeout elapses, even if no new call has arrived. But
        the transition into CLOSED still requires a successful probe —
        reading state never changes recovery semantics, only display.
        """
        if (
            self._state is CircuitState.OPEN
            and self._last_failure_time is not None
            and self._clock() - self._last_failure_time >= self.recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            self._probe_in_flight = False
        return self._state

    def call(self, func, *args, **kwargs):
        """Invoke ``func`` if the breaker permits it.

        Returns whatever ``func`` returns, or raises whatever ``func``
        raises after recording the failure. Raises ``CircuitBreakerError``
        without invoking ``func`` when the breaker is OPEN (and the
        recovery timeout hasn't elapsed) or when a HALF_OPEN probe is
        already running.
        """
        current = self.state

        if current is CircuitState.OPEN:
            raise CircuitBreakerError(
                f"{self.name} is OPEN; call rejected"
            )

        if current is CircuitState.HALF_OPEN:
            # The canonical 'awkward edge': while the single probe is
            # in flight, every concurrent or re-entrant call must be
            # rejected. Otherwise the half-open window collapses into
            # closed as soon as the probe succeeds, defeating the
            # purpose of probing.
            if self._probe_in_flight:
                raise CircuitBreakerError(
                    f"{self.name} is HALF_OPEN with a probe in flight; call rejected"
                )
            self._probe_in_flight = True
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                self._on_failure(exc)
                raise
            else:
                self._on_success()
                return result
            finally:
                self._probe_in_flight = False

        # CLOSED
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            self._on_failure(exc)
            raise
        self._on_success()
        return result

    def _on_failure(self, exc):
        self._failures += 1
        self.last_exception = exc
        self._last_failure_time = self._clock()
        if self._failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._probe_in_flight = False

    def _on_success(self):
        # One success closes the breaker entirely and forgets prior
        # failures. In HALF_OPEN this is the recovery signal. In
        # CLOSED this is a full reset rather than a decay, which is
        # deliberately conservative: either the recent window is bad
        # or it isn't.
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._probe_in_flight = False

    def reset(self):
        """Force the breaker back to a pristine CLOSED state.

        Useful from operational tooling or after an out-of-band fix.
        Does not clear ``last_exception`` — that is diagnostic state,
        not breaker state.
        """
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._probe_in_flight = False
        self._last_failure_time = None

    def __call__(self, func):
        """Decorator form. The returned wrapper preserves ``__name__``/``__doc__``
        via ``functools.wraps`` so decorated functions still introspect
        correctly in logs and traces.
        """
        @wraps(func)
        def wrapper(*args, **kwargs):
            return self.call(func, *args, **kwargs)
        return wrapper
