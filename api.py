"""
api.py — FraudShield FastAPI Backend
======================================
Prometheus metrics auto-exposed at GET /metrics
Model metrics at GET /model-metrics
SHAP importances at GET /shap
Real-time prediction at POST /predict
"""

import os
import pickle
import json
import time as time_module
import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import redis

# Prometheus instrumentation
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Histogram, Gauge

# Flink state reader
from flink_processor import get_user_flink_state, get_stream_log

# ──────────────────────────────────────────────
# Custom Prometheus Metrics
# ──────────────────────────────────────────────
fraud_predictions_total = Counter(
    "fraud_predictions_total",
    "Total fraud predictions made",
    ["verdict"]          # labels: 'fraud' or 'legit'
)

prediction_latency_ms = Histogram(
    "prediction_latency_milliseconds",
    "End-to-end prediction latency in ms",
    buckets=[1, 2, 5, 10, 20, 50, 100, 200, 500]
)

redis_latency_ms = Histogram(
    "redis_lookup_latency_milliseconds",
    "Redis state lookup latency in ms",
    buckets=[0.5, 1, 2, 5, 10, 25, 50]
)

fraud_amount_blocked = Counter(
    "fraud_amount_blocked_dollars_total",
    "Cumulative USD amount blocked as fraud"
)

flink_velocity_gauge = Gauge(
    "flink_user_velocity_score",
    "Last Flink velocity score seen by the API"
)

# ──────────────────────────────────────────────
# App
# ──────────────────────────────────────────────
app = FastAPI(
    title="FraudShield API",
    description="Real-time fraud detection — LightGBM + Flink + Prometheus",
    version="2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Instrument ALL endpoints automatically → exposes GET /metrics for Prometheus
Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    should_respect_env_var=False,
    should_instrument_requests_inprogress=True,
    excluded_handlers=[],
    body_handlers=[],
    inprogress_name="http_requests_inprogress",
    inprogress_labels=True,
).instrument(app).expose(app)   # GET /metrics


# ── Redis ──
redis_client = None
try:
    _redis_host = os.environ.get("REDIS_HOST", "localhost")
    _redis_port = int(os.environ.get("REDIS_PORT", 6379))
    redis_client = redis.Redis(host=_redis_host, port=_redis_port, db=0,
                               socket_connect_timeout=2)
    redis_client.ping()
    print("[API] Redis connected ✓")
except Exception as e:
    print(f"[API] Redis unavailable: {e}")

# ── Model ──
model_data = None
try:
    with open('model.pkl', 'rb') as f:
        model_data = pickle.load(f)
    print("[API] Model loaded ✓")
except FileNotFoundError:
    print("[API] Warning: model.pkl not found. Run ecommerce_fraud_model.py first.")


# ──────────────────────────────────────────────
# Request Schema
# ──────────────────────────────────────────────
class Transaction(BaseModel):
    user_id: int
    amount: float
    total_transactions_user: int
    avg_amount_user: float
    account_age_days: int
    shipping_distance_km: float
    country: str
    bin_country: str
    channel: str
    merchant_category: str
    promo_used: int
    avs_match: int
    cvv_result: int
    three_ds_flag: int
    transaction_time: str


