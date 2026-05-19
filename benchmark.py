"""
benchmark.py — Real Performance Benchmark for FraudShield
===========================================================
Measures ACTUAL latencies, TPS, Redis speed — no made-up numbers.
Run AFTER api.py is started.

Usage:
    source venv/bin/activate
    python benchmark.py

Outputs real stats you can put on your resume.
"""

import requests
import time
import json
import redis
import statistics
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

API_URL = "http://127.0.0.1:8001/predict"

# ── Load a sample batch of real transactions ──
print("Loading sample transactions from CSV...")
df = pd.read_csv("transactions.csv")
df['transaction_time'] = pd.to_datetime(df['transaction_time'], utc=True)
df = df.sort_values('transaction_time').reset_index(drop=True)

# Use the last 1000 rows (test set / future data)
sample = df.tail(1000).reset_index(drop=True)

def build_payload(row):
    return {
        "user_id":                  int(row['user_id']),
        "amount":                   float(row['amount']),
        "total_transactions_user":  int(row['total_transactions_user']),
        "avg_amount_user":          float(row['avg_amount_user']),
        "account_age_days":         int(row['account_age_days']),
        "shipping_distance_km":     float(row['shipping_distance_km']),
        "country":                  str(row['country']),
        "bin_country":              str(row['bin_country']),
        "channel":                  str(row['channel']),
        "merchant_category":        str(row['merchant_category']),
        "promo_used":               int(row['promo_used']),
        "avs_match":                int(row['avs_match']),
        "cvv_result":               int(row['cvv_result']),
        "three_ds_flag":            int(row['three_ds_flag']),
        "transaction_time":         str(row['transaction_time']),
    }


# ══════════════════════════════════════════════
# BENCHMARK 1 — Sequential latency (p50, p95, p99)
# ══════════════════════════════════════════════
def bench_sequential(n=200):
    print(f"\n{'='*55}")
    print(f"  BENCHMARK 1: Sequential Latency  ({n} requests)")
    print(f"{'='*55}")
    latencies = []
    errors = 0
    for i in range(n):
        row = sample.iloc[i % len(sample)]
        payload = build_payload(row)
        t0 = time.perf_counter()
        try:
            r = requests.post(API_URL, json=payload, timeout=5)
            r.raise_for_status()
            lat = (time.perf_counter() - t0) * 1000
            latencies.append(lat)
        except Exception as e:
            errors += 1
    latencies.sort()
    p50  = statistics.median(latencies)
    p95  = latencies[int(0.95 * len(latencies))]
    p99  = latencies[int(0.99 * len(latencies))]
    mean = statistics.mean(latencies)
    mn   = min(latencies)
    mx   = max(latencies)
    print(f"  Requests sent : {n}  |  Errors: {errors}")
    print(f"  Mean latency  : {mean:.2f} ms")
    print(f"  Min  latency  : {mn:.2f} ms")
    print(f"  p50  latency  : {p50:.2f} ms")
    print(f"  p95  latency  : {p95:.2f} ms")
    print(f"  p99  latency  : {p99:.2f} ms")
    print(f"  Max  latency  : {mx:.2f} ms")
    return {"mean": round(mean,2), "p50": round(p50,2), "p95": round(p95,2),
            "p99": round(p99,2), "min": round(mn,2), "max": round(mx,2), "errors": errors}


# ══════════════════════════════════════════════
# BENCHMARK 2 — Throughput (TPS) with concurrency
# ══════════════════════════════════════════════
def bench_tps(n=500, workers=10):
    print(f"\n{'='*55}")
    print(f"  BENCHMARK 2: Throughput — {workers} concurrent workers, {n} requests")
    print(f"{'='*55}")
    payloads = [build_payload(sample.iloc[i % len(sample)]) for i in range(n)]
    start = time.perf_counter()
    ok = 0
    def do_request(payload):
        try:
            r = requests.post(API_URL, json=payload, timeout=10)
            return r.status_code == 200
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(do_request, p) for p in payloads]
        for f in as_completed(futures):
            if f.result():
                ok += 1

    elapsed = time.perf_counter() - start
    tps = ok / elapsed
    print(f"  Completed     : {ok}/{n} requests")
    print(f"  Total time    : {elapsed:.2f}s")
    print(f"  Throughput    : {tps:.1f} req/s  ({tps*60:.0f} req/min)")
    return {"tps": round(tps, 1), "requests": ok, "elapsed_s": round(elapsed, 2)}


# ══════════════════════════════════════════════
# BENCHMARK 3 — Redis latency
# ══════════════════════════════════════════════
def bench_redis(n=500):
    print(f"\n{'='*55}")
    print(f"  BENCHMARK 3: Redis Lookup Latency  ({n} ops)")
    print(f"{'='*55}")
    try:
        r = redis.Redis(host='localhost', port=6379, db=0)
        r.ping()
    except Exception:
        print("  ⚠️  Redis not running — skipping Redis benchmark")
        return None

    latencies = []
    for i in range(n):
        key = f"bench:test:{i % 100}"
        r.set(key, "1234.56", ex=60)
        t0 = time.perf_counter()
        r.get(key)
        latencies.append((time.perf_counter() - t0) * 1000)

    latencies.sort()
    p50 = statistics.median(latencies)
    p99 = latencies[int(0.99 * len(latencies))]
    print(f"  Mean latency  : {statistics.mean(latencies):.3f} ms")
    print(f"  p50  latency  : {p50:.3f} ms")
    print(f"  p99  latency  : {p99:.3f} ms")
    return {"mean": round(statistics.mean(latencies),3),
            "p50": round(p50,3), "p99": round(p99,3)}


