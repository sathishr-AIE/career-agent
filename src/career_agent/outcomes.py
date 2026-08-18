import sqlite3

CALLBACK_TYPES = ("screen", "interview", "offer")


def derive_no_response(conn: sqlite3.Connection, after_days: int = 30) -> int:
    """Silence is never reported, so it has to be inferred. Without this the
    callback-rate denominator shrinks to whatever you remembered to annotate,
    which inflates the rate the v3 decision depends on."""
    cur = conn.execute(
        "INSERT INTO outcome (application_id, type, derived)"
        " SELECT a.id, 'no_response', 1 FROM application a"
        "  WHERE a.status = 'submitted'"
        f"   AND a.submitted_at < datetime('now', '-{int(after_days)} days')"
        "    AND NOT EXISTS (SELECT 1 FROM outcome o WHERE o.application_id = a.id)")
    conn.commit()
    return cur.rowcount


def effective_outcome(conn: sqlite3.Connection, application_id: int) -> str | None:
    """Latest occurred_at wins; a derived row loses a tie against a manual one."""
    row = conn.execute(
        "SELECT type FROM outcome WHERE application_id = ?"
        " ORDER BY occurred_at DESC, derived ASC, id DESC LIMIT 1",
        (application_id,)).fetchone()
    return row["type"] if row else None


def callback_rate(conn: sqlite3.Connection) -> tuple[int, int]:
    """Returns (callbacks, total submitted). The denominator is every submitted
    application, not only the annotated ones."""
    rows = conn.execute(
        "SELECT id FROM application WHERE status = 'submitted'").fetchall()
    callbacks = sum(1 for r in rows
                    if effective_outcome(conn, r["id"]) in CALLBACK_TYPES)
    return callbacks, len(rows)
