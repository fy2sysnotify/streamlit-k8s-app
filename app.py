import streamlit as st
import pandas as pd
import requests
import plotly.express as px
from datetime import datetime, timezone, timedelta, UTC
from kubernetes import client, config
from kubernetes.client import V1PodList, V1Pod, V1DeploymentList, V1NodeList, V1ServiceList
from kubernetes.client import V1ConfigMapList, V1StatefulSetList, V1DaemonSetList, V1JobList, V1CronJobList
from kubernetes.client import V1PersistentVolumeList, V1PersistentVolumeClaimList
from kubernetes.client.rest import ApiException
from typing import Any, Callable, Optional, List, Dict
import time

# --- Load Kubernetes config ---
try:
    config.load_incluster_config()
except Exception:
    config.load_kube_config()

core = client.CoreV1Api()
apps = client.AppsV1Api()
batch = client.BatchV1Api()

# --- Streamlit config ---
st.set_page_config(page_title="Kubernetes Dashboard", layout="wide")
st.title("☸️ Kubernetes Cluster Dashboard")

# --- Sidebar ---
st.sidebar.header("Settings")
namespace_filter: str = st.sidebar.text_input("Namespace filter (optional)", "")
prom_url: str = st.sidebar.text_input(
    "Prometheus URL",
    "http://prometheus-kube-prometheus-prometheus.prometheus.svc.cluster.local:9090"
)
refresh_interval: int = st.sidebar.slider("Auto-refresh interval (seconds)", 10, 120, 30)
manual_refresh: bool = st.sidebar.button("🔄 Refresh now")


# ============================================================
# Utility functions
# ============================================================

def age_in_human_readable(ts: Optional[datetime]) -> str:
    """
    Convert a Kubernetes creation timestamp into a human-readable age.

    Parameters
    ----------
    ts : datetime or None
        Timestamp in UTC for the object's creation.

    Returns
    -------
    str
        Age formatted as e.g. '2d 3h', '5h 22m', or '-' if timestamp is None.
    """
    if not ts:
        return "-"
    delta = datetime.now(timezone.utc) - ts
    days, seconds = delta.days, delta.seconds
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"


def status_emoji(status: str) -> str:
    """
    Map Kubernetes status strings to emoji indicators.

    Parameters
    ----------
    status : str
        The raw status (e.g., 'Running', 'Pending', etc.).

    Returns
    -------
    str
        Corresponding emoji indicator for quick visualization.
    """
    if status in ("Running", "Active", "True", "Ready", "Succeeded", "Bound"):
        return "🟢"
    elif status in ("Pending", "Creating", "ContainerCreating"):
        return "🟡"
    elif status in ("Failed", "CrashLoopBackOff", "Terminating", "Unknown"):
        return "🔴"
    else:
        return "⚪️"


def safe_list(func: Callable[..., Any], namespace: Optional[str] = None) -> List[Any]:
    """
    Safely list Kubernetes resources using a provided list function.

    Parameters
    ----------
    func : Callable
        Kubernetes client list function (e.g., `core.list_pod_for_all_namespaces`).
    namespace : str, optional
        If provided and not "All", restricts the listing to that namespace.

    Returns
    -------
    list
        List of Kubernetes objects (e.g., pods, deployments). Empty list if an error occurs.
    """
    try:
        # Call namespace-specific or cluster-wide listing
        if namespace and namespace != "All":
            return func(namespace=namespace).items
        return func().items
    except ApiException as e:
        st.error(f"Unable to fetch resources ({func.__name__}): {e.reason}")
        return []


# ============================================================
# Prometheus helper functions
# ============================================================

