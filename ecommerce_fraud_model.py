"""
ecommerce_fraud_model.py
=========================
Train a LightGBM fraud detection model with:
  - Strict TIME-BASED train/test split (no data leakage)
  - SHAP explainability (like production systems at Stripe/PayPal use)
  - Class imbalance handling via scale_pos_weight
  - Feature engineering (time + velocity features)

Why time-based split?
  Using random split would let the model "peek" into the future —
  transaction patterns from December helping predict January fraud.
  In real fraud detection, you ONLY train on past data and test on future data.
"""

import pandas as pd
import lightgbm as lgb
import shap
import pickle
import numpy as np
from sklearn.metrics import (
    classification_report, roc_auc_score,
    precision_recall_curve, confusion_matrix
)
from sklearn.preprocessing import OrdinalEncoder

# ──────────────────────────────────────────────
# Feature Engineering
# ──────────────────────────────────────────────
def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Extract time-based and behavioral features.
    These are the same features PayPal/Stripe use in their rule engines.
    """
    df = df.copy()

    # Time features
    df['txn_hour']       = df['transaction_time'].dt.hour
    df['txn_day']        = df['transaction_time'].dt.day
    df['txn_month']      = df['transaction_time'].dt.month
    df['txn_dayofweek']  = df['transaction_time'].dt.dayofweek
    df['is_weekend']     = (df['txn_dayofweek'] >= 5).astype(int)
    df['is_night']       = ((df['txn_hour'] < 6) | (df['txn_hour'] >= 22)).astype(int)

    # Risk ratio: current amount vs user's average (large spike = suspicious)
    df['amount_vs_avg']  = df['amount'] / (df['avg_amount_user'] + 1)

    # Geographic mismatch (card issued in country X, used in country Y)
    df['geo_mismatch']   = (df['country'] != df['bin_country']).astype(int)

    return df


def train_full_model():
    print("=" * 55)
    print("  E-Commerce Fraud Detection — Model Training")
    print("=" * 55)

    # ── Load & sort chronologically ──
    print("\n[1/6] Loading dataset...")
    df = pd.read_csv('transactions.csv')
    df['transaction_time'] = pd.to_datetime(df['transaction_time'], utc=True)
    df = df.sort_values('transaction_time').reset_index(drop=True)
    print(f"      Total records: {len(df):,}")
    print(f"      Date range: {df['transaction_time'].min().date()} → {df['transaction_time'].max().date()}")
    print(f"      Fraud rate:  {df['is_fraud'].mean()*100:.2f}%")

    # ── Feature engineering ──
    print("\n[2/6] Engineering features...")
    df = engineer_features(df)

    # ── TIME-BASED SPLIT (80% train / 20% test) ──
    print("\n[3/6] Performing strict TIME-BASED split (no data leakage)...")
    split_date = df['transaction_time'].quantile(0.80)
    train_mask = df['transaction_time'] < split_date
    test_mask  = df['transaction_time'] >= split_date

    print(f"      Train: {train_mask.sum():,} records  |  up to {split_date.date()}")
    print(f"      Test:  {test_mask.sum():,}  records  |  from {split_date.date()}")
    print(f"      Train fraud rate: {df[train_mask]['is_fraud'].mean()*100:.2f}%")
    print(f"      Test  fraud rate: {df[test_mask]['is_fraud'].mean()*100:.2f}%")

    # Save the split date so the UI can show the timeline
    with open('train_test_split_date.txt', 'w') as f:
        f.write(str(split_date.date()))

    # ── Prepare X, y ──
    drop_cols = ['transaction_id', 'user_id', 'transaction_time', 'is_fraud']
    cat_cols  = ['country', 'bin_country', 'channel', 'merchant_category']
    features  = [c for c in df.columns if c not in drop_cols]
    num_cols  = [c for c in features if c not in cat_cols]

    X = df[features].copy()
    y = df['is_fraud']

    # Fill missing
    X[num_cols] = X[num_cols].fillna(0)
    X[cat_cols] = X[cat_cols].fillna("Missing")

    # Encode categoricals
    encoder = OrdinalEncoder(handle_unknown='use_encoded_value', unknown_value=-1)
    X[cat_cols] = encoder.fit_transform(X[cat_cols])

    X_train, y_train = X[train_mask], y[train_mask]
    X_test,  y_test  = X[test_mask],  y[test_mask]

    # ── Class imbalance: fraud is rare, so we upweight fraud samples ──
    neg = (y_train == 0).sum()
    pos = (y_train == 1).sum()
    scale_pos = round(neg / pos, 2)
    print(f"\n[4/6] Class imbalance — scale_pos_weight set to {scale_pos}")

    # ── Train LightGBM ──
    print("\n[5/6] Training LightGBM model...")
    model = lgb.LGBMClassifier(
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=63,
        max_depth=8,
        scale_pos_weight=scale_pos,   # handles class imbalance
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        n_jobs=-1,
        verbose=-1
    )
    model.fit(X_train, y_train)

    # ── Evaluate ──
    print("\n[6/6] Evaluating on FUTURE (test) data...")
    y_pred     = model.predict(X_test)
    y_prob     = model.predict_proba(X_test)[:, 1]
    auc_score  = roc_auc_score(y_test, y_prob)

    print(f"\n      ROC-AUC Score: {auc_score:.4f}  (industry target: >0.85)")
    print("\n      Classification Report:")
    print(classification_report(y_test, y_pred, target_names=["Legit", "Fraud"]))

    # Confusion matrix
    cm = confusion_matrix(y_test, y_pred)
    print(f"      Confusion Matrix: TN={cm[0,0]} | FP={cm[0,1]} | FN={cm[1,0]} | TP={cm[1,1]}")

    # Save evaluation metrics for UI display
    metrics = {
        "auc": round(auc_score, 4),
        "tn": int(cm[0,0]), "fp": int(cm[0,1]),
        "fn": int(cm[1,0]), "tp": int(cm[1,1]),
        "precision_fraud": round(float(classification_report(y_test, y_pred, output_dict=True)['1']['precision']), 4),
        "recall_fraud":    round(float(classification_report(y_test, y_pred, output_dict=True)['1']['recall']), 4),
        "f1_fraud":        round(float(classification_report(y_test, y_pred, output_dict=True)['1']['f1-score']), 4),
        "split_date": str(split_date.date()),
        "train_size": int(train_mask.sum()),
        "test_size": int(test_mask.sum()),
    }
    import json
    with open('model_metrics.json', 'w') as f:
        json.dump(metrics, f, indent=2)

    # SHAP explainability
    print("\n  Computing SHAP values for explainability (sample of 500)...")
    explainer   = shap.TreeExplainer(model)
    sample_idx  = np.random.choice(len(X_test), min(500, len(X_test)), replace=False)
    shap_values = explainer.shap_values(X_test.iloc[sample_idx])
    # For binary classification, lightgbm returns list; take class=1
    if isinstance(shap_values, list):
        shap_values = shap_values[1]
    mean_shap = pd.Series(
        np.abs(shap_values).mean(axis=0),
        index=X_test.columns
    ).sort_values(ascending=False)
    top_features = mean_shap.head(10).to_dict()

    with open('shap_importance.json', 'w') as f:
        json.dump({k: round(float(v), 5) for k, v in top_features.items()}, f, indent=2)
    print(f"  Top SHAP feature: '{mean_shap.idxmax()}'  (importance={mean_shap.max():.4f})")

    # ── Save model bundle ──
    with open('model.pkl', 'wb') as f:
        pickle.dump({
            'model':    model,
            'features': features,
            'cat_cols': cat_cols,
            'num_cols': num_cols,
            'encoder':  encoder,
            'explainer': explainer,
        }, f)

    print("\n  Model and SHAP explainer saved to model.pkl ✓")
    print("  Metrics saved to model_metrics.json ✓")
    print("  SHAP importances saved to shap_importance.json ✓\n")


if __name__ == '__main__':
    train_full_model()
