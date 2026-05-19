import redis
import time
import pandas as pd

def simulate_flink_job():
    print("Starting simulated Stateful Flink Stream Processing...")
    try:
        r = redis.Redis(host='localhost', port=6379, db=0)
        r.ping()
        print("Connected to Redis Feature Store successfully.")
    except Exception as e:
        print("Redis is not running. Using fallback in FastAPI.")
        return

    # Simulate processing real transactions chronologically
    print("Loading data stream...")
    df = pd.read_csv('transactions.csv')
    df['transaction_time'] = pd.to_datetime(df['transaction_time'])
    df = df.sort_values(by='transaction_time').reset_index(drop=True)
    
    # We will pick the top active user that ends up doing fraud to demonstrate stream aggregations
    fraud_users = df[df['is_fraud'] == 1]['user_id'].unique()
    
    print("Simulating event windows over time...")
    for user in fraud_users[:5]:
        user_stream = df[df['user_id'] == user]
        
        running_count = 0
        for idx, row in user_stream.iterrows():
            running_count += 1
            # Flink computes dynamic "velocity" state: e.g. how many txns have occurred recently.
            # In a real Flink job, this uses a SlidingEventTimeWindows
            velocity = running_count * 1.5 
            
            r.set(f"user_velocity_{user}", velocity)
            print(f"[{row['transaction_time']}] Processed user_{user} txn. Velocity updated to {velocity}")
            time.sleep(1)

if __name__ == "__main__":
    simulate_flink_job()
