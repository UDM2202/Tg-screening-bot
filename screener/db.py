"""SQLite storage: the watchlist, every decision, and price checkpoints for the tracker."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    address      TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    first_seen   REAL NOT NULL,
    last_checked REAL,
    status       TEXT NOT NULL DEFAULT 'watching',  -- watching | alerted | rejected | expired
    symbol       TEXT,
    name         TEXT,
    decided_at   REAL,
    ref_price    REAL,  -- price at decision time, the baseline for returns
    ref_mcap     REAL,
    ref_liquidity REAL,
    reject_codes TEXT,  -- JSON list of tier 1 rule codes
    details      TEXT   -- JSON blob of facts shown in the alert
);
CREATE INDEX IF NOT EXISTS tokens_status ON tokens(status, first_seen);

CREATE TABLE IF NOT EXISTS snapshots (
    address    TEXT NOT NULL,
    checkpoint TEXT NOT NULL,
    taken_at   REAL NOT NULL,
    price      REAL,  -- NULL when the token has no pool anymore
    liquidity  REAL,
    market_cap REAL,
    missed     INTEGER NOT NULL DEFAULT 0,  -- 1 when the bot was offline at checkpoint time
    PRIMARY KEY (address, checkpoint)
);

CREATE TABLE IF NOT EXISTS websites (
    url     TEXT NOT NULL,
    address TEXT NOT NULL,
    PRIMARY KEY (url, address)
);

CREATE TABLE IF NOT EXISTS watches (
    address          TEXT PRIMARY KEY,
    symbol           TEXT,
    bought           INTEGER NOT NULL DEFAULT 0,  -- 1 after /bought: full watch, else rug-only
    active           INTEGER NOT NULL DEFAULT 1,
    started_at       REAL NOT NULL,               -- alert time, or /bought time
    entry_price      REAL,
    entry_mcap       REAL,
    peak_price       REAL,
    peak_mcap        REAL,
    max_liquidity    REAL,
    sent             TEXT,                        -- JSON list of one-off events already sent
    dip_peak         REAL,                        -- peak price when the last dip warning went out
    last_pressure_at REAL,
    alert_message_id INTEGER
);
CREATE INDEX IF NOT EXISTS watches_message ON watches(alert_message_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # --- watchlist -------------------------------------------------------

    def add_token(self, address: str, source: str, now: float | None = None) -> bool:
        """Add a token to the watchlist. Returns False if it was already known."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO tokens (address, source, first_seen) VALUES (?, ?, ?)",
            (address, source, now or time.time()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def watching(self, limit: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT address FROM tokens WHERE status = 'watching' ORDER BY first_seen DESC LIMIT ?",
            (limit,),
        )
        return [r["address"] for r in rows]

    def expire_unseen(self, older_than: float) -> int:
        """Drop watchlist tokens that never got a usable pool."""
        cur = self.conn.execute(
            "UPDATE tokens SET status = 'expired' WHERE status = 'watching' AND first_seen < ?",
            (older_than,),
        )
        self.conn.commit()
        return cur.rowcount

    def touch(self, address: str, symbol: str | None = None, name: str | None = None) -> None:
        self.conn.execute(
            "UPDATE tokens SET last_checked = ?, symbol = COALESCE(?, symbol), name = COALESCE(?, name)"
            " WHERE address = ?",
            (time.time(), symbol, name, address),
        )
        self.conn.commit()

    def set_expired(self, address: str) -> None:
        self.conn.execute("UPDATE tokens SET status = 'expired' WHERE address = ?", (address,))
        self.conn.commit()

    def decide(
        self,
        address: str,
        status: str,
        *,
        price: float | None,
        mcap: float | None,
        liquidity: float | None,
        reject_codes: list[str] | None = None,
        details: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE tokens SET status = ?, decided_at = ?, ref_price = ?, ref_mcap = ?,"
            " ref_liquidity = ?, reject_codes = ?, details = ? WHERE address = ?",
            (
                status,
                now or time.time(),
                price,
                mcap,
                liquidity,
                json.dumps(reject_codes or []),
                json.dumps(details or {}, default=str),
                address,
            ),
        )
        self.conn.commit()

    def status(self, address: str) -> str | None:
        row = self.conn.execute("SELECT status FROM tokens WHERE address = ?", (address,)).fetchone()
        return row["status"] if row else None

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM tokens GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def reject_summary(self, recent: int = 5) -> tuple[dict[str, int], int, list[sqlite3.Row]]:
        """How often each rule rejected a coin, total rejections, and the latest rejections."""
        counts: dict[str, int] = {}
        total = 0
        for row in self.conn.execute("SELECT reject_codes FROM tokens WHERE status = 'rejected'"):
            total += 1
            for code in json.loads(row["reject_codes"] or "[]"):
                counts[code] = counts.get(code, 0) + 1
        latest = list(
            self.conn.execute(
                "SELECT symbol, address, details FROM tokens WHERE status = 'rejected'"
                " ORDER BY decided_at DESC LIMIT ?",
                (recent,),
            )
        )
        return counts, total, latest

    def recent_alerts(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM tokens WHERE status = 'alerted' ORDER BY decided_at DESC LIMIT ?",
                (limit,),
            )
        )

    def recent_alert_ages(self, limit: int = 20) -> list[float]:
        """Coin age in minutes at the moment of each recent alert."""
        ages = []
        for row in self.recent_alerts(limit):
            age = json.loads(row["details"] or "{}").get("age_minutes")
            if age is not None:
                ages.append(float(age))
        return ages

    # --- websites (reuse detection) --------------------------------------

    def record_websites(self, address: str, urls: list[str]) -> dict[str, int]:
        """Store this token's websites; return how many *other* tokens used each one."""
        reuse = {}
        for url in urls:
            key = url.lower().rstrip("/")
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM websites WHERE url = ? AND address != ?", (key, address)
            ).fetchone()
            reuse[url] = row["n"]
            self.conn.execute("INSERT OR IGNORE INTO websites VALUES (?, ?)", (key, address))
        self.conn.commit()
        return reuse

    # --- tracker ---------------------------------------------------------

    def due_snapshots(
        self, checkpoints: dict[str, int], statuses: list[str], now: float
    ) -> list[tuple[str, str, float]]:
        """(address, checkpoint, target time) for every checkpoint that is due."""
        due = []
        marks = ",".join("?" * len(statuses))
        for name, seconds in checkpoints.items():
            rows = self.conn.execute(
                f"SELECT t.address, t.decided_at + ? AS target FROM tokens t WHERE t.status IN ({marks})"
                " AND t.decided_at IS NOT NULL AND t.decided_at + ? <= ?"
                " AND NOT EXISTS (SELECT 1 FROM snapshots s WHERE s.address = t.address AND s.checkpoint = ?)",
                (seconds, *statuses, seconds, now, name),
            )
            due.extend((r["address"], name, r["target"]) for r in rows)
        return due

    def add_snapshot(
        self,
        address: str,
        checkpoint: str,
        price: float | None,
        liquidity: float | None,
        market_cap: float | None,
        now: float | None = None,
        missed: bool = False,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
            (address, checkpoint, now or time.time(), price, liquidity, market_cap, int(missed)),
        )
        self.conn.commit()

    def alert_progress(self, checkpoints: list[str], since: float = 0) -> dict[str, tuple[int, int]]:
        """Per checkpoint: (alerts still waiting for it, alerts whose checkpoint was missed)."""
        progress = {}
        for cp in checkpoints:
            row = self.conn.execute(
                "SELECT"
                " SUM(CASE WHEN s.address IS NULL THEN 1 ELSE 0 END) AS waiting,"
                " SUM(CASE WHEN s.missed = 1 THEN 1 ELSE 0 END) AS missed"
                " FROM tokens t LEFT JOIN snapshots s ON s.address = t.address AND s.checkpoint = ?"
                " WHERE t.status = 'alerted' AND t.decided_at >= ?",
                (cp, since),
            ).fetchone()
            progress[cp] = (row["waiting"] or 0, row["missed"] or 0)
        return progress

    def outcomes(self, since: float = 0) -> list[sqlite3.Row]:
        """One row per (decided token, checkpoint) with the baseline and snapshot."""
        return list(
            self.conn.execute(
                "SELECT t.address, t.symbol, t.status, t.reject_codes, t.details, t.ref_price, t.decided_at,"
                " s.checkpoint, s.price, s.liquidity"
                " FROM tokens t JOIN snapshots s ON s.address = t.address"
                " WHERE t.status IN ('alerted', 'rejected') AND s.missed = 0 AND t.decided_at >= ?",
                (since,),
            )
        )

    def undo_token2022_rejections(self) -> int:
        """One-off repair for a GoPlus parsing bug that flagged every coin as a Token-2022 trap.

        Removes that rule from past rejections. Coins with no other failed rule go back on
        the watchlist (their tracker data is dropped, since they were never truly rejected).
        """
        if self.get_meta("fixed_token2022_v1") == "1":
            return 0
        requeued = 0
        rows = list(self.conn.execute(
            "SELECT address, reject_codes, details FROM tokens WHERE status = 'rejected'"
        ))
        for row in rows:
            codes = json.loads(row["reject_codes"] or "[]")
            if "token2022_trap" not in codes:
                continue
            codes = [c for c in codes if c != "token2022_trap"]
            details = json.loads(row["details"] or "{}")
            details["reasons"] = [r for r in details.get("reasons", []) if not r.startswith("Token-2022 trap")]
            if codes:
                self.conn.execute(
                    "UPDATE tokens SET reject_codes = ?, details = ? WHERE address = ?",
                    (json.dumps(codes), json.dumps(details), row["address"]),
                )
            else:
                self.conn.execute(
                    "UPDATE tokens SET status = 'watching', decided_at = NULL, ref_price = NULL,"
                    " ref_mcap = NULL, ref_liquidity = NULL, reject_codes = NULL, details = NULL"
                    " WHERE address = ?",
                    (row["address"],),
                )
                self.conn.execute("DELETE FROM snapshots WHERE address = ?", (row["address"],))
                requeued += 1
        self.set_meta("fixed_token2022_v1", "1")
        self.conn.commit()
        return requeued

    # --- watches (follow-ups after an alert) ----------------------------

    WATCH_FIELDS = (
        "address", "symbol", "bought", "active", "started_at", "entry_price", "entry_mcap",
        "peak_price", "peak_mcap", "max_liquidity", "sent", "dip_peak", "last_pressure_at",
        "alert_message_id",
    )

    def save_watch(self, values: dict[str, Any]) -> None:
        cols = ", ".join(self.WATCH_FIELDS)
        marks = ", ".join("?" * len(self.WATCH_FIELDS))
        self.conn.execute(
            f"INSERT OR REPLACE INTO watches ({cols}) VALUES ({marks})",
            tuple(values.get(f) for f in self.WATCH_FIELDS),
        )
        self.conn.commit()

    def get_watch(self, address: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM watches WHERE address = ?", (address,)).fetchone()

    def watch_for_message(self, message_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM watches WHERE alert_message_id = ?", (message_id,)
        ).fetchone()

    def active_watches(self, alert_hours: float, position_hours: float, now: float) -> list[sqlite3.Row]:
        """Bought coins within the position window, plus rug-only watches within the alert window."""
        return list(self.conn.execute(
            "SELECT * FROM watches WHERE active = 1 AND ("
            " (bought = 1 AND started_at >= ?) OR (bought = 0 AND started_at >= ?))",
            (now - position_hours * 3600, now - alert_hours * 3600),
        ))

    def positions(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM watches WHERE bought = 1 AND active = 1 ORDER BY started_at DESC"
        ))

    def stop_watch(self, address: str) -> None:
        self.conn.execute("UPDATE watches SET active = 0 WHERE address = ?", (address,))
        self.conn.commit()

    def alerted_by_symbol(self, symbol: str) -> str | None:
        """Address of the most recent alerted coin with this symbol."""
        row = self.conn.execute(
            "SELECT address FROM tokens WHERE status = 'alerted' AND LOWER(symbol) = LOWER(?)"
            " ORDER BY decided_at DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return row["address"] if row else None

    # --- meta ------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.conn.commit()
