import json

from screener.db import Database


def test_undo_token2022_rejections():
    db = Database(":memory:")
    for addr in ("only2022", "also_lp", "clean_reject"):
        db.add_token(addr, "test")
    db.decide("only2022", "rejected", price=1, mcap=1, liquidity=1, reject_codes=["token2022_trap"],
              details={"reasons": ["Token-2022 trap: new accounts frozen by default"]})
    db.decide("also_lp", "rejected", price=1, mcap=1, liquidity=1, reject_codes=["lp_unlocked", "token2022_trap"],
              details={"reasons": ["only 10% of LP locked/burned", "Token-2022 trap: new accounts frozen by default"]})
    db.decide("clean_reject", "rejected", price=1, mcap=1, liquidity=1, reject_codes=["mint_authority"],
              details={"reasons": ["mint authority not revoked"]})
    db.add_snapshot("only2022", "1h", 2, 1000, 1)

    assert db.undo_token2022_rejections() == 1
    assert db.status("only2022") == "watching"
    assert db.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
    row = db.conn.execute("SELECT reject_codes, details FROM tokens WHERE address = 'also_lp'").fetchone()
    assert json.loads(row["reject_codes"]) == ["lp_unlocked"]
    assert json.loads(row["details"])["reasons"] == ["only 10% of LP locked/burned"]
    assert db.status("clean_reject") == "rejected"
    assert db.undo_token2022_rejections() == 0  # runs once only


def test_insider_count_reads_new_and_old_alerts():
    from screener.tracker import insider_count
    assert insider_count(json.dumps({"insiders": 33, "warnings": []})) == 33
    # Alerts from before the field existed only have the warning text.
    assert insider_count(json.dumps({"warnings": ["272 insider wallets detected", "no socials at all"]})) == 272
    assert insider_count(json.dumps({"warnings": ["no socials at all"]})) == 0
    assert insider_count(None) is None
