# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — Builder: install all Python deps in a separate layer
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# System deps needed to compile some packages (e.g., lightgbm, shap)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first (layer cache busting on code changes only)
COPY requirements.txt .

# Install into a prefix we can copy to the final image
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — Runtime: lean final image
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# libgomp1 is required at runtime by LightGBM
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source code
COPY api.py .
COPY app.py .
COPY flink_processor.py .
COPY flink_job_simulator.py .
COPY ecommerce_fraud_model.py .
COPY model.pkl .
COPY model_metrics.json .
COPY shap_importance.json .
COPY train_test_split_date.txt .
COPY transactions.csv .

# ─── Environment variables ───────────────────────────────────────────────────
# PORT is set by Render at runtime; default to 8001 for local runs
ENV PORT=8001
# Redis host — override with REDIS_HOST env var on Render
ENV REDIS_HOST=localhost
ENV REDIS_PORT=6379
# Disable Python buffering for clean Docker logs
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Expose the port
EXPOSE ${PORT}

# ─── Startup ─────────────────────────────────────────────────────────────────
# Use shell form so $PORT expands correctly
CMD uvicorn api:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 30
