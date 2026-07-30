import requests
import pandas as pd
import numpy as np
import os
import time
import sys
import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import cast
from xgboost import XGBRegressor
from prometheus_client import start_http_server, Gauge  # Gauge kept for RECOMMENDED_PODS_GAUGE

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROMETHEUS_URL  = os.getenv("PROMETHEUS_URL")
GRAFANA_USER    = os.getenv("GRAFANA_INSTANCE_ID")
GRAFANA_TOKEN   = os.getenv("GRAFANA_ACCESS_TOKEN")
NAMESPACE       = os.getenv("NAMESPACE", "sampleapp")
LOOKBACK_HOURS  = int(os.getenv("LOOKBACK_HOURS", "24"))
HORIZON_MINUTES = int(os.getenv("HORIZON_MINUTES", "5"))
# LAG_MINUTES is independent of HORIZON_MINUTES: 30 lags give the model
# enough temporal context to learn intra-day patterns.
LAG_MINUTES            = int(os.getenv("LAG_MINUTES", "30"))
LOOP_INTERVAL_SECONDS  = int(os.getenv("LOOP_INTERVAL_SECONDS", str(HORIZON_MINUTES * 60)))
TRAIN_INTERVAL_SECONDS = int(os.getenv("TRAIN_INTERVAL_SECONDS", "0"))
TRAFFIC_SIMILARITY_FRACTION = float(os.getenv("TRAFFIC_SIMILARITY_FRACTION", "0.2"))
REPLICA_HISTORY_PERCENTILE  = float(os.getenv("REPLICA_HISTORY_PERCENTILE", "90"))
# Fallback cap used only when HPA maxReplicas is unavailable.
REPLICA_MAX_SCALE_MULTIPLIER = float(os.getenv("REPLICA_MAX_SCALE_MULTIPLIER", "2.0"))
REPLICA_MIN_SCORED_FORECASTS = int(os.getenv("REPLICA_MIN_SCORED_FORECASTS", "1"))
# REPLICA_MAX_THEIL_U: block replica recommendations when the model is worse than
# a naïve last-value baseline (Theil U ≥ 1).  1.0 is the natural threshold;
# a small buffer (1.1) avoids flapping right at the boundary.
REPLICA_MAX_THEIL_U          = float(os.getenv("REPLICA_MAX_THEIL_U", "1.1"))
# REPLICA_HEADROOM: scale the history-based pod count up by this factor before
# applying the ceiling.  Compensates for the fact that replica observations at
# similar traffic levels were captured during HPA ramp-up (not at settled
# capacity), so the raw p90 systematically under-provisions.
# 1.0 = no headroom (original behaviour); 1.2 = 20% buffer (recommended).
REPLICA_HEADROOM             = float(os.getenv("REPLICA_HEADROOM", "1.2"))
FORECAST_ERROR_EMA_ALPHA     = float(os.getenv("FORECAST_ERROR_EMA_ALPHA", "0.3"))
SAMPLE_WEIGHT_MAX            = float(os.getenv("SAMPLE_WEIGHT_MAX", "3.0"))
# Actual traffic above this percentile of the recent window counts as a "spike".
# p90 keeps the threshold well above normal variance, reducing false alarms on
# low-traffic services where p75 sits inside everyday noise.
SPIKE_PERCENTILE = float(os.getenv("SPIKE_PERCENTILE", "90"))
# How many minutes of recent traffic to use when computing the spike threshold.
# A short window (default 60 min) reflects current traffic conditions so the
# threshold tracks the session baseline rather than the full 24h history.
# Using the full lookback caused the threshold to be set by historical peaks
# that current traffic never reached (all FN became TN); freezing it caused
# the opposite problem when sessions started at lower traffic (all FP).
SPIKE_THRESHOLD_WINDOW_MINUTES = int(os.getenv("SPIKE_THRESHOLD_WINDOW_MINUTES", "60"))
# Theil U must exceed this on two consecutive scored forecasts to trigger an
# early retrain (concept drift detection).  Lowered from 1.5 to 1.1 so that
# the broad band of services sitting between 1.1 and 1.49 (worse than naïve
# but below the old threshold) also trigger an early retrain.
DRIFT_THEIL_U_THRESHOLD = float(os.getenv("DRIFT_THEIL_U_THRESHOLD", "1.1"))
HPA_REFRESH_LOOPS       = max(1, int(os.getenv("HPA_REFRESH_LOOPS", "5")))
# MAPE_MIN_ACTUAL: actuals below this floor are excluded from MAPE to avoid
# division-by-near-zero inflating the EMA with meaningless percentages.
MAPE_MIN_ACTUAL = float(os.getenv("MAPE_MIN_ACTUAL", "0.01"))
RESOLUTION = "1m"
# Lag depth for the primary signal (net_rx_packets) and secondary signals.
PRIMARY_LAGS   = LAG_MINUTES
SECONDARY_LAGS = max(1, LAG_MINUTES // 3)
# Name of the primary signal — prediction target and replica-history lookup key.
PRIMARY_SIGNAL = "net_rx_packets"

# ---------------------------------------------------------------------------
# Prometheus gauges
# ---------------------------------------------------------------------------
RECOMMENDED_PODS_GAUGE = Gauge("predicted_recommended_pods", "Suggested minReplicas per app", ["app"])

# ---------------------------------------------------------------------------
# Per-app state
# ---------------------------------------------------------------------------
@dataclass
class AppState:
    """All mutable state for a single deployment, keyed by app name."""
    # Pending forecasts waiting to be scored once HORIZON_MINUTES elapses
    pending_forecasts: list = field(default_factory=list)
    # Last scored forecast (used for adaptive sample weights)
    last_scored: dict = field(default_factory=dict)
    # EMA metrics
    mae_ema:  float | None = None
    mape_ema: float | None = None
    sq_error_ema: float | None = None   # → sqrt = RMSE EMA
    dir_acc_ema:  float | None = None
    scored_count: int = 0
    # Theil U from the two most recent scored forecasts — used for drift detection.
    last_theil_u: float | None = None
    prev_theil_u: float | None = None
    # Full-history arrays for batch metrics (R², Theil U, F1)
    actuals:     list[float] = field(default_factory=list)
    predictions: list[float] = field(default_factory=list)
    naive_preds: list[float] = field(default_factory=list)  # random-walk baseline
    spike_threshold: float = 0.0
    # Confusion matrix for scaling decisions
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    # Cached trained model
    model: XGBRegressor | None = None
    model_trained_at: float = 0.0
    # Signal columns present at last training — retrain when this changes
    last_signal_cols: frozenset = field(default_factory=frozenset)

_app_state: dict[str, AppState] = {}

def _state(app: str) -> AppState:
    if app not in _app_state:
        _app_state[app] = AppState()
    return _app_state[app]

# ---------------------------------------------------------------------------
# HPA data
# ---------------------------------------------------------------------------
@dataclass
class HpaData:
    replicas:     pd.Series      # current_replicas time-series
    max_replicas: int | None     # spec maxReplicas, None if unavailable

# ---------------------------------------------------------------------------
# Prometheus helpers
# ---------------------------------------------------------------------------
def _prom_get(endpoint: str, params: dict) -> list:
    """Shared HTTP call to Prometheus; returns result list or [] on error."""
    try:
        resp = requests.get(
            endpoint, params=params,
            auth=(GRAFANA_USER, GRAFANA_TOKEN), timeout=10
        )
        resp.raise_for_status()
        return resp.json()["data"]["result"]
    except Exception as e:
        logger.error(f"Prometheus error ({endpoint}): {e}")
        return []


def _parse_range_series(results: list) -> pd.Series:
    """Turn a Prometheus range result into a single combined pd.Series."""
    frames = []
    for result in results:
        df = pd.DataFrame(result["values"], columns=["timestamp", "value"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        df["value"] = df["value"].astype(float)
        df.set_index("timestamp", inplace=True)
        frames.append(df["value"])
    if not frames:
        return pd.Series(dtype=float)
    combined = pd.concat(frames, axis=1, sort=False).sum(axis=1)
    return combined

# ---------------------------------------------------------------------------
# Traffic queries
# ---------------------------------------------------------------------------
# Raw Prometheus queries — only metrics that map directly to a single series.
# Derived signals (rx_bytes_per_packet) are computed in fetch_signals() from
# these raw results to avoid double-querying.
#
# PRIMARY_SIGNAL (net_rx_packets) is the prediction target; others are features.
_RAW_QUERIES: dict[str, str] = {
    "net_rx_packets": (
        'sum(rate(container_network_receive_packets_total'
        '{{namespace="{ns}",pod=~"{app}-[a-z0-9]+-[a-z0-9]+"}}[2m]))'
    ),
    "net_rx_bytes": (
        'sum(rate(container_network_receive_bytes_total'
        '{{namespace="{ns}",pod=~"{app}-[a-z0-9]+-[a-z0-9]+"}}[2m]))'
    ),
    "cpu": (
        'sum(rate(container_cpu_usage_seconds_total'
        '{{namespace="{ns}",pod=~"{app}-[a-z0-9]+-[a-z0-9]+",container!="",container!="POD"}}[2m]))'
    ),
}

# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
def fetch_signals(app: str, start_time: datetime, end_time: datetime) -> pd.DataFrame:
    """
    Fetch raw signals and derive composite features; return a time-aligned DataFrame.

    Raw queries fetched:
      net_rx_packets  — packets/s; primary signal and prediction target
      net_rx_bytes    — bytes/s; used only to derive rx_bytes_per_packet
      cpu             — CPU cores consumed; processing-cost signal

    Derived features added after alignment:
      rx_bytes_per_packet — payload-size proxy (bytes / packets); distinguishes
                            bulk transfers from RPS spikes. Computed here to avoid
                            a separate Prometheus query.

    net_rx_packets is mandatory — returns empty DataFrame if unavailable.
    Other raw signals degrade gracefully when missing.
    net_rx_bytes is not kept as a model feature (subsumed by the ratio).
    """
    base = f"{PROMETHEUS_URL}/api/v1/query_range"
    common_params = {"start": start_time.timestamp(), "end": end_time.timestamp(), "step": RESOLUTION}

    raw: dict[str, pd.Series] = {}
    for name, template in _RAW_QUERIES.items():
        query = template.format(ns=NAMESPACE, app=app)
        series = _parse_range_series(_prom_get(base, {**common_params, "query": query}))
        if series.empty:
            if name == PRIMARY_SIGNAL:
                logger.warning(f"[{app}] No {PRIMARY_SIGNAL} data — skipping app.")
                return pd.DataFrame()
            logger.warning(f"[{app}] Raw signal '{name}' returned no data — omitting.")
        else:
            raw[name] = series

    # Align all raw series on a common 1-minute index; forward-fill gaps.
    df = pd.concat(raw, axis=1, sort=False).sort_index()
    df.ffill(inplace=True)
    df.fillna(0, inplace=True)

    # Derive rx_bytes_per_packet — only when both raw series are present.
    if "net_rx_bytes" in df.columns and PRIMARY_SIGNAL in df.columns:
        packets = df[PRIMARY_SIGNAL].replace(0, float("nan"))
        df["rx_bytes_per_packet"] = (df["net_rx_bytes"] / packets).fillna(0)

    # Drop net_rx_bytes — subsumed by the ratio; keeping it would add redundant
    # correlated features that increase noise without information gain.
    df.drop(columns=["net_rx_bytes"], inplace=True, errors="ignore")

    logger.info(f"[{app}] Signals loaded: {list(df.columns)}")
    return df


def fetch_hpa_targets(end_time: datetime) -> dict[str, int | None]:
    """
    Return a mapping of deployment name -> maxReplicas for every HPA in NAMESPACE.
    A single instant query fetches both the app list and maxReplicas in one call.
    """
    results = _prom_get(
        f"{PROMETHEUS_URL}/api/v1/query",
        {"query": f'kube_horizontalpodautoscaler_spec_max_replicas{{namespace="{NAMESPACE}"}}',
         "time": end_time.timestamp()}
    )
    out: dict[str, int | None] = {}
    for r in results:
        hpa_name = r["metric"].get("horizontalpodautoscaler", "")
        if not hpa_name:
            continue
        app = hpa_name.removesuffix("-hpa")
        out[app] = int(float(r["value"][1]))
    return out


def fetch_app_hpa_replicas(app: str, start_time: datetime, end_time: datetime,
                           max_replicas: int | None) -> HpaData:
    """Fetch current-replicas history for this app's HPA (range query only)."""
    sel = f'namespace="{NAMESPACE}",horizontalpodautoscaler=~"{app}(-hpa)?"'

    rep_results = _prom_get(f"{PROMETHEUS_URL}/api/v1/query_range", {
        "query": f"kube_horizontalpodautoscaler_status_current_replicas{{{sel}}}",
        "start": start_time.timestamp(), "end": end_time.timestamp(), "step": RESOLUTION,
    })
    frames = []
    for r in rep_results:
        df = pd.DataFrame(r["values"], columns=["timestamp", "replicas"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        df["replicas"] = df["replicas"].astype(float)
        df.set_index("timestamp", inplace=True)
        frames.append(df["replicas"])
    replicas_series = pd.Series(dtype=float)
    if frames:
        replicas_series = pd.concat(frames, axis=1, sort=False).max(axis=1)
        replicas_series.ffill(inplace=True)

    return HpaData(replicas=replicas_series, max_replicas=max_replicas)

# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------
def build_dataset(df: pd.DataFrame, lag_mins: int, horizon_mins: int) -> pd.DataFrame:
    """
    Build XGBoost feature matrix from a multi-signal DataFrame.

    Primary signal (net_rx_packets) gets PRIMARY_LAGS lag columns.
    Secondary signals get SECONDARY_LAGS lag columns each.
    Target is future net_rx_packets (shifted -horizon_mins).
    """
    df = df.copy()
    df.ffill(inplace=True)
    df.fillna(0, inplace=True)
    out = pd.DataFrame(index=df.index)

    primary   = PRIMARY_SIGNAL if PRIMARY_SIGNAL in df.columns else df.columns[0]
    secondary = [c for c in df.columns if c != primary]

    # Primary signal: full lag window
    for lag in range(1, PRIMARY_LAGS + 1):
        out[f"{primary}_lag_{lag}m"] = df[primary].shift(lag)
    out[f"{primary}_current"] = df[primary]

    # Secondary signals: shorter lag window
    for col in secondary:
        for lag in range(1, SECONDARY_LAGS + 1):
            out[f"{col}_lag_{lag}m"] = df[col].shift(lag)
        out[f"{col}_current"] = df[col]

    # Rolling statistics on the primary signal — give the model an explicit
    # "is traffic currently ramping?" signal that calendar features cannot capture.
    out[f"{primary}_rolling_mean_5m"] = df[primary].rolling(5, min_periods=1).mean()
    out[f"{primary}_rolling_std_5m"]  = df[primary].rolling(5, min_periods=1).std().fillna(0)

    # Calendar features
    out["hour"]          = df.index.hour
    out["day_of_week"]   = df.index.dayofweek
    out["minute_of_day"] = df.index.hour * 60 + df.index.minute

    # Target: future value of the primary signal
    out["target"] = df[primary].shift(-horizon_mins)
    out.dropna(inplace=True)
    return out

# ---------------------------------------------------------------------------
# Model training
# ---------------------------------------------------------------------------
def _build_sample_weights(st: AppState, X_train: pd.DataFrame) -> np.ndarray:
    weights = np.ones(len(X_train), dtype=float)
    last = st.last_scored
    if not last:
        return weights

    ref      = last["origin_traffic"]
    boost    = 1.0 + min(last["abs_error"] / max(ref, 1e-9), 1.0)
    # Use a scale-relative tolerance (same fix as recommend_replicas_from_history)
    current_col = f"{PRIMARY_SIGNAL}_current" if f"{PRIMARY_SIGNAL}_current" in X_train.columns else X_train.columns[0]
    sig_range  = float(X_train[current_col].max() - X_train[current_col].min())
    floor      = max(sig_range * 0.01, 1e-9)
    tolerance  = max(ref * TRAFFIC_SIMILARITY_FRACTION, floor)
    similar    = (X_train[current_col] - ref).abs() <= tolerance
    weights[similar.to_numpy()] *= boost
    weights = np.minimum(weights, SAMPLE_WEIGHT_MAX)

    if similar.sum() > 0:
        logger.info(f"Sample weights: boosted {similar.sum()} rows "
                    f"(ref={ref:.4g}, boost={boost:.2f})")
    return weights


def get_or_train_model(app: str, dataset: pd.DataFrame) -> XGBRegressor | None:
    """Retrain model on every call (TRAIN_INTERVAL_SECONDS=0); skip only on signal-set change guard."""
    st  = _state(app)
    now = time.monotonic()

    feature_cols  = [c for c in dataset.columns if c != "target"]
    current_cols  = frozenset(feature_cols)
    signal_changed = current_cols != st.last_signal_cols and st.model is not None

    if signal_changed:
        logger.info(f"[{app}] Signal set changed — forcing model retrain.")

    # Drift detection: if Theil U has been above DRIFT_THEIL_U_THRESHOLD on both
    # of the last two scored forecasts the model is actively getting worse than a
    # naïve baseline — force an immediate retrain rather than waiting for the
    # scheduled interval.
    drift_detected = (
        st.model is not None
        and st.last_theil_u is not None
        and st.prev_theil_u is not None
        and st.last_theil_u >= DRIFT_THEIL_U_THRESHOLD
        and st.prev_theil_u >= DRIFT_THEIL_U_THRESHOLD
    )
    if drift_detected:
        logger.warning(
            f"[{app}] Concept drift detected "
            f"(Theil U: {st.prev_theil_u:.3f} → {st.last_theil_u:.3f} >= {DRIFT_THEIL_U_THRESHOLD})"
            f" — forcing early retrain."
        )
        # Reset drift sentinels so we don't retrain every loop until improvement shows.
        st.last_theil_u = None
        st.prev_theil_u = None

    if st.model is not None and not signal_changed and not drift_detected and (now - st.model_trained_at) < TRAIN_INTERVAL_SECONDS:
        return st.model

    min_rows = LAG_MINUTES + HORIZON_MINUTES + 1
    if len(dataset) < min_rows:
        rec = math.ceil((2 * (LAG_MINUTES + HORIZON_MINUTES) + 1) / 60)
        logger.warning(f"[{app}] Insufficient history "
                       f"(rows={len(dataset)}, need>={min_rows}, recommend>={rec}h lookback)")
        return None

    X = dataset[feature_cols].iloc[:-1]
    y = dataset["target"].iloc[:-1]

    t0 = time.monotonic()
    model = XGBRegressor(n_estimators=150, max_depth=5, learning_rate=0.05, n_jobs=-1)
    model.fit(X, y, sample_weight=_build_sample_weights(st, X))
    logger.info(f"[{app}] Model retrained in {time.monotonic()-t0:.1f}s "
                f"({len(X)} rows, {len(feature_cols)} features)")

    st.model, st.model_trained_at, st.last_signal_cols = model, now, current_cols
    return model


def predict_traffic(app: str, dataset: pd.DataFrame) -> float | None:
    model = get_or_train_model(app, dataset)
    if model is None:
        return None
    feature_cols = [c for c in dataset.columns if c != "target"]
    return max(0.0, float(model.predict(dataset[feature_cols].iloc[-1:])[0]))

# ---------------------------------------------------------------------------
# Forecast scoring
# ---------------------------------------------------------------------------
def _ema(prev: float | None, sample: float, alpha: float) -> float:
    return sample if prev is None else alpha * sample + (1 - alpha) * prev


def _compute_batch_metrics(st: AppState) -> tuple[float, float, float, float, float]:
    """Return (r2, theil_u, precision, recall, f1) from full-history arrays."""
    a = np.array(st.actuals,     dtype=float)
    p = np.array(st.predictions, dtype=float)
    n = np.array(st.naive_preds, dtype=float)

    if len(a) < 2:
        nan = float("nan")
        return nan, nan, nan, nan, nan

    ss_res = float(np.sum((a - p) ** 2))
    ss_tot = float(np.sum((a - a.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    rmse_m = math.sqrt(float(np.mean((a - p) ** 2)))
    rmse_n = math.sqrt(float(np.mean((a - n) ** 2)))
    theil_u = rmse_m / rmse_n if rmse_n > 0 else float("nan")

    tp, fp, fn = st.tp, st.fp, st.fn
    precision = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
    recall    = tp / (tp + fn) if (tp + fn) > 0 else float("nan")
    f1 = (2 * precision * recall / (precision + recall)
          if not (math.isnan(precision) or math.isnan(recall)) and (precision + recall) > 0
          else float("nan"))

    return r2, theil_u, precision, recall, f1


def score_pending_forecasts(app: str, traffic_series: pd.Series) -> None:
    """Score any forecast whose horizon has elapsed; update all metrics."""
    traffic = traffic_series.ffill().dropna()
    if traffic.empty:
        return

    st = _state(app)
    a  = FORECAST_ERROR_EMA_ALPHA
    still_pending = []

    for item in st.pending_forecasts:
        target_time = item["origin"] + timedelta(minutes=HORIZON_MINUTES)
        if target_time > traffic.index[-1]:
            still_pending.append(item)
            continue

        target_loc = traffic.index.get_indexer([target_time], method="nearest")[0]
        if target_loc < 0:
            still_pending.append(item)
            continue

        actual    = float(traffic.iloc[target_loc])
        predicted = item["predicted"]
        error     = actual - predicted
        abs_error = abs(error)

        origin_loc     = traffic.index.get_indexer([item["origin"]], method="nearest")[0]
        origin_traffic = float(traffic.iloc[origin_loc]) if origin_loc >= 0 else 0.0

        st.last_scored = {"origin": item["origin"], "predicted": predicted,
                          "actual": actual, "error": error, "abs_error": abs_error,
                          "origin_traffic": origin_traffic}

        # EMA metrics
        st.mae_ema      = _ema(st.mae_ema,      abs_error, a)
        # Guard against near-zero actuals (e.g. idle services) inflating MAPE.
        # Samples where actual < MAPE_MIN_ACTUAL are excluded from the MAPE EMA
        # rather than being counted as 0%, which would deflate it just as badly.
        if actual >= MAPE_MIN_ACTUAL:
            st.mape_ema = _ema(st.mape_ema, abs_error / actual, a)
        st.sq_error_ema = _ema(st.sq_error_ema,  error ** 2, a)

        flat  = max(origin_traffic * 0.01, 1e-6)
        a_up  = (actual    - origin_traffic) > flat
        a_dn  = (actual    - origin_traffic) < -flat
        p_up  = (predicted - origin_traffic) > flat
        p_dn  = (predicted - origin_traffic) < -flat
        dir_ok = float((a_up and p_up) or (a_dn and p_dn)
                       or (not a_up and not a_dn and not p_up and not p_dn))
        st.dir_acc_ema = _ema(st.dir_acc_ema, dir_ok, a)

        st.scored_count += 1

        # Full-history arrays
        st.actuals.append(actual)
        st.predictions.append(predicted)
        st.naive_preds.append(origin_traffic)

        # Confusion matrix
        thr = st.spike_threshold
        a_spike = actual    > thr
        p_spike = predicted > thr
        if   a_spike and p_spike:      st.tp += 1
        elif not a_spike and p_spike:  st.fp += 1
        elif a_spike and not p_spike:  st.fn += 1
        else:                          st.tn += 1

        _, theil_u, _, _, _ = _compute_batch_metrics(st)

        # Track rolling Theil U for drift detection in get_or_train_model.
        if not math.isnan(theil_u):
            st.prev_theil_u = st.last_theil_u
            st.last_theil_u = theil_u

        logger.info(
            f"[{app}] Forecast #{st.scored_count}: "
            f"predicted={predicted:.4g}, actual={actual:.4g}, "
            f"error={error:+.4g} (MAPE={'n/a' if st.mape_ema is None else f'{st.mape_ema*100:.1f}%'}), "
            f"RMSE_ema={math.sqrt(st.sq_error_ema):.4g}, "
            f"dir={'✓' if dir_ok else '✗'} (acc={st.dir_acc_ema:.2f})"
        )

    st.pending_forecasts = still_pending


def record_forecast(app: str, origin: datetime, predicted: float) -> None:
    _state(app).pending_forecasts.append({"origin": origin, "predicted": predicted})

# ---------------------------------------------------------------------------
# Replica recommendation
# ---------------------------------------------------------------------------
def recommend_replicas_from_history(app: str, traffic_series: pd.Series,
                                    app_replicas: pd.Series,
                                    predicted_traffic: float) -> int | None:
    """
    Return the p90 replica count observed during historical minutes with
    similar traffic to `predicted_traffic`. p90 captures the settled HPA
    count, not the low readings during the ramp-up that min() would return.
    """
    traffic_series = traffic_series.ffill().fillna(0)
    if traffic_series.empty or app_replicas.empty:
        return None

    # Round both indices to the minute so minor Prometheus scrape-time jitter
    # (sub-second offsets between different metric paths) does not produce an
    # empty intersection and silently drop the recommendation.
    traffic_series = traffic_series.copy()
    traffic_series.index = traffic_series.index.floor("1min")
    app_replicas = app_replicas.copy()
    app_replicas.index = app_replicas.index.floor("1min")

    common_idx = traffic_series.index.intersection(app_replicas.index)
    if common_idx.empty:
        return None

    t = traffic_series.loc[common_idx]
    r = app_replicas.loc[common_idx]

    sig_range = float(t.max() - t.min())
    floor     = max(sig_range * 0.01, 1e-9)
    tolerance = max(predicted_traffic * TRAFFIC_SIMILARITY_FRACTION, floor)

    candidates = r[(t - predicted_traffic).abs() <= tolerance]
    if candidates.empty:
        nearest = (t - predicted_traffic).abs().nsmallest(min(15, len(t))).index
        candidates = r.loc[nearest]
        logger.info(f"[{app}] No traffic within ±{tolerance:.4g}; "
                    f"using {len(candidates)} nearest minutes")
    else:
        logger.info(f"[{app}] {len(candidates)} minutes within ±{tolerance:.4g} "
                    f"of predicted {predicted_traffic:.4g}")

    return int(math.ceil(np.percentile(candidates.values, REPLICA_HISTORY_PERCENTILE)))

# ---------------------------------------------------------------------------
# Accuracy summary log line
# ---------------------------------------------------------------------------
def log_accuracy_summary(apps: list[str], ts: datetime) -> None:
    """
    Emit one machine-parseable ACCURACY_SUMMARY line per app.
    Extract to CSV:
        grep ACCURACY_SUMMARY predictor.log | \\
        python3 -c "import sys,re,pandas as pd; \\
        print(pd.DataFrame([dict(re.findall(r'(\\w+)=([^\\s]+)',l)) \\
        for l in sys.stdin]).to_csv(index=False))"
    """
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    for app in apps:
        st = _state(app)
        if st.scored_count == 0:
            continue
        rmse_ema = math.sqrt(st.sq_error_ema) if st.sq_error_ema is not None else float("nan")
        r2, theil_u, precision, recall, f1 = _compute_batch_metrics(st)
        logger.info(
            f"ACCURACY_SUMMARY ts={ts_str} app={app} n={st.scored_count} "
            f"mae_ema={st.mae_ema:.6g} mape_ema={'n/a' if st.mape_ema is None else f'{st.mape_ema:.4f}'} "
            f"rmse_ema={rmse_ema:.6g} dir_acc={st.dir_acc_ema:.4f} "
            f"r2={r2:.4f} theil_u={theil_u:.4f} "
            f"precision={precision:.4f} recall={recall:.4f} f1={f1:.4f} "
            f"tp={st.tp} fp={st.fp} fn={st.fn} tn={st.tn} "
            f"spike_threshold={st.spike_threshold:.6g}"
        )

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    rec_lookback = math.ceil((2 * (LAG_MINUTES + HORIZON_MINUTES) + 1) / 60)
    logger.info(
        f"Starting Predictor on port 8000 "
        f"(namespace={NAMESPACE}, lookback={LOOKBACK_HOURS}h, "
        f"horizon={HORIZON_MINUTES}m, lag={LAG_MINUTES}m, loop={LOOP_INTERVAL_SECONDS}s)"
    )
    if LOOKBACK_HOURS < rec_lookback:
        logger.warning(f"LOOKBACK_HOURS={LOOKBACK_HOURS} is low; recommend >={rec_lookback}h")
    # Lead-time check: warn when the prediction horizon is too tight to account
    # for scheduling lag (scaler poll) + HPA actuator delay + pod startup time.
    # Rule of thumb: HORIZON_MINUTES should exceed (LOOP_INTERVAL_SECONDS/60 + 3).
    rec_horizon = math.ceil(LOOP_INTERVAL_SECONDS / 60) + 3
    if HORIZON_MINUTES < rec_horizon:
        logger.warning(
            f"HORIZON_MINUTES={HORIZON_MINUTES} may be too short: scaler poll "
            f"({LOOP_INTERVAL_SECONDS}s loop) + HPA actuator + pod startup typically "
            f"needs >={rec_horizon}m of lead time. Consider HORIZON_MINUTES={rec_horizon}."
        )
    start_http_server(8000)

    # Slow-changing state cached across loops.
    _loop_count:    int = 0
    _actionable:    list[str] = []
    _hpa_cache:     dict[str, HpaData] = {}   # app -> HpaData (maxReplicas + replica history)
    _gauges_inited: set[str] = set()           # apps whose gauges have been pre-initialised

    while True:
        loop_start = time.monotonic()
        now_utc    = datetime.now(timezone.utc)
        logger.info("Running evaluation loop...")

        end_time   = now_utc
        start_time = end_time - timedelta(hours=LOOKBACK_HOURS)

        # --- Slow path: refresh HPA targets + replica history every N loops ---
        if _loop_count % HPA_REFRESH_LOOPS == 0:
            hpa_targets = fetch_hpa_targets(end_time)   # app -> maxReplicas, single instant query
            discovered  = sorted(hpa_targets.keys())
            if not discovered:
                logger.warning(f"No HPA-backed deployments in namespace '{NAMESPACE}'. "
                               "Is kube-state-metrics running?")
                _loop_count += 1
                time.sleep(LOOP_INTERVAL_SECONDS)
                continue
            if discovered != _actionable:
                logger.info(f"HPA targets updated: {_actionable} -> {discovered}")
            _actionable = discovered

            for app in _actionable:
                max_rep = hpa_targets.get(app)
                _hpa_cache[app] = fetch_app_hpa_replicas(app, start_time, end_time, max_rep)

            # Pre-initialise gauge labels for any newly discovered app.
            for app in _actionable:
                if app not in _gauges_inited:
                    RECOMMENDED_PODS_GAUGE.labels(app=app)
                    _gauges_inited.add(app)

            logger.info(f"Processing {len(_actionable)} HPA-backed deployment(s): {', '.join(_actionable)}")

        if not _actionable:
            logger.warning(f"No HPA-backed deployments in namespace '{NAMESPACE}'. "
                           "Is kube-state-metrics running?")
            _loop_count += 1
            time.sleep(LOOP_INTERVAL_SECONDS)
            continue

        _loop_count += 1

        for app in _actionable:
            logger.info(f"[{app}] Processing...")

            signals = fetch_signals(app, start_time, end_time)
            if signals.empty:
                continue

            primary_series = pd.Series(signals[PRIMARY_SIGNAL], dtype=float)

            # Compute spike threshold from a short trailing window so it tracks
            # the current session's traffic baseline.  Using the full 24h lookback
            # set the bar from historical peaks that current traffic never reached
            # (spikes registered as TN instead of FN/TP).  A rolling 60-min window
            # rises and falls with the session without the runaway drift that the
            # full-lookback rolling threshold had.
            st_early = _state(app)
            recent = primary_series.ffill().fillna(0)
            if SPIKE_THRESHOLD_WINDOW_MINUTES < len(recent):
                recent = recent.iloc[-SPIKE_THRESHOLD_WINDOW_MINUTES:]
            st_early.spike_threshold = float(
                np.percentile(recent.to_numpy(), SPIKE_PERCENTILE)
            )

            hpa = _hpa_cache.get(app, HpaData(replicas=pd.Series(dtype=float), max_replicas=None))
            if hpa.replicas.empty:
                logger.warning(f"[{app}] No HPA replica data — skipping replica recommendation.")

            dataset = build_dataset(signals, LAG_MINUTES, HORIZON_MINUTES)
            score_pending_forecasts(app, primary_series)
            future_prediction = predict_traffic(app, dataset)
            if future_prediction is None:
                continue

            current_traffic = float(primary_series.ffill().iloc[-1])
            record_forecast(app, cast(pd.Timestamp, dataset.index[-1]).floor("us").to_pydatetime(), future_prediction)
            logger.info(f"[{app}] current={current_traffic:.4g}, predicted={future_prediction:.4g}")

            if not hpa.replicas.empty:
                st = _state(app)
                scored_ok  = st.scored_count >= REPLICA_MIN_SCORED_FORECASTS
                # Gate on Theil U instead of MAPE: Theil U < 1 means the model beats
                # a naïve last-value baseline and is safe to act on.  MAPE is
                # unreliable on low-traffic services (near-zero denominator spikes).
                # Pass through while Theil U is still None (warm-up: not yet 2 scored
                # forecasts) so the very first recommendation is not blocked.
                theil_ok   = st.last_theil_u is None or st.last_theil_u <= REPLICA_MAX_THEIL_U
                if not scored_ok:
                    logger.warning(f"[{app}] Skipping recommendation: "
                                   f"only {st.scored_count} scored forecast(s), "
                                   f"need >={REPLICA_MIN_SCORED_FORECASTS}")
                elif not theil_ok:
                    logger.warning(f"[{app}] Skipping recommendation: "
                                   f"Theil U={st.last_theil_u:.3f} > threshold {REPLICA_MAX_THEIL_U:.2f} "
                                   f"(model worse than naive baseline)")
                else:
                    pods = recommend_replicas_from_history(
                        app, primary_series, hpa.replicas, future_prediction
                    )
                    if pods is not None:
                        current_replicas = int(hpa.replicas.ffill().iloc[-1])
                        if hpa.max_replicas is not None:
                            ceiling = hpa.max_replicas
                            ceiling_src = f"HPA maxReplicas={ceiling}"
                        else:
                            ceiling = max(1, math.ceil(current_replicas * REPLICA_MAX_SCALE_MULTIPLIER))
                            ceiling_src = f"fallback {REPLICA_MAX_SCALE_MULTIPLIER}x current={current_replicas}"
                        pods_with_headroom = math.ceil(pods * REPLICA_HEADROOM)
                        safe = max(1, min(pods_with_headroom, ceiling))
                        if safe < pods_with_headroom:
                            logger.info(f"[{app}]  -> Capped: {pods_with_headroom} -> {safe} ({ceiling_src})")
                        RECOMMENDED_PODS_GAUGE.labels(app=app).set(safe)
                        logger.info(
                            f"[{app}]  -> Suggested minReplicas: {safe} "
                            f"(history p{REPLICA_HISTORY_PERCENTILE:.0f}={pods}, "
                            f"headroom x{REPLICA_HEADROOM}={pods_with_headroom})"
                        )
                    else:
                        logger.warning(f"[{app}] No replica recommendation produced.")

        log_accuracy_summary(_actionable, now_utc)

        elapsed    = time.monotonic() - loop_start
        sleep_secs = max(0.0, LOOP_INTERVAL_SECONDS - elapsed)
        logger.info(f"Loop done in {elapsed:.1f}s, sleeping {sleep_secs:.0f}s...")
        time.sleep(sleep_secs)
