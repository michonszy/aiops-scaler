import time
import requests
import os
import logging
import sys
from kubernetes import client, config
from kubernetes.client.rest import ApiException

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL")
GRAFANA_USER = os.getenv("GRAFANA_INSTANCE_ID")
GRAFANA_TOKEN = os.getenv("GRAFANA_ACCESS_TOKEN")
NAMESPACE = os.getenv("NAMESPACE", "sampleapp")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
METRIC_MAX_AGE_SECONDS = int(os.getenv("METRIC_MAX_AGE_SECONDS", "300"))
BASELINE_MIN_REPLICAS = int(os.getenv("BASELINE_MIN_REPLICAS", "1"))
LOWER_STEP = int(os.getenv("LOWER_STEP", "1"))


def get_target_replicas():
    endpoint = f"{PROMETHEUS_URL}/api/v1/query"
    params = {"query": "predicted_recommended_pods"}

    try:
        response = requests.get(
            endpoint, params=params, auth=(GRAFANA_USER, GRAFANA_TOKEN), timeout=10
        )
        response.raise_for_status()
        results = response.json()["data"]["result"]
    except Exception as e:
        logger.error(f"Error fetching metrics from Prometheus: {e}")
        return {}

    now = time.time()
    recommendations = {}
    for result in results:
        app_name = result["metric"].get("app")
        if not app_name:
            continue

        metric_ts = float(result["value"][0])
        age_seconds = now - metric_ts
        if age_seconds > METRIC_MAX_AGE_SECONDS:
            logger.warning(
                f"Stale recommendation for {app_name} ({age_seconds:.0f}s old), skipping"
            )
            continue

        target_pods = int(float(result["value"][1]))
        recommendations[app_name] = target_pods

    return recommendations


def compute_next_min_replicas(current_min, target_replicas, max_replicas):
    """
    Raise minReplicas immediately when load is predicted to increase.
    Lower gradually so transient dips do not drop the floor too fast.
    """
    safe_target = min(max(target_replicas, BASELINE_MIN_REPLICAS), max_replicas)

    if safe_target > current_min:
        return safe_target
    if safe_target < current_min:
        stepped_down = max(safe_target, current_min - LOWER_STEP)
        return max(stepped_down, BASELINE_MIN_REPLICAS)
    return current_min


def scale_hpas():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        logger.warning("Not running inside K8s. Falling back to local kubeconfig.")
        config.load_kube_config()

    autoscaling_api = client.AutoscalingV2Api()

    while True:
        logger.info("Polling for scaling recommendations...")
        recommendations = get_target_replicas()

        if not recommendations:
            logger.warning("No fresh recommendations found. Skipping cycle.")

        for app_name, target_replicas in recommendations.items():
            try:
                hpa_name = f"{app_name}-hpa"
                hpa = autoscaling_api.read_namespaced_horizontal_pod_autoscaler(
                    hpa_name, NAMESPACE
                )
                current_min = hpa.spec.min_replicas
                max_replicas = hpa.spec.max_replicas

                next_min = compute_next_min_replicas(
                    current_min, target_replicas, max_replicas
                )

                if current_min != next_min:
                    direction = "Raising" if next_min > current_min else "Lowering"
                    logger.info(
                        f"{direction} HPA floor for {app_name}: "
                        f"minReplicas {current_min} -> {next_min} "
                        f"(predicted target={target_replicas})"
                    )
                    hpa.spec.min_replicas = next_min
                    autoscaling_api.patch_namespaced_horizontal_pod_autoscaler(
                        hpa_name, NAMESPACE, hpa
                    )
                else:
                    logger.info(
                        f"{app_name}-hpa floor unchanged at {current_min} "
                        f"(predicted target={target_replicas})"
                    )

            except ApiException as e:
                if e.status == 404:
                    logger.error(
                        f"HPA '{hpa_name}' not found in namespace '{NAMESPACE}'. "
                        "Cannot patch floor."
                    )
                else:
                    logger.error(f"Kubernetes API error scaling {hpa_name}: {e.reason}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logger.info("Starting Custom AIOps HPA Patcher...")
    scale_hpas()