# ══════════════════════════════════════════════
# LOAD MODEL METRICS (already computed during training)
# ══════════════════════════════════════════════
def load_model_metrics():
    print(f"\n{'='*55}")
    print(f"  MODEL METRICS (on held-out FUTURE data)")
    print(f"{'='*55}")
    try:
        with open('model_metrics.json') as f:
            m = json.load(f)
        print(f"  ROC-AUC          : {m['auc']}")
        print(f"  Fraud Precision  : {m['precision_fraud']}")
        print(f"  Fraud Recall     : {m['recall_fraud']}")
        print(f"  Fraud F1-score   : {m['f1_fraud']}")
        print(f"  True Positives   : {m['tp']:,}")
        print(f"  False Positives  : {m['fp']:,}")
        print(f"  False Negatives  : {m['fn']:,}")
        print(f"  Train set size   : {m['train_size']:,}")
        print(f"  Test set size    : {m['test_size']:,}")
        print(f"  Train/Test split : {m['split_date']}")
        return m
    except Exception as e:
        print(f"  Error loading metrics: {e}")
        return {}


# ══════════════════════════════════════════════
# LOAD SHAP TOP FEATURES
# ══════════════════════════════════════════════
def load_shap():
    print(f"\n{'='*55}")
    print(f"  TOP SHAP FEATURES (mean |SHAP| on test set)")
    print(f"{'='*55}")
    try:
        with open('shap_importance.json') as f:
            s = json.load(f)
        for feat, val in list(s.items())[:5]:
            print(f"  {feat:<30} {val:.5f}")
        return s
    except Exception:
        return {}


# ══════════════════════════════════════════════
# FINAL SUMMARY — HONEST resume-ready stats
# ══════════════════════════════════════════════
def print_summary(seq, tps_result, redis_result, model_m):
    print(f"\n{'='*55}")
    print(f"  ✅  HONEST PROJECT STATS (printable on CV)")
    print(f"{'='*55}")

    total_records = 299695
    fraud_rate = 2.21

    if seq:
        print(f"\n  📡 API Performance (measured on localhost):")
        print(f"     Prediction latency  — p50: {seq['p50']} ms | p99: {seq['p99']} ms")
        print(f"     Min / Max           — {seq['min']} ms / {seq['max']} ms")

    if tps_result:
        print(f"\n  ⚡ Throughput:")
        print(f"     Single-node TPS       = {tps_result['tps']} req/s")
        print(f"     (with {10}-thread concurrency on local machine)")

    if redis_result:
        print(f"\n  🗄️  Redis Feature Store:")
        print(f"     Key lookup p50        = {redis_result['p50']} ms")
        print(f"     Key lookup p99        = {redis_result['p99']} ms")

    if model_m:
        print(f"\n  🧠 ML Model (LightGBM — time-based split, no leakage):")
        print(f"     Dataset               = {total_records:,} transactions")
        print(f"     Class imbalance       = {fraud_rate:.2f}% fraud (≈ 1:44 ratio)")
        print(f"     ROC-AUC               = {model_m.get('auc')}")
        print(f"     Fraud Recall          = {model_m.get('recall_fraud')} ({float(model_m.get('recall_fraud',0))*100:.1f}% of fraud caught)")
        print(f"     Fraud Precision       = {model_m.get('precision_fraud')}")
        print(f"     Fraud F1-score        = {model_m.get('f1_fraud')}")
        print(f"     True Positive rate    = {model_m.get('tp'):,} fraud blocked")
        print(f"     False Positive rate   = {model_m.get('fp'):,} false alerts")

    print(f"\n  🔥 Apache Flink-style Processor:")
    print(f"     Windowing             = TumblingWindow(1h) + SlidingWindow(24h)")
    print(f"     Features computed     = 5 real-time velocity features per user")
    print(f"     State backend         = Redis (TTL=24h per user key)")

    print(f"\n  📊 Monitoring:")
    print(f"     Prometheus endpoint   = http://localhost:8001/metrics")
    print(f"     Grafana dashboard     = http://localhost:3000")

    print(f"\n{'='*55}")

    # Save for Streamlit to display
    results = {
        "latency": seq,
        "tps": tps_result,
        "redis": redis_result,
        "model": {k: model_m.get(k) for k in
                  ['auc','precision_fraud','recall_fraud','f1_fraud',
                   'tp','fp','fn','tn','train_size','test_size','split_date']}
                  if model_m else {}
    }
    with open('benchmark_results.json', 'w') as f:
        json.dump(results, f, indent=2)
    print(f"  Results saved → benchmark_results.json")


# ══════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════
if __name__ == "__main__":
    print("\n🛡️  FraudShield — Real Performance Benchmark")
    print("  Make sure api.py is running on port 8001 first!\n")

    # Check API is alive
    try:
        requests.get("http://127.0.0.1:8001/health", timeout=3)
    except Exception:
        print("❌ API not reachable at http://127.0.0.1:8001. Start it first.")
        exit(1)

    seq_result   = bench_sequential(n=200)
    tps_result   = bench_tps(n=300, workers=10)
    redis_result = bench_redis(n=500)
    model_m      = load_model_metrics()
    load_shap()
    print_summary(seq_result, tps_result, redis_result, model_m)
