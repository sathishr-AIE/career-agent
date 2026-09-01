import sqlite3

CALLBACK_TYPES = ("screen", "interview", "offer")

# no_response is deliberately absent: nobody writes to say they are
# ignoring you, so it is derived after 30 days rather than entered. Left
# manual, the table would fill with rejections and screens and stay silent
# on the majority case, inflating callback rate by shrinking its
# denominator to whatever you remembered to annotate.
MANUAL_TYPES = ("rejected", "screen", "interview", "offer")


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
        " ORDER BY date(occurred_at) DESC, derived ASC, occurred_at DESC, id DESC LIMIT 1",
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


def record(conn: sqlite3.Connection, application_id: int, type_: str,
           occurred_at: str, notes: str | None = None) -> int:
    """Hand-enter an outcome. Corrections supersede rather than edit:
    effective_outcome takes the latest occurred_at, so recording again
    overrides without deleting the history."""
    if type_ == "no_response":
        raise ValueError(
            "no_response is derived after 30 days, not entered by hand")
    if type_ not in MANUAL_TYPES:
        raise ValueError(
            f"unknown outcome type {type_!r}; expected one of {MANUAL_TYPES}")

    cur = conn.execute(
        "INSERT INTO outcome (application_id, type, derived, occurred_at,"
        " notes) VALUES (?, ?, 0, ?, ?)",
        (application_id, type_, occurred_at, notes))
    conn.commit()
    return cur.lastrowid
