"""Walk through the whole watch flow in the terminal (no LLM): connect, safety check, scan, watch, check.

Run from the project root, after scripts.make_demo_target:
    python -m scripts.test_watch
"""

from app import config, watch

HOST, PORT = config.POSTGRES_HOST, config.POSTGRES_PORT

if __name__ == "__main__":
    print("1. Connect with the read-only user")
    connection_id, access = watch.save_connection("demo shop", HOST, PORT, "shop_demo", "demo_reader", "demo_reader_pw")
    print(f"   connection {connection_id}: read_only={access['read_only']} warnings={access['warnings']}")

    print("\n2. Connect with a powerful user to see the safety warning")
    _, risky = watch.save_connection("risky", HOST, PORT, "shop_demo", config.POSTGRES_USER, config.POSTGRES_PASSWORD)
    print(f"   read_only={risky['read_only']}")
    for w in risky["warnings"]:
        print("   warning:", w)

    print("\n3. Scan: tables, best candidates first")
    tables = watch.scan_connection(connection_id)
    for t in tables:
        print(f"   {t['schema']}.{t['table']:<16} rows={t['rows']:<6} time={t['suggested_time_column']!s:<11} key={t['suggested_key_column']!s:<11} "
              f"watchable={t['watchable']} score={t['score']}  exclude? {t['suggested_excluded']}")

    print("\n4. Watch the best table, excluding the columns we suggested")
    best = tables[0]
    watch_id = watch.add_watch(connection_id, best["schema"], best["table"], best["suggested_time_column"],
                               best["suggested_key_column"], best["suggested_excluded"])
    print(f"   watching {best['table']} as watch {watch_id}")

    print("\n5. Learn normal and check yesterday")
    result = watch.run_watch(watch_id)
    print(f"   day {result['day']}: {result['status']} ({result['message']})")
    for r in result["results"]:
        print(f"   [{'PASS' if r['passed'] else 'FAIL'}] {r['check_name']:<18} {r['details']}")
