import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone
import requests
from typing import Any, List


# ============================================================
#             Mock Kubernetes Config at Import
# ============================================================
# Prevents tests from failing due to missing kube-config or cluster
with patch("kubernetes.config.load_incluster_config"), \
     patch("kubernetes.config.load_kube_config"), \
     patch("app.safe_list", return_value=[]), \
     patch("app.core.list_pod_for_all_namespaces", return_value=MagicMock(items=[])), \
     patch("app.core.list_node", return_value=MagicMock(items=[])), \
     patch("app.core.list_namespaced_deployment", return_value=MagicMock(items=[])):
    import app


# ============================================================
#                 Utility Function Tests
# ============================================================

def test_age_in_human_readable_minutes() -> None:
    """
    Test: Ensure that `age_in_human_readable()` correctly converts
    a datetime object that is 5 minutes old into a readable string.
    """
    ts: datetime = datetime.now(timezone.utc) - timedelta(minutes=5)  # Simulate timestamp from 5 minutes ago
    result: str = app.age_in_human_readable(ts)  # Call the function being tested
    assert "m" in result  # Expect minutes indicator
    assert "5" in result  # Expect numeric value "5" present in output


def test_age_in_human_readable_hours() -> None:
    """
    Test: Validate conversion for timestamps that are a few hours old.
    """
    ts: datetime = datetime.now(timezone.utc) - timedelta(hours=2, minutes=10)
    result: str = app.age_in_human_readable(ts)
    assert "2h" in result  # Should show hours (e.g., '2h')


def test_age_in_human_readable_days() -> None:
    """
    Test: Validate conversion for timestamps that are several days old.
    """
    ts: datetime = datetime.now(timezone.utc) - timedelta(days=3, hours=5)
    result: str = app.age_in_human_readable(ts)
    assert "3d" in result  # Expect '3d' for 3 days


def test_age_in_human_readable_none() -> None:
    """
    Test: When None is passed, should return a placeholder ('-').
    """
    assert app.age_in_human_readable(None) == "-"


@pytest.mark.parametrize("status,emoji", [
    ("Running", "🟢"),  # Active pods or resources
    ("Pending", "🟡"),  # Waiting to start
    ("Failed", "🔴"),   # Failed state
    ("Unknown", "🔴"),  # Unknown state defaults to red
    ("SomethingElse", "⚪️"),  # Non-mapped states default to white
])
def test_status_emoji(status: str, emoji: str) -> None:
    """
    Test: Validate mapping between resource status and emoji icons.
    Ensures each status correctly returns the intended emoji.
    """
    assert app.status_emoji(status) == emoji


# ============================================================
#                 Prometheus Query Tests
# ============================================================

@patch("app.requests.get")
def test_query_prometheus_success(mock_get: MagicMock) -> None:
    """
    Test: Verify that `query_prometheus()` handles a successful
    Prometheus API call and returns the parsed data list.
    """
    # Create a mock response mimicking a Prometheus success result
    mock_resp: MagicMock = MagicMock()
    mock_resp.json.return_value = {"status": "success", "data": {"result": [{"metric": {"ns": "default"}}]}}
    mock_resp.raise_for_status.return_value = None  # Simulate successful HTTP request
    mock_get.return_value = mock_resp  # Assign mock to requests.get

    # Execute the function under test
    result: List[dict[str, Any]] = app.query_prometheus("http://fake-prom", "up")

    # Assertions
    assert isinstance(result, list)
    assert "metric" in result[0]  # Ensure the response structure is correct


@patch("app.requests.get", side_effect=requests.exceptions.RequestException("timeout"))
@patch("app.st.error")
def test_query_prometheus_error(mock_st_error: MagicMock, mock_get: MagicMock) -> None:
    """
    Test: Ensure that request errors are gracefully handled and logged.
    Should return an empty list and trigger a Streamlit error message.
    """
    result: List[Any] = app.query_prometheus("http://fake-prom", "up")
    assert result == []  # Expect empty list on failure
    mock_st_error.assert_called_once()  # Ensure error was reported via Streamlit


@patch("app.requests.get")
def test_query_prometheus_range_success(mock_get: MagicMock) -> None:
    """
    Test: Validate `query_prometheus_range()` which retrieves
    time-range metric data. Ensures proper JSON parsing.
    """
    mock_resp: MagicMock = MagicMock()
    mock_resp.json.return_value = {
        "status": "success",
        "data": {"result": [{"metric": {"namespace": "test"}, "values": [[1, "0.5"]]}]}
    }
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    result: List[dict[str, Any]] = app.query_prometheus_range("http://fake-prom", "cpu_usage", 0, 10, 5)

    assert isinstance(result, list)
    assert "values" in result[0]  # Confirm metric data exists


