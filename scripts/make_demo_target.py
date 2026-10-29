"""Create a pretend company database ("shop_demo") to connect PipelineDoctor to, plus a read-only user for it.

Run from the project root:
    python -m scripts.make_demo_target                       # 30 normal days
    python -m scripts.make_demo_target --anomaly amount_x100 # yesterday's amounts are 100x too big
Other anomalies: row_drop, null_spike, late_partial, duplicates.

Connect with:  host localhost (or "postgres" from inside Docker), port 5432, database shop_demo,
               user demo_reader, password demo_reader_pw   (this user can only SELECT)
"""

import argparse
import random
from datetime import datetime, timedelta

import psycopg2
from psycopg2.extras import execute_values

from app import config

DB, READER, READER_PASSWORD = "shop_demo", "demo_reader", "demo_reader_pw"
DAYS = 30
PRODUCTS = [("Wireless Mouse", 699), ("USB-C Cable", 299), ("Earbuds", 2499), ("Notebook Set", 199),
            ("Yoga Mat", 799), ("Water Bottle", 399), ("Mixer Grinder", 3499), ("Face Wash", 249)]
FIRST_NAMES = ["Aarav", "Diya", "Rohan", "Isha", "Kavya", "Neha", "Vivaan", "Meera"]
CITIES = ["Mumbai", "Pune", "Delhi", "Bengaluru", "Chennai"]

SCHEMA = """
CREATE TABLE customers (customer_id SERIAL PRIMARY KEY, name TEXT, email TEXT, city TEXT, signup_date DATE);
CREATE TABLE product_catalog (product_id SERIAL PRIMARY KEY, name TEXT, price NUMERIC(10, 2));
CREATE TABLE sales (sale_id SERIAL PRIMARY KEY, customer_email TEXT, customer_name TEXT, product TEXT,
                    quantity INT, amount NUMERIC(12, 2), created_at TIMESTAMP);
CREATE TABLE payments (payment_id SERIAL PRIMARY KEY, sale_id INT, method TEXT, status TEXT, paid_at TIMESTAMP);
CREATE TABLE audit_log (log_id SERIAL PRIMARY KEY, actor TEXT, action TEXT, logged_at TIMESTAMP);
"""


def make_day(day, rng, anomaly=None):
    """One day of sales and matching payments and audit rows. `anomaly` damages the day in a known way."""
    n = int(rng.gauss(200, 14))
    if anomaly == "row_drop":
        n = int(n * 0.35)
    seconds_limit = 9 * 3600 if anomaly == "late_partial" else 24 * 3600
    sales, payments = [], []
    for i in range(n):
        name = rng.choice(FIRST_NAMES)
        product, price = rng.choice(PRODUCTS)
        quantity = rng.randint(1, 4)
        amount = price * quantity * (100 if anomaly == "amount_x100" else 1)
        email = None if anomaly == "null_spike" and i % 10 < 4 else f"{name.lower()}{rng.randint(1, 999)}@example.com"
        ts = day + timedelta(seconds=rng.randint(0, seconds_limit - 1))
        sales.append((email, name, product, quantity, amount, ts))
        payments.append((rng.choice(["card", "upi", "cod"]), rng.choice(["paid", "paid", "paid", "failed"]), ts + timedelta(minutes=2)))
    if anomaly == "duplicates":
        sales += sales[: n // 8]
    audit = [(rng.choice(["system", "admin", "job"]), rng.choice(["login", "export", "update"]), day + timedelta(seconds=rng.randint(0, 86399)))
             for _ in range(50)]
    return sales, payments, audit


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--anomaly", choices=["amount_x100", "row_drop", "null_spike", "late_partial", "duplicates"],
                        help="damage yesterday's data in a known way")
    args = parser.parse_args()
    rng = random.Random(11)

    # The server's own superuser creates the database and the read-only user.
    admin = psycopg2.connect(config.get_db_url())
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
    cur.execute(f"CREATE DATABASE {DB}")
    cur.execute(f"DROP ROLE IF EXISTS {READER}")
    cur.execute(f"CREATE ROLE {READER} LOGIN PASSWORD '{READER_PASSWORD}'")
    admin.close()

    conn = psycopg2.connect(host=config.POSTGRES_HOST, port=config.POSTGRES_PORT, dbname=DB,
                            user=config.POSTGRES_USER, password=config.POSTGRES_PASSWORD)
    cur = conn.cursor()
    cur.execute(SCHEMA)
    execute_values(cur, "INSERT INTO customers (name, email, city, signup_date) VALUES %s",
                   [(rng.choice(FIRST_NAMES), f"customer{i}@example.com", rng.choice(CITIES), "2024-01-01") for i in range(300)])
    execute_values(cur, "INSERT INTO product_catalog (name, price) VALUES %s", PRODUCTS)

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for back in range(DAYS, 0, -1):   # DAYS days ago ... yesterday (today is left out: it is unfinished)
        day = today - timedelta(days=back)
        sales, payments, audit = make_day(day, rng, args.anomaly if back == 1 else None)
        execute_values(cur, "INSERT INTO sales (customer_email, customer_name, product, quantity, amount, created_at) VALUES %s", sales)
        execute_values(cur, "INSERT INTO payments (method, status, paid_at) VALUES %s", payments)
        execute_values(cur, "INSERT INTO audit_log (actor, action, logged_at) VALUES %s", audit)

    cur.execute(f"GRANT CONNECT ON DATABASE {DB} TO {READER}")
    cur.execute(f"GRANT USAGE ON SCHEMA public TO {READER}")
    cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {READER}")
    conn.commit()
    conn.close()
    print(f"Created database {DB} with {DAYS} days of data" + (f", yesterday has anomaly '{args.anomaly}'" if args.anomaly else ""))
    print(f"Connect as: user {READER} / password {READER_PASSWORD} (read-only), database {DB}, port {config.POSTGRES_PORT}")
