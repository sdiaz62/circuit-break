# circuit_break

A small circuit breaker with half-open probing. Standard library only — no dependencies.

## Usage

```python
from circuit_break import CircuitBreaker, CircuitBreakerError, CircuitState

cb = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

@cb
def fetch_thing(thing_id):
    return {"id": thing_id}  # pretend this hits a real downstream

# imperative form
try:
    result = cb.call(fetch_thing, 1)
except CircuitBreakerError:
    # breaker is OPEN; call was not attempted
    fallback()
```

## Exports

- `CircuitBreaker(failure_threshold=5, recovery_timeout=30.0, clock=None, name=None)`
  - `.call(func, *args, **kwargs)` — invoke `func` if the breaker permits.
  - `.state` — one of `CircuitState.CLOSED`, `CircuitState.OPEN`, `CircuitState.HALF_OPEN`.
  - `.reset()` — force back to CLOSED.
  - `__call__(func)` — decorator wrapper.
  - `.last_exception` — the most recent exception caught (diagnostic; not cleared by `reset`).
- `CircuitState` — enum of the three states.
- `CircuitBreakerError` — raised when a call is rejected without being attempted.

## Why this exists

Calls to flaky downstreams need a way to fail fast instead of queuing up work that is
almost certain to fail. A circuit breaker trips after `failure_threshold` failures in a
row, stays open for `recovery_timeout` seconds, then allows exactly one probe call before
admitting traffic again.

The deliberate trade-off: every exception counts as a failure (no exception allowlist), and
a single success in CLOSED resets the failure count to zero rather than decaying it. Both
keep the rule easy to state: either the recent window is bad or it isn't.

## The awkward edge

While the single half-open probe is in flight, every other call is rejected with
`CircuitBreakerError` — including re-entrant calls from the probe itself. Without this, the
half-open window collapses into closed the instant the probe succeeds, letting a stampede
through before you've actually confirmed the downstream is healthy. If your probe function
recursively re-enters the breaker, it will see `CircuitBreakerError` on the inner call;
structure the probe so it does not.

## Time and testing

`CircuitBreaker` takes a `clock` callable defaulting to `time.monotonic`. Pass an integer
counter in tests so transitions are exact and never depend on wall-clock drift.

## Design notes

The window stores values eagerly rather than keeping running aggregates. Running
sums drift with floating point over long streams, and recomputing from a small
buffer is cheap enough that the drift is not worth the speed.