@patch("app.requests.get", side_effect=requests.exceptions.RequestException("timeout"))
@patch("app.st.error")
def test_query_prometheus_range_error(mock_st_error: MagicMock, mock_get: MagicMock) -> None:
    """
    Test: Confirm proper handling of network or request failures
    for Prometheus range queries.
    """
    result: List[Any] = app.query_prometheus_range("http://fake-prom", "cpu_usage", 0, 10, 5)
    assert result == []  # Expect empty result set
    mock_st_error.assert_called_once()


# ============================================================
#                Kubernetes Helper Function Tests
# ============================================================

@patch("app.st.error")
def test_safe_list_success(mock_st_error: MagicMock) -> None:
    """
    Test: Ensure that `safe_list()` returns .items when the API call succeeds.
    """
    mock_func: MagicMock = MagicMock()
    mock_func.return_value.items = [1, 2, 3]  # Simulate K8s client list return
    result: List[int] = app.safe_list(mock_func)
    assert result == [1, 2, 3]
    mock_st_error.assert_not_called()  # No error should be displayed


@patch("app.st.error")
def test_safe_list_with_namespace(mock_st_error: MagicMock) -> None:
    """
    Test: Validate that the namespace argument is properly forwarded
    to the Kubernetes client list function.
    """
    mock_func: MagicMock = MagicMock()
    mock_func.return_value.items = [1]  # Simulated result
    result: List[int] = app.safe_list(mock_func, namespace="test")
    assert result == [1]
    mock_st_error.assert_not_called()


@patch("app.st.error")
def test_safe_list_failure(mock_st_error: MagicMock) -> None:
    """
    Test: Simulate APIException in `safe_list()` and ensure an empty list
    is returned with a Streamlit error message logged.
    """
    mock_func: MagicMock = MagicMock(side_effect=app.ApiException(reason="API down"))
    result: List[Any] = app.safe_list(mock_func)
    assert result == []
    mock_st_error.assert_called_once()


# ============================================================
#                 DataFrame Builder Tests
# ============================================================

@pytest.fixture
def mock_pod() -> MagicMock:
    """
    Fixture: Returns a mock Pod object that mimics Kubernetes API objects.
    Used for testing DataFrame builders.
    """
    mock: MagicMock = MagicMock()
    mock.metadata.name = "mypod"
    mock.metadata.namespace = "default"
    mock.metadata.creation_timestamp = datetime.now(timezone.utc) - timedelta(hours=1)
    mock.status.phase = "Running"
    mock.spec.node_name = "node1"
    return mock


@pytest.fixture
def mock_deployment() -> MagicMock:
    """
    Fixture: Returns a mock Deployment object with replica counts.
    """
    mock: MagicMock = MagicMock()
    mock.metadata.name = "mydep"
    mock.metadata.namespace = "default"
    mock.status.replicas = 3
    mock.status.available_replicas = 2
    return mock


@pytest.fixture
def mock_node() -> MagicMock:
    """
    Fixture: Returns a mock Node object, including ready condition.
    """
    mock: MagicMock = MagicMock()
    mock.metadata.name = "node1"
    mock.metadata.creation_timestamp = datetime.now(timezone.utc) - timedelta(days=1)
    cond_ready: MagicMock = MagicMock()
    cond_ready.type = "Ready"
    cond_ready.status = "True"
    mock.status.conditions = [cond_ready]
    return mock


@patch("app.safe_list")
def test_get_pods(mock_safe_list: MagicMock, mock_pod: MagicMock) -> None:
    """
    Test: Verify that `get_pods()` correctly constructs a pandas DataFrame
    with proper columns and values from mock Pod objects.
    """
    mock_safe_list.return_value = [mock_pod]
    df: pd.DataFrame = app.get_pods()
    assert isinstance(df, pd.DataFrame)
    assert "namespace" in df.columns
    assert df.iloc[0]["name"] == "mypod"  # Verify expected data mapping


@patch("app.safe_list")
def test_get_deployments(mock_safe_list: MagicMock, mock_deployment: MagicMock) -> None:
    """
    Test: Ensure `get_deployments()` returns DataFrame with
    correct replica and availability data.
    """
    mock_safe_list.return_value = [mock_deployment]
    df: pd.DataFrame = app.get_deployments()
    assert isinstance(df, pd.DataFrame)
    assert "replicas" in df.columns
    assert df.iloc[0]["available"] == 2


@patch("app.safe_list")
def test_get_nodes(mock_safe_list: MagicMock, mock_node: MagicMock) -> None:
    """
    Test: Ensure `get_nodes()` produces DataFrame with node status and emoji.
    """
    mock_safe_list.return_value = [mock_node]
    df: pd.DataFrame = app.get_nodes()
    assert isinstance(df, pd.DataFrame)
    assert "status" in df.columns
    assert df.iloc[0]["status_icon"] == "🟢"  # Node is ready (green icon)
