"""Generate sample data: customers + products go into Postgres, 7 daily vendor-feed CSVs go into data/feeds/.

Run from the project root:  python -m scripts.seed
A fixed random seed means you get the same data every time.
"""

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from psycopg2.extras import execute_values

from app.db import get_conn

FEED_DIR = Path("data") / "feeds"
FIRST_DAY = date(2026, 10, 21)  # 7 clean days: Oct 21 .. Oct 27
NUM_DAYS = 7
NUM_CUSTOMERS = 200

CITIES = ["Mumbai", "Pune", "Delhi", "Bengaluru", "Hyderabad", "Chennai", "Kolkata", "Ahmedabad"]
FIRST_NAMES = ["Aarav", "Vivaan", "Aditya", "Ishaan", "Rohan", "Ananya", "Diya", "Isha", "Kavya", "Meera", "Neha", "Priya"]
LAST_NAMES = ["Sharma", "Patil", "Mehta", "Iyer", "Reddy", "Khan", "Singh", "Nair", "Joshi", "Das"]
# (name, category, price in INR)
PRODUCTS = [
    ("Wireless Mouse", "Electronics", 699), ("USB-C Cable", "Electronics", 299), ("Bluetooth Speaker", "Electronics", 1999),
    ("Power Bank", "Electronics", 1499), ("Earbuds", "Electronics", 2499), ("Laptop Stand", "Electronics", 899),
    ("Cotton T-Shirt", "Clothing", 499), ("Denim Jeans", "Clothing", 1799), ("Hoodie", "Clothing", 1299),
    ("Running Shoes", "Clothing", 2999), ("Cap", "Clothing", 349), ("Socks Pack", "Clothing", 249),
    ("Notebook Set", "Stationery", 199), ("Gel Pens", "Stationery", 149), ("Desk Organizer", "Stationery", 399),
    ("Sticky Notes", "Stationery", 99), ("Yoga Mat", "Fitness", 799), ("Dumbbell Pair", "Fitness", 1599),
    ("Skipping Rope", "Fitness", 249), ("Water Bottle", "Fitness", 399), ("Mixer Grinder", "Home", 3499),
    ("Cookware Set", "Home", 2799), ("Bedsheet", "Home", 899), ("LED Bulb Pack", "Home", 449),
    ("Face Wash", "Beauty", 249), ("Moisturizer", "Beauty", 399), ("Shampoo", "Beauty", 299), ("Perfume", "Beauty", 1199),
]


def make_customers(rng) -> pd.DataFrame:
    rows = []
    for customer_id in range(1, NUM_CUSTOMERS + 1):
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        signup = date(2024, 1, 1) + timedelta(days=int(rng.integers(0, 600)))
        rows.append((customer_id, name, str(rng.choice(CITIES)), signup))
    return pd.DataFrame(rows, columns=["customer_id", "name", "city", "signup_date"])


def make_products() -> pd.DataFrame:
    rows = [(i + 1, name, category, price) for i, (name, category, price) in enumerate(PRODUCTS)]
    return pd.DataFrame(rows, columns=["product_id", "name", "category", "price_inr"])


def make_feed(day: date, first_order_id: int, rng, customers, products) -> pd.DataFrame:
    """One day of clean orders, as the vendor would send them."""
    n = int(rng.integers(280, 321))
    product_ids = rng.choice(products["product_id"].to_numpy(), size=n)
    prices = products.set_index("product_id").loc[product_ids, "price_inr"].to_numpy()
    quantity = rng.integers(1, 5, size=n)
    seconds = np.sort(rng.integers(0, 24 * 3600, size=n))  # spread through the day, in time order
    order_ts = pd.to_datetime(day) + pd.to_timedelta(seconds, unit="s")
    return pd.DataFrame({
        "order_id": np.arange(first_order_id, first_order_id + n),
        "customer_id": rng.choice(customers["customer_id"].to_numpy(), size=n),
        "product_id": product_ids,
        "quantity": quantity,
        "amount_inr": prices * quantity,  # clean data: amount is always price x quantity
        "order_ts": order_ts.strftime("%Y-%m-%d %H:%M:%S"),
    })


def load_reference_tables(customers, products) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("TRUNCATE orders, customers, products CASCADE;")
    execute_values(cur, "INSERT INTO customers VALUES %s", customers.astype(object).values.tolist())
    execute_values(cur, "INSERT INTO products VALUES %s", products.astype(object).values.tolist())
    conn.commit()
    conn.close()


if __name__ == "__main__":
    rng = np.random.default_rng(42)
    customers = make_customers(rng)
    products = make_products()
    load_reference_tables(customers, products)
    print(f"Loaded {len(customers)} customers and {len(products)} products into Postgres")

    FEED_DIR.mkdir(parents=True, exist_ok=True)
    for old_file in FEED_DIR.glob("orders_*.csv"):  # start fresh, also removes any injected broken feed
        old_file.unlink()
    next_order_id = 100001
    for i in range(NUM_DAYS):
        day = FIRST_DAY + timedelta(days=i)
        feed = make_feed(day, next_order_id, rng, customers, products)
        feed.to_csv(FEED_DIR / f"orders_{day}.csv", index=False)
        next_order_id += len(feed)
        print(f"  wrote orders_{day}.csv  ({len(feed)} rows)")
