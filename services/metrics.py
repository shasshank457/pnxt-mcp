import threading
from statistics import mean

_lock = threading.Lock()
_latencies_ms: list[float] = []
_backend_latencies_ms: list[float] = []
_total_requests = 0
_successful_requests = 0
_failed_requests = 0
_slow_requests = 0


def record_request(
    *, request_id: str, latency_ms: float, backend_latency_ms: float, success: bool
) -> None:
    global _total_requests, _successful_requests, _failed_requests, _slow_requests
    with _lock:
        _total_requests += 1
        _successful_requests += int(success)
        _failed_requests += int(not success)
        _slow_requests += int(latency_ms > 1000)
        _latencies_ms.append(latency_ms)
        _backend_latencies_ms.append(backend_latency_ms)


def get_metrics() -> dict:
    with _lock:
        return {
            "total_requests": _total_requests,
            "successful_requests": _successful_requests,
            "failed_requests": _failed_requests,
            "slow_requests_over_1s": _slow_requests,
            "average_latency_ms": round(mean(_latencies_ms), 2) if _latencies_ms else 0,
            "average_backend_latency_ms": round(mean(_backend_latencies_ms), 2)
            if _backend_latencies_ms
            else 0,
        }