# ──────────────────────────────────────────────
# POST /predict
# ──────────────────────────────────────────────
@app.post("/predict")
def predict_fraud(txn: Transaction):
    overall_start = time_module.perf_counter()

    if not model_data:
        return {"error": "Model not loaded. Run ecommerce_fraud_model.py first."}

    # ── Redis lookup (timed separately) ──
    redis_start = time_module.perf_counter()
    flink_state = get_user_flink_state(txn.user_id)
    redis_ms = (time_module.perf_counter() - redis_start) * 1000
    redis_latency_ms.observe(redis_ms)

    features = model_data['features']
    cat_cols  = model_data['cat_cols']
    num_cols  = model_data['num_cols']
    encoder   = model_data['encoder']

    input_dict = dict(txn)

    # Time features (same as training)
    parsed_time = pd.to_datetime(input_dict['transaction_time'], utc=True)
    input_dict['txn_hour']      = parsed_time.hour
    input_dict['txn_day']       = parsed_time.day
    input_dict['txn_month']     = parsed_time.month
    input_dict['txn_dayofweek'] = parsed_time.dayofweek
    input_dict['is_weekend']    = int(parsed_time.dayofweek >= 5)
    input_dict['is_night']      = int(parsed_time.hour < 6 or parsed_time.hour >= 22)
    input_dict['amount_vs_avg'] = txn.amount / (txn.avg_amount_user + 1)
    input_dict['geo_mismatch']  = int(txn.country != txn.bin_country)

    input_df = pd.DataFrame([input_dict])
    input_df = input_df[features].copy()
    input_df[num_cols] = input_df[num_cols].fillna(0)
    input_df[cat_cols] = input_df[cat_cols].fillna("Missing")
    input_df[cat_cols] = encoder.transform(input_df[cat_cols])

    # Inject Flink real-time velocity
    if flink_state["velocity_score"] > 0:
        input_df['total_transactions_user'] = flink_state["txn_count_24h"]

    # ── Score ──
    prediction  = int(model_data['model'].predict(input_df)[0])
    probability = float(model_data['model'].predict_proba(input_df)[0][1])

    # ── Prometheus counters ──
    verdict = "fraud" if prediction == 1 else "legit"
    fraud_predictions_total.labels(verdict=verdict).inc()
    if prediction == 1:
        fraud_amount_blocked.inc(txn.amount)
    flink_velocity_gauge.set(flink_state["velocity_score"])

    # ── SHAP explanation ──
    explanation = {}
    try:
        explainer = model_data.get('explainer')
        if explainer:
            sv = explainer.shap_values(input_df)
            if isinstance(sv, list) and len(sv) == 2:
                sv = sv[1]
            elif isinstance(sv, list):
                sv = sv[0]
            sv_row = sv[0] if sv.ndim == 2 else sv
            pairs = sorted(zip(features, sv_row), key=lambda x: abs(x[1]), reverse=True)[:5]
            explanation = {f: round(float(v), 5) for f, v in pairs}
    except Exception:
        pass

    # Update legacy Redis velocity counter
    if redis_client:
        try:
            redis_client.incr(f"user_velocity_{txn.user_id}")
            redis_client.expire(f"user_velocity_{txn.user_id}", 3600)
        except Exception:
            pass

    # ── Record total prediction latency ──
    total_ms = (time_module.perf_counter() - overall_start) * 1000
    prediction_latency_ms.observe(total_ms)

    return {
        "is_fraud": prediction,
        "fraud_probability": round(probability, 4),
        "risk_level": "HIGH" if probability > 0.7 else ("MEDIUM" if probability > 0.4 else "LOW"),
        "flink_velocity_score": flink_state["velocity_score"],
        "flink_txn_1h": flink_state["txn_count_1h"],
        "flink_txn_24h": flink_state["txn_count_24h"],
        "prediction_latency_ms": round(total_ms, 3),
        "redis_latency_ms": round(redis_ms, 3),
        "message": "⚠️ FRAUD DETECTED" if prediction == 1 else "✅ Transaction Approved",
        "explanation": explanation,
    }


# ──────────────────────────────────────────────
# GET /health
# ──────────────────────────────────────────────
@app.get("/health")
def health_check():
    redis_ok = False
    flink_status = "UNKNOWN"
    flink_processed = 0
    try:
        if redis_client:
            redis_client.ping()
            redis_ok = True
            flink_status = (redis_client.get("flink:job:status") or b"NOT_STARTED").decode()
            flink_processed = int(redis_client.get("flink:job:processed") or 0)
    except Exception:
        pass
    return {
        "api": "OK",
        "model": "loaded" if model_data else "not_loaded",
        "redis": "connected" if redis_ok else "disconnected",
        "flink_job_status": flink_status,
        "flink_events_processed": flink_processed,
    }


# ──────────────────────────────────────────────
# GET /flink endpoints
# ──────────────────────────────────────────────
@app.get("/flink/stream")
def flink_stream(n: int = 20):
    return {"events": get_stream_log(n)}


@app.get("/flink/user/{user_id}")
def flink_user_state(user_id: int):
    return get_user_flink_state(user_id)


# ──────────────────────────────────────────────
# GET /model-metrics  (renamed from /metrics to avoid clash with Prometheus)
# ──────────────────────────────────────────────
@app.get("/model-metrics")
def get_model_metrics():
    try:
        with open('model_metrics.json') as f:
            return json.load(f)
    except FileNotFoundError:
        return {"error": "model_metrics.json not found."}


@app.get("/shap")
def get_shap():
    try:
        with open('shap_importance.json') as f:
            return json.load(f)
    except FileNotFoundError:
        return {"error": "shap_importance.json not found."}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=False)
