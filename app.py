import streamlit as st
import pandas as pd
import requests
import plotly.express as px
from datetime import datetime, timezone, timedelta, UTC
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import logging
import json
import sys
import time
from typing import List, Any, Dict, Optional


# --------------------------------------------------
# ✅ JSON Logging Configuration (Kubernetes-Friendly)
# --------------------------------------------------
class JsonFormatter(logging.Formatter):
    """
    Custom logging formatter that outputs JSON objects for machine-readable logs.
    Compatible with Loki, ELK, Datadog, etc.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_record = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_record)


# Configure global logger
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers = [handler]

logger = logging.getLogger("k8s-dashboard")

# --------------------------------------------------
# Load Kubernetes configuration
# --------------------------------------------------
try:
    config.load_incluster_config()
    logger.info("Loaded in-cluster Kubernetes configuration.")
except Exception:
    config.load_kube_config()
    logger.info("Loaded local kubeconfig (fallback).")

# Initialize Kubernetes API clients
core = client.CoreV1Api()
apps = client.AppsV1Api()
batch = client.BatchV1Api()

# --------------------------------------------------
# Streamlit page setup
# --------------------------------------------------
st.set_page_config(page_title="Kubernetes Dashboard", layout="wide")
st.title("☸️ Kubernetes Cluster Dashboard")

# --- Sidebar controls ---
st.sidebar.header("Settings")
namespace_filter: str = st.sidebar.text_input("Namespace filter (optional)", "")
prom_url: str = st.sidebar.text_input(
    "Prometheus URL",
    "http://prometheus-kube-prometheus-prometheus.prometheus.svc.cluster.local:9090"
)
refresh_interval: int = st.sidebar.slider("Auto-refresh interval (seconds)", 10, 120, 30)
manual_refresh: bool = st.sidebar.button("🔄 Refresh now")


# --------------------------------------------------
# Utility Functions
# --------------------------------------------------
def age_in_human_readable(ts: Optional[datetime]) -> str:
    """
    Convert a datetime timestamp into a human-readable age string.
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
    Return an emoji representing resource status.
    """
    if status in ("Running", "Active", "True", "Ready", "Succeeded", "Bound"):
        return "🟢"
    elif status in ("Pending", "Creating", "ContainerCreating"):
        return "🟡"
    elif status in ("Failed", "CrashLoopBackOff", "Terminating", "Unknown"):
        return "🔴"
    else:
        return "⚪️"


def safe_list(func: Any, namespace: Optional[str] = None) -> List[Any]:
    """
    Safely call a Kubernetes list function, handling exceptions gracefully.
    """
    try:
        if namespace and namespace != "All":
            logger.debug(f"Listing with {func.__name__} in namespace '{namespace}'")
            return func(namespace=namespace).items
        logger.debug(f"Listing with {func.__name__} across all namespaces")
        return func().items
    except ApiException as e:
        logger.error(f"Unable to fetch resources ({func.__name__}): {e.reason}")
        st.error(f"Unable to fetch resources ({func.__name__}): {e.reason}")
        return []
    except Exception as e:
        logger.exception(f"Unexpected error in {func.__name__}: {e}")
        st.error(f"Unexpected error fetching resources ({func.__name__}): {e}")
        return []


# --------------------------------------------------
# Prometheus Query Helpers
# --------------------------------------------------
def query_prometheus(prometheus_url: str, query: str) -> List[Dict[str, Any]]:
    """
    Run an instant Prometheus query.
    """
    try:
        logger.debug(f"Prometheus instant query: {query}")
        resp = requests.get(f"{prometheus_url}/api/v1/query", params={"query": query}, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            return data["data"]["result"]
        logger.warning(f"Prometheus returned non-success status for instant query: {data.get('status')}")
        return []
    except Exception as e:
        logger.error(f"Prometheus query error: {e}")
        st.error(f"Prometheus query error: {e}")
        return []


def query_prometheus_range(prometheus_url: str, query: str, start: int, end: int, step: int) -> List[Dict[str, Any]]:
    """
    Run a range-based Prometheus query (time series).
    """
    try:
        logger.debug(f"Prometheus range query: {query} (start={start}, end={end}, step={step})")
        params = {"query": query, "start": start, "end": end, "step": step}
        resp = requests.get(f"{prometheus_url}/api/v1/query_range", params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            return data["data"]["result"]
        logger.warning(f"Prometheus returned non-success status for range query: {data.get('status')}")
        return []
    except Exception as e:
        logger.error(f"Prometheus range query error: {e}")
        st.error(f"Prometheus range query error: {e}")
        return []


# --------------------------------------------------
# Kubernetes Resource Fetchers
# --------------------------------------------------
def get_pods() -> pd.DataFrame:
    pods = safe_list(core.list_pod_for_all_namespaces)
    data = []
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
    logger.info(f"Fetched {len(data)} pods (filtered by namespace='{namespace_filter or 'All'}').")
    return pd.DataFrame(data)


def get_deployments() -> pd.DataFrame:
    deps = safe_list(apps.list_deployment_for_all_namespaces)
    data = []
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
    logger.info(f"Fetched {len(data)} deployments (filtered by namespace='{namespace_filter or 'All'}').")
    return pd.DataFrame(data)


def get_nodes() -> pd.DataFrame:
    nodes = safe_list(core.list_node)
    data = []
    for n in nodes:
        conds = {c.type: c.status for c in n.status.conditions or []}
        ready = conds.get("Ready", "Unknown")
        data.append({
            "name": n.metadata.name,
            "status": ready,
            "status_icon": status_emoji(ready),
            "age": age_in_human_readable(n.metadata.creation_timestamp),
        })
    logger.info(f"Fetched {len(data)} nodes.")
    return pd.DataFrame(data)


def get_services() -> pd.DataFrame:
    svcs = safe_list(core.list_service_for_all_namespaces)
    data = [{"namespace": s.metadata.namespace,
             "name": s.metadata.name,
             "type": s.spec.type,
             "cluster_ip": s.spec.cluster_ip}
            for s in svcs if not namespace_filter or s.metadata.namespace == namespace_filter]
    logger.info(f"Fetched {len(data)} services (filtered by namespace='{namespace_filter or 'All'}').")
    return pd.DataFrame(data)


def get_configmaps() -> pd.DataFrame:
    cms = safe_list(core.list_config_map_for_all_namespaces)
    data = [{"namespace": cm.metadata.namespace,
             "name": cm.metadata.name,
             "age": age_in_human_readable(cm.metadata.creation_timestamp)}
            for cm in cms if not namespace_filter or cm.metadata.namespace == namespace_filter]
    logger.info(f"Fetched {len(data)} configmaps (filtered by namespace='{namespace_filter or 'All'}').")
    return pd.DataFrame(data)


def get_statefulsets() -> pd.DataFrame:
    ss = safe_list(apps.list_stateful_set_for_all_namespaces)
    data = [{"namespace": s.metadata.namespace,
             "name": s.metadata.name,
             "replicas": s.status.replicas or 0,
             "ready": s.status.ready_replicas or 0}
            for s in ss if not namespace_filter or s.metadata.namespace == namespace_filter]
    logger.info(f"Fetched {len(data)} statefulsets (filtered by namespace='{namespace_filter or 'All'}').")
    return pd.DataFrame(data)


def get_daemonsets() -> pd.DataFrame:
    ds = safe_list(apps.list_daemon_set_for_all_namespaces)
    data = [{"namespace": d.metadata.namespace,
             "name": d.metadata.name,
             "desired": d.status.desired_number_scheduled or 0,
             "current": d.status.current_number_scheduled or 0}
            for d in ds if not namespace_filter or d.metadata.namespace == namespace_filter]
    logger.info(f"Fetched {len(data)} daemonsets (filtered by namespace='{namespace_filter or 'All'}').")
    return pd.DataFrame(data)


def get_jobs() -> pd.DataFrame:
    jobs = safe_list(batch.list_job_for_all_namespaces)
    data = [{"namespace": j.metadata.namespace,
             "name": j.metadata.name,
             "succeeded": j.status.succeeded or 0,
             "failed": j.status.failed or 0}
            for j in jobs if not namespace_filter or j.metadata.namespace == namespace_filter]
    logger.info(f"Fetched {len(data)} jobs (filtered by namespace='{namespace_filter or 'All'}').")
    return pd.DataFrame(data)


def get_cronjobs() -> pd.DataFrame:
    cronjobs = safe_list(batch.list_cron_job_for_all_namespaces)
    data = [{"namespace": c.metadata.namespace,
             "name": c.metadata.name,
             "schedule": c.spec.schedule}
            for c in cronjobs if not namespace_filter or c.metadata.namespace == namespace_filter]
    logger.info(f"Fetched {len(data)} cronjobs (filtered by namespace='{namespace_filter or 'All'}').")
    return pd.DataFrame(data)


def get_persistent_volumes() -> pd.DataFrame:
    pvs = safe_list(core.list_persistent_volume)
    data = [{
        "name": pv.metadata.name,
        "capacity": pv.spec.capacity.get("storage") if pv.spec.capacity else "-",
        "status": pv.status.phase,
        "reclaim_policy": pv.spec.persistent_volume_reclaim_policy,
        "age": age_in_human_readable(pv.metadata.creation_timestamp),
        "status_icon": status_emoji(pv.status.phase)
    } for pv in pvs]
    logger.info(f"Fetched {len(data)} persistent volumes.")
    return pd.DataFrame(data)


def get_persistent_volume_claims() -> pd.DataFrame:
    pvcs = safe_list(core.list_persistent_volume_claim_for_all_namespaces)
    data = [{
        "namespace": pvc.metadata.namespace,
        "name": pvc.metadata.name,
        "status": pvc.status.phase,
        "volume": pvc.spec.volume_name,
        "storage": pvc.spec.resources.requests.get(
            "storage") if pvc.spec.resources and pvc.spec.resources.requests else "-",
        "age": age_in_human_readable(pvc.metadata.creation_timestamp),
        "status_icon": status_emoji(pvc.status.phase)
    } for pvc in pvcs if not namespace_filter or pvc.metadata.namespace == namespace_filter]
    logger.info(f"Fetched {len(data)} persistent volume claims (filtered by namespace='{namespace_filter or 'All'}').")
    return pd.DataFrame(data)


# --------------------------------------------------
# Streamlit Tabs
# --------------------------------------------------
tab_overview, tab_resources, tab_metrics = st.tabs(["📊 Cluster Overview", "📋 Resources", "📈 Prometheus Metrics"])

# --- Cluster Overview Tab ---
with tab_overview:
    pods_df = get_pods()
    nodes_df = get_nodes()
    deps_df = get_deployments()
    svc_df = get_services()
    cm_df = get_configmaps()
    ss_df = get_statefulsets()
    ds_df = get_daemonsets()
    jobs_df = get_jobs()
    cron_df = get_cronjobs()
    pvs_df = get_persistent_volumes()
    pvcs_df = get_persistent_volume_claims()

    st.subheader("Cluster Summary")
    c1, c2, c3, c4, c5, c6, c7, c8, c9, c10 = st.columns(10)
    c1.metric("Total Pods", len(pods_df))
    c2.metric("🟢 Running", len(pods_df[pods_df["status"] == "Running"]))
    c3.metric("🟡 Pending", len(pods_df[pods_df["status"] == "Pending"]))
    c4.metric("🔴 Failed", len(pods_df[pods_df["status"] == "Failed"]))
    c5.metric("Nodes Ready/Total", f"{len(nodes_df[nodes_df['status'] == 'True'])}/{len(nodes_df)}")
    c6.metric("Deployments", len(deps_df))
    c7.metric("StatefulSets", len(ss_df))
    c8.metric("DaemonSets", len(ds_df))
    c9.metric("Services", len(svc_df))
    c10.metric("ConfigMaps", len(cm_df))
    c11, c12 = st.columns(2)
    c11.metric("PersistentVolumes", len(pvs_df))
    c12.metric("PersistentVolumeClaims", len(pvcs_df))

    st.divider()
    st.subheader("Pods per Namespace")
    if not pods_df.empty:
        counts = pods_df.groupby("namespace")["name"].count().reset_index()
        fig = px.bar(counts, x="namespace", y="name", labels={"name": "Pod count"}, title="Pods per Namespace")
        st.plotly_chart(fig, use_container_width=True)

# --- Resources Tab ---
with tab_resources:
    resource_type = st.selectbox("Select a resource", [
        "Pods", "Deployments", "Nodes", "StatefulSets", "DaemonSets",
        "Services", "ConfigMaps", "Jobs", "CronJobs",
        "PersistentVolumes", "PersistentVolumeClaims"
    ])

    resource_map = {
        "Pods": pods_df,
        "Deployments": deps_df,
        "Nodes": nodes_df,
        "StatefulSets": ss_df,
        "DaemonSets": ds_df,
        "Services": svc_df,
        "ConfigMaps": cm_df,
        "Jobs": jobs_df,
        "CronJobs": cron_df,
        "PersistentVolumes": pvs_df,
        "PersistentVolumeClaims": pvcs_df
    }

    df = resource_map.get(resource_type, pd.DataFrame())
    if not df.empty:
        st.dataframe(df, use_container_width=True)
    else:
        st.info("No resources found.")

# --- Prometheus Metrics Tab ---
with tab_metrics:
    st.subheader("Resource Usage Metrics")
    time_range = st.slider("Time range (minutes)", 5, 120, 30)
    step = st.selectbox("Step (seconds)", [15, 30, 60], index=1)

    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(minutes=time_range)
    start = int(start_time.timestamp())
    end = int(end_time.timestamp())

    # CPU Usage
    st.markdown("### 🔹 CPU Usage (cores per namespace)")
    cpu_query = 'sum by (namespace) (rate(container_cpu_usage_seconds_total{container!="",pod!=""}[5m]))'
    cpu_results = query_prometheus_range(prom_url, cpu_query, start, end, step)
    if cpu_results:
        df_cpu = pd.DataFrame([
            {"time": datetime.fromtimestamp(float(v[0])),
             "namespace": r["metric"].get("namespace", "unknown"),
             "cpu_cores": float(v[1])}
            for r in cpu_results for v in r["values"]
        ])
        fig_cpu = px.line(df_cpu, x="time", y="cpu_cores", color="namespace", title="CPU Usage Trend")
        st.plotly_chart(fig_cpu, use_container_width=True)
    else:
        st.info("No CPU metrics found.")

    # Memory Usage
    st.markdown("### 🔹 Memory Usage (MB per namespace)")
    mem_query = 'sum by (namespace) (container_memory_working_set_bytes * on(pod) group_left(namespace) kube_pod_info)'
    mem_results = query_prometheus_range(prom_url, mem_query, start, end, step)
    if mem_results:
        df_mem = pd.DataFrame([
            {
                "time": datetime.fromtimestamp(float(v[0])),
                "namespace": r["metric"].get("namespace", "unknown"),
                "memory_mb": float(v[1]) / 1024 / 1024,
            }
            for r in mem_results for v in r["values"]
        ])
        fig_mem = px.line(
            df_mem,
            x="time",
            y="memory_mb",
            color="namespace",
            title="Memory Usage Trend (per Namespace)",
            labels={"memory_mb": "Memory (MB)", "time": "Time"}
        )
        st.plotly_chart(fig_mem, use_container_width=True)
    else:
        st.info("No memory metrics found.")

# --- Auto-refresh ---
logger.info(f"Sleeping for {refresh_interval} seconds before potential refresh.")
time.sleep(refresh_interval)
if manual_refresh:
    logger.info("Manual refresh triggered by user.")
    st.rerun()
