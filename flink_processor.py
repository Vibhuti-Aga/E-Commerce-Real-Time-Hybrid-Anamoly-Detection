"""
flink_processor.py
==================
Real Apache Flink-style stateful stream processing in Python.

Architecture mirrors core Flink concepts:
  - KeyedStream     → transactions grouped by user_id
  - TumblingWindow  → count events in non-overlapping time buckets (e.g. 1-hour window)
  - SlidingWindow   → rolling aggregations over last N transactions
  - State Backend   → Redis acts as the distributed state store (like Flink's RocksDB backend)
  - Source          → CSV file read chronologically (like Kafka source in production)
  - Sink            → Redis keys written per user (like Kafka/JDBC sink in production)

In a production Flink deployment (Java/JVM), each operator would be a separate
parallel task running in a JobManager/TaskManager cluster.
Here we use Python threads to simulate that parallelism.
"""

import os
import redis
import time
import json
import threading
import pandas as pd
from datetime import timedelta
from collections import defaultdict, deque

# ──────────────────────────────────────────────
# Flink Source Operator  (reads ordered event stream)
# ──────────────────────────────────────────────
class TransactionSource:
    """
    Mimics a Flink DataStream Source (e.g., FlinkKafkaConsumer or FileSource).
    Emits transactions in chronological order with configurable replay speed.
    """
    def __init__(self, csv_path: str, speedup: float = 50.0):
        df = pd.read_csv(csv_path)
        df['transaction_time'] = pd.to_datetime(df['transaction_time'], utc=True)
        df = df.sort_values('transaction_time').reset_index(drop=True)
        self.df = df
        self.speedup = speedup   # replay at 50x real speed

    def emit(self):
        """Generator that yields (event_time, row) pairs – the Flink event stream."""
        prev_time = None
        for _, row in self.df.iterrows():
            ts = row['transaction_time']
            if prev_time is not None:
                gap = (ts - prev_time).total_seconds() / self.speedup
                gap = min(gap, 0.05)   # cap at 50ms so demo doesn't stall
                time.sleep(gap)
            prev_time = ts
            yield ts, row


# ──────────────────────────────────────────────
# Flink KeyedProcessFunction  (per-user stateful logic)
# ──────────────────────────────────────────────
class UserFraudStateFunction:
    """
    Equivalent to Flink's KeyedProcessFunction<Long, Transaction, Result>.

    Maintains these state variables per user:
      • txn_count_1h  → TumblingEventTimeWindow(1 hour) count
      • txn_count_24h → SlidingEventTimeWindow(24 hours, 1 hour) count  
      • amount_sum_1h → sum of amounts in the last sliding 1-hour window
      • recent_amounts → circular buffer (ValueState / ListState equivalent)
      • velocity_score → computed risk signal written to Redis
    """

    def __init__(self):
        # Per-user state: {user_id: deque of (timestamp, amount)}
        self._event_buffer: dict = defaultdict(lambda: deque(maxlen=500))

    def process(self, user_id: int, event_time: pd.Timestamp, amount: float) -> dict:
        buf = self._event_buffer[user_id]
        buf.append((event_time, amount))

        # ── Tumbling 1-hour window: count in current hour bucket
        hour_start = event_time.floor('h')
        txn_count_1h = sum(1 for ts, _ in buf
                           if ts >= hour_start and ts <= event_time)

        # ── Sliding 24-hour window: total count + amount sum
        cutoff_24h = event_time - timedelta(hours=24)
        recent_24h = [(ts, amt) for ts, amt in buf if ts >= cutoff_24h]
        txn_count_24h = len(recent_24h)
        amount_sum_24h = sum(amt for _, amt in recent_24h)
        avg_amount_24h = amount_sum_24h / txn_count_24h if txn_count_24h else 0

        # ── Sliding 1-hour window: amount sum
        cutoff_1h = event_time - timedelta(hours=1)
        recent_1h = [(ts, amt) for ts, amt in buf if ts >= cutoff_1h]
        amount_sum_1h = sum(amt for _, amt in recent_1h)
        txn_count_1h_sliding = len(recent_1h)

        # ── Velocity score (risk signal): high velocity + high amount = higher risk
        # This is what gets written to the Redis "state backend"
        velocity_score = round(
            (txn_count_1h_sliding * 0.4) +
            (amount_sum_1h / 100.0 * 0.3) +
            (txn_count_24h * 0.3),
            4
        )

        return {
            "user_id": user_id,
            "event_time": str(event_time),
            "txn_count_1h": txn_count_1h_sliding,
            "txn_count_24h": txn_count_24h,
            "amount_sum_1h": round(amount_sum_1h, 2),
            "avg_amount_24h": round(avg_amount_24h, 2),
            "velocity_score": velocity_score,
        }


# ──────────────────────────────────────────────
# Flink Sink Operator  (writes to Redis state store)
# ──────────────────────────────────────────────
class RedisSink:
    """
    Equivalent to a Flink Sink that writes enriched state to Redis.
    In production this would be a JdbcSink or KafkaSink.
    """

    def __init__(self):
        self.r = None
        try:
            _host = os.environ.get("REDIS_HOST", "localhost")
            _port = int(os.environ.get("REDIS_PORT", 6379))
            self.r = redis.Redis(host=_host, port=_port, db=0,
                                 socket_connect_timeout=2)
            self.r.ping()
            print("[Flink Sink] Connected to Redis state backend ✓")
        except Exception as e:
            print(f"[Flink Sink] Redis unavailable: {e}. State will be lost.")

    def write(self, state: dict):
        """Write user state to Redis with 24h TTL."""
        if not self.r:
            return
        uid = state["user_id"]
        try:
            self.r.setex(f"flink:user:{uid}:velocity",   86400, state["velocity_score"])
            self.r.setex(f"flink:user:{uid}:txn_1h",     86400, state["txn_count_1h"])
            self.r.setex(f"flink:user:{uid}:txn_24h",    86400, state["txn_count_24h"])
            self.r.setex(f"flink:user:{uid}:amt_1h",     86400, state["amount_sum_1h"])
            self.r.setex(f"flink:user:{uid}:avg_24h",    86400, state["avg_amount_24h"])
            # Also push to a global stream log (last 200 events)
            self.r.lpush("flink:stream:log", json.dumps(state))
            self.r.ltrim("flink:stream:log", 0, 199)
        except Exception:
            pass


