# test_app.py
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

import app  # assuming app.py is in the same directory


# --- Fixtures for sample Kubernetes objects ---
@pytest.fixture
def mock_pod():
    pod = MagicMock()
    pod.metadata.name = "mypod"
    pod.metadata.namespace = "default"
    pod.metadata.creation_timestamp = datetime(2025, 1, 1, tzinfo=timezone.utc)
    pod.status.phase = "Running"
    pod.spec.node_name = "node1"
    return pod


@pytest.fixture
def mock_deployment():
    dep = MagicMock()
    dep.metadata.name = "mydep"
    dep.metadata.namespace = "default"
    dep.status.replicas = 3
    dep.status.available_replicas = 2
    return dep


@pytest.fixture
def mock_node():
    node = MagicMock()
    node.metadata.name = "node1"
    node.metadata.creation_timestamp = datetime(2025, 1, 1, tzinfo=timezone.utc)
    cond = MagicMock()
    cond.type = "Ready"
    cond.status = "True"
    node.status.conditions = [cond]
    return node


# --- Tests for utility functions ---
def test_age_in_human_readable():
    ts = datetime.now(timezone.utc)
    result = app.age_in_human_readable(ts)
    assert isinstance(result, str)
    assert any(unit in result for unit in ["m", "h", "d"])


@pytest.mark.parametrize("status,emoji", [
    ("Running", "🟢"),
    ("Pending", "🟡"),
    ("Failed", "🔴"),
    ("UnknownStatus", "⚪️")
])
def test_status_emoji(status, emoji):
    assert app.status_emoji(status) == emoji


# --- Tests for Kubernetes helpers with mocks ---
@patch("app.core.list_pod_for_all_namespaces")
def test_get_pods(mock_list, mock_pod):
    mock_list.return_value.items = [mock_pod]
    df = app.get_pods()
    assert isinstance(df, pd.DataFrame)
    assert df.iloc[0]["name"] == "mypod"
    assert df.iloc[0]["status_icon"] == "🟢"


@patch("app.apps.list_deployment_for_all_namespaces")
def test_get_deployments(mock_list, mock_deployment):
    mock_list.return_value.items = [mock_deployment]
    df = app.get_deployments()
    assert isinstance(df, pd.DataFrame)
    assert df.iloc[0]["replicas"] == 3
    assert df.iloc[0]["available"] == 2


@patch("app.core.list_node")
def test_get_nodes(mock_list, mock_node):
    mock_list.return_value.items = [mock_node]
    df = app.get_nodes()
    assert isinstance(df, pd.DataFrame)
    assert df.iloc[0]["status_icon"] == "🟢"


# --- Tests for Prometheus query helpers ---
@patch("app.requests.get")
def test_query_prometheus(mock_get):
    mock_get.return_value.json.return_value = {"status": "success", "data": {"result": [1, 2, 3]}}
    mock_get.return_value.raise_for_status = lambda: None
    result = app.query_prometheus("http://fake", "up")
    assert result == [1, 2, 3]


@patch("app.requests.get")
def test_query_prometheus_range(mock_get):
    mock_get.return_value.json.return_value = {"status": "success", "data": {"result": [1, 2]}}
    mock_get.return_value.raise_for_status = lambda: None
    result = app.query_prometheus_range("http://fake", "up", 0, 10, 5)
    assert result == [1, 2]


# --- Test safe_list with ApiException ---
from kubernetes.client.rest import ApiException


def test_safe_list_exception():
    def fail_func():
        raise ApiException(reason="Fail")

    result = app.safe_list(fail_func)
    assert result == []