def query_prometheus(prometheus_url: str, query: str) -> List[Dict[str, Any]]:
    """
    Execute an instant Prometheus query.

    Parameters
    ----------
    prometheus_url : str
        Base URL of the Prometheus server.
    query : str
        PromQL query string.

    Returns
    -------
    list of dict
        List of query results. Returns empty list on failure.
    """
    try:
        resp = requests.get(f"{prometheus_url}/api/v1/query",
                            params={"query": query}, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return data["data"]["result"] if data.get("status") == "success" else []
    except Exception as e:
        st.error(f"Prometheus query error: {e}")
        return []


def query_prometheus_range(
        prometheus_url: str,
        query: str,
        start: int,
        end: int,
        step: int
) -> List[Dict[str, Any]]:
    """
    Execute a range query against Prometheus over a time window.

    Parameters
    ----------
    prometheus_url : str
        Base URL of the Prometheus server.
    query : str
        PromQL query.
    start : int
        Start timestamp (seconds since epoch).
    end : int
        End timestamp (seconds since epoch).
    step : int
        Query step interval in seconds.

    Returns
    -------
    list of dict
        Time series results with metric labels and values.
    """
    try:
        params = {"query": query, "start": start, "end": end, "step": step}
        resp = requests.get(f"{prometheus_url}/api/v1/query_range",
                            params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data["data"]["result"] if data.get("status") == "success" else []
    except Exception as e:
        st.error(f"Prometheus range query error: {e}")
        return []


# ============================================================
# Kubernetes resource helpers
# ============================================================

def get_pods() -> pd.DataFrame:
    """Return all Pods in the cluster (optionally filtered by namespace)."""
    pods: List[V1Pod] = safe_list(core.list_pod_for_all_namespaces)
    data: List[Dict[str, Any]] = []
    for p in pods:
        ns = p.metadata.namespace
        if namespace_filter and ns != namespace_filter:
            continue
        data.append({
            "namespace": ns,
            "name": p.metadata.name,
            "status": p.status.phase,
            "node": getattr(p.spec, "node_name", "-"),
            "age": age_in_human_readable(p.metadata.creation_timestamp),
            "status_icon": status_emoji(p.status.phase)
        })
    return pd.DataFrame(data)


def get_deployments() -> pd.DataFrame:
    """Return all Deployments in the cluster (optionally filtered)."""
    deps = safe_list(apps.list_deployment_for_all_namespaces)
    data: List[Dict[str, Any]] = []
    for d in deps:
        ns = d.metadata.namespace
        if namespace_filter and ns != namespace_filter:
            continue
        data.append({
            "namespace": ns,
            "name": d.metadata.name,
            "replicas": d.status.replicas or 0,
            "available": d.status.available_replicas or 0,
        })
    return pd.DataFrame(data)


def get_nodes() -> pd.DataFrame:
    """Return Node status and readiness."""
    nodes = safe_list(core.list_node)
    data: List[Dict[str, Any]] = []
    for n in nodes:
        # Extract node readiness condition
        conds = {c.type: c.status for c in n.status.conditions or []}
        ready = conds.get("Ready", "Unknown")
        data.append({
            "name": n.metadata.name,
            "status": ready,
            "status_icon": status_emoji(ready),
            "age": age_in_human_readable(n.metadata.creation_timestamp),
        })
    return pd.DataFrame(data)


def get_services() -> pd.DataFrame:
    """Return all Services in the cluster (optionally filtered)."""
    svcs = safe_list(core.list_service_for_all_namespaces)
    data = [{
        "namespace": s.metadata.namespace,
        "name": s.metadata.name,
        "type": s.spec.type,
        "cluster_ip": s.spec.cluster_ip
    } for s in svcs if not namespace_filter or s.metadata.namespace == namespace_filter]
    return pd.DataFrame(data)


def get_configmaps() -> pd.DataFrame:
    """Return all ConfigMaps."""
    cms = safe_list(core.list_config_map_for_all_namespaces)
    data = [{
        "namespace": cm.metadata.namespace,
        "name": cm.metadata.name,
        "age": age_in_human_readable(cm.metadata.creation_timestamp)
    } for cm in cms if not namespace_filter or cm.metadata.namespace == namespace_filter]
    return pd.DataFrame(data)


def get_statefulsets() -> pd.DataFrame:
    """Return all StatefulSets."""
    ss = safe_list(apps.list_stateful_set_for_all_namespaces)
    data = [{
        "namespace": s.metadata.namespace,
        "name": s.metadata.name,
        "replicas": s.status.replicas or 0,
        "ready": s.status.ready_replicas or 0
    } for s in ss if not namespace_filter or s.metadata.namespace == namespace_filter]
    return pd.DataFrame(data)


def get_daemonsets() -> pd.DataFrame:
    """Return all DaemonSets."""
    ds = safe_list(apps.list_daemon_set_for_all_namespaces)
    data = [{
        "namespace": d.metadata.namespace,
        "name": d.metadata.name,
        "desired": d.status.desired_number_scheduled or 0,
        "current": d.status.current_number_scheduled or 0
    } for d in ds if not namespace_filter or d.metadata.namespace == namespace_filter]
    return pd.DataFrame(data)


def get_jobs() -> pd.DataFrame:
    """Return all Jobs."""
    jobs = safe_list(batch.list_job_for_all_namespaces)
    data = [{
        "namespace": j.metadata.namespace,
        "name": j.metadata.name,
        "succeeded": j.status.succeeded or 0,
        "failed": j.status.failed or 0
    } for j in jobs if not namespace_filter or j.metadata.namespace == namespace_filter]
    return pd.DataFrame(data)


def get_cronjobs() -> pd.DataFrame:
    """Return all CronJobs."""
    cronjobs = safe_list(batch.list_cron_job_for_all_namespaces)
    data = [{
        "namespace": c.metadata.namespace,
        "name": c.metadata.name,
        "schedule": c.spec.schedule
    } for c in cronjobs if not namespace_filter or c.metadata.namespace == namespace_filter]
    return pd.DataFrame(data)


def get_persistent_volumes() -> pd.DataFrame:
    """Return all PersistentVolumes."""
    pvs = safe_list(core.list_persistent_volume)
    data = [{
        "name": pv.metadata.name,
        "capacity": pv.spec.capacity.get("storage") if pv.spec.capacity else "-",
        "status": pv.status.phase,
        "reclaim_policy": pv.spec.persistent_volume_reclaim_policy,
        "age": age_in_human_readable(pv.metadata.creation_timestamp),
        "status_icon": status_emoji(pv.status.phase)
    } for pv in pvs]
    return pd.DataFrame(data)


def get_persistent_volume_claims() -> pd.DataFrame:
    """Return all PersistentVolumeClaims."""
    pvcs = safe_list(core.list_persistent_volume_claim_for_all_namespaces)
    data = [{
        "namespace": pvc.metadata.namespace,
        "name": pvc.metadata.name,
        "status": pvc.status.phase,
        "volume": pvc.spec.volume_name,
        "storage": pvc.spec.resources.requests.get("storage")
        if pvc.spec.resources and pvc.spec.resources.requests else "-",
        "age": age_in_human_readable(pvc.metadata.creation_timestamp),
        "status_icon": status_emoji(pvc.status.phase)
    } for pvc in pvcs if not namespace_filter or pvc.metadata.namespace == namespace_filter]
    return pd.DataFrame(data)