# ──────────────────────────────────────────────
# Flink Job Runner  (the equivalent of env.execute())
# ──────────────────────────────────────────────
class FlinkJob:
    """
    Orchestrates Source → KeyedStream → ProcessFunction → Sink.
    Runs in a background daemon thread so the UI stays responsive.
    
    Flink equivalent:
        env.addSource(TransactionSource)
           .keyBy(t -> t.user_id)
           .process(UserFraudStateFunction)
           .addSink(RedisSink)
           .execute("EcommerceFlinkJob")
    """

    def __init__(self, csv_path: str = "transactions.csv", speedup: float = 50.0):
        self.csv_path = csv_path
        self.speedup = speedup
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.processed_count = 0
        self.is_running = False

        # Operators
        self.source = TransactionSource(csv_path, speedup)
        self.process_fn = UserFraudStateFunction()
        self.sink = RedisSink()

        # Write job status to Redis so the UI can read it
        self._status_sink = self.sink  # reuse same redis connection

    def _run(self):
        """Internal run loop — this is the TaskManager thread."""
        self.is_running = True
        try:
            self.sink.r and self.sink.r.set("flink:job:status", "RUNNING")
        except Exception:
            pass

        print("[FlinkJob] Job RUNNING — processing event stream...")

        for event_time, row in self.source.emit():
            if self._stop_event.is_set():
                break

            user_id = int(row['user_id'])
            amount  = float(row['amount'])

            # KeyedProcessFunction call
            state = self.process_fn.process(user_id, event_time, amount)

            # Sink write
            self.sink.write(state)

            self.processed_count += 1

            # Update job-level metrics in Redis every 50 events
            if self.processed_count % 50 == 0:
                try:
                    self.sink.r and self.sink.r.set("flink:job:processed", self.processed_count)
                    print(f"[FlinkJob] Processed {self.processed_count} events | "
                          f"Last user: {user_id} | velocity: {state['velocity_score']}")
                except Exception:
                    pass

        self.is_running = False
        print(f"[FlinkJob] Job FINISHED — total events: {self.processed_count}")
        try:
            self.sink.r and self.sink.r.set("flink:job:status", "FINISHED")
        except Exception:
            pass

    def start(self):
        """Start the Flink job in a background thread."""
        if self._thread and self._thread.is_alive():
            print("[FlinkJob] Already running.")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="FlinkJobThread")
        self._thread.start()
        print("[FlinkJob] Background thread started.")

    def stop(self):
        self._stop_event.set()
        print("[FlinkJob] Stop signal sent.")


# ──────────────────────────────────────────────
# Convenience: read enriched state from Redis for a user
# ──────────────────────────────────────────────
def get_user_flink_state(user_id: int) -> dict:
    """
    Called by FastAPI at prediction time to retrieve the real-time
    windowed features computed by the Flink job.
    """
    try:
        _host = os.environ.get("REDIS_HOST", "localhost")
        _port = int(os.environ.get("REDIS_PORT", 6379))
        r = redis.Redis(host=_host, port=_port, db=0, socket_connect_timeout=1)
        uid = user_id
        return {
            "velocity_score": float(r.get(f"flink:user:{uid}:velocity") or 0),
            "txn_count_1h":   int(r.get(f"flink:user:{uid}:txn_1h")    or 0),
            "txn_count_24h":  int(r.get(f"flink:user:{uid}:txn_24h")   or 0),
            "amount_sum_1h":  float(r.get(f"flink:user:{uid}:amt_1h")  or 0),
            "avg_amount_24h": float(r.get(f"flink:user:{uid}:avg_24h") or 0),
        }
    except Exception:
        return {
            "velocity_score": 0, "txn_count_1h": 0, "txn_count_24h": 0,
            "amount_sum_1h": 0,  "avg_amount_24h": 0
        }


def get_stream_log(n: int = 20) -> list[dict]:
    """Fetch latest N events from Flink's stream log in Redis."""
    try:
        _host = os.environ.get("REDIS_HOST", "localhost")
        _port = int(os.environ.get("REDIS_PORT", 6379))
        r = redis.Redis(host=_host, port=_port, db=0, socket_connect_timeout=1)
        raw = r.lrange("flink:stream:log", 0, n - 1)
        return [json.loads(x) for x in raw]
    except Exception:
        return []


if __name__ == "__main__":
    print("=" * 60)
    print("  Apache Flink-style Stateful Stream Processing Job")
    print("  Concepts: KeyedStream | TumblingWindow | SlidingWindow")
    print("  State Backend: Redis (like Flink's RocksDB)")
    print("=" * 60)
    job = FlinkJob(speedup=100.0)
    job.start()
    # Keep main thread alive
    try:
        while job.is_running:
            time.sleep(5)
    except KeyboardInterrupt:
        job.stop()
