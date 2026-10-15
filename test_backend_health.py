from unittest.mock import MagicMock, patch

import pytest
import requests

from services.pointnxt_api import PointNXTAPI


def _api() -> PointNXTAPI:
    api = PointNXTAPI()
    api.base_url = "https://api.example.test"
    return api


def test_health_check_200_is_healthy():
    response = MagicMock(status_code=200)
    with patch.object(PointNXTAPI, "_apply_auth"), patch(
        "services.pointnxt_api.requests.get", return_value=response
    ) as get:
        api = _api()
        with patch("services.pointnxt_api.perf_counter", side_effect=[0, 0.1]):
            result = api.check_backend_health()

    assert result == {
        "status": "healthy",
        "reachable": True,
        "http_status": 200,
        "latency_ms": 100.0,
        "error": None,
        "endpoint": "/commerce/orders/stats",
    }
    get.assert_called_once_with(
        "https://api.example.test/commerce/orders/stats",
        headers=api.headers,
        timeout=5,
    )


@pytest.mark.parametrize("status_code", [404, 500])
def test_health_check_http_error_is_unhealthy(status_code):
    with patch.object(PointNXTAPI, "_apply_auth"), patch(
        "services.pointnxt_api.requests.get",
        return_value=MagicMock(status_code=status_code),
    ):
        result = _api().check_backend_health()

    assert result["status"] == "unhealthy"
    assert result["reachable"] is True
    assert result["http_status"] == status_code
    assert result["error"] == f"HTTP {status_code} from /commerce/orders/stats"


@pytest.mark.parametrize("exception", [requests.Timeout(), requests.ConnectionError()])
def test_health_check_request_failure_is_unhealthy(exception):
    with patch.object(PointNXTAPI, "_apply_auth"), patch(
        "services.pointnxt_api.requests.get", side_effect=exception
    ):
        result = _api().check_backend_health()

    assert result["status"] == "unhealthy"
    assert result["reachable"] is False
    assert result["http_status"] is None
    assert result["endpoint"] == "/commerce/orders/stats"


def test_health_check_slow_200_is_degraded():
    with patch.object(PointNXTAPI, "_apply_auth"), patch(
        "services.pointnxt_api.requests.get", return_value=MagicMock(status_code=200)
    ), patch("services.pointnxt_api.perf_counter", side_effect=[0, 2.1]):
        result = _api().check_backend_health()

    assert result["status"] == "degraded"
    assert result["http_status"] == 200
