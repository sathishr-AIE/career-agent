import datetime as dt
import sqlite3

from career_agent import outcomes

CALLBACK_LABELS = {"screen": "Response", "interview": "Interview",
                    "offer": "Offer", "rejected": "Rejected",
                    "no_response": "No Response"}


def _daily_counts(conn: sqlite3.Connection, sql: str) -> dict[str, int]:
    return {r["day"]: r["n"] for r in conn.execute(sql)}


def sparkline_values(conn: sqlite3.Connection, count_sql: str) -> list[int]:
    """count_sql must SELECT (day, n) grouped by an ISO date `day` column,
    covering at least the last 7 days. Zero-filled for days with no rows."""
    counts = _daily_counts(conn, count_sql)
    # SQLite's date('now') (used by every count_sql caller passes) is UTC.
    # dt.date.today() is the host's local calendar day, which disagrees with
    # UTC for part of every day off-UTC (e.g. Chennai, UTC+5:30, roughly
    # 05:30-11:00 IST) -- misaligning the "today" bucket against the SQL.
    today = dt.datetime.now(dt.timezone.utc).date()
    return [counts.get(str(today - dt.timedelta(days=i)), 0)
            for i in range(6, -1, -1)]


def sparkline_points(values: list[int]) -> str:
    """SVG polyline points, scaled into a 64x28 box (matches the prototype's
    .spark svg viewBox="0 0 64 28"). All-zero (or empty) series still emit
    one point per value, flat along the baseline (y=24) -- callers such as
    kpis() rely on a fixed 7-points-per-week shape regardless of activity."""
    if not values:
        return "0,24 63,24"
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    step = 63 / (len(values) - 1) if len(values) > 1 else 0
    points = []
    for i, v in enumerate(values):
        x = round(i * step, 1)
        y = round(24 - ((v - lo) / span) * 20, 1)
        points.append(f"{x},{y}")
    return " ".join(points)


def kpis(conn: sqlite3.Connection) -> dict:
    discovered = conn.execute(
        "SELECT COUNT(*) n FROM job"
        " WHERE date(discovered_at) = date('now')").fetchone()["n"]

    after_hard_filter = conn.execute(
        "SELECT COUNT(*) n FROM job j JOIN assessment a ON a.job_id = j.id"
        " WHERE date(j.discovered_at) = date('now')"
        "   AND a.stage = 'scored'"
        "   AND a.id = (SELECT id FROM assessment a2 WHERE a2.job_id = j.id"
        "               ORDER BY a2.created_at DESC, a2.id DESC LIMIT 1)"
        ).fetchone()["n"]

    shortlisted = conn.execute(
        "SELECT COUNT(*) n FROM job j JOIN assessment a ON a.job_id = j.id"
        " WHERE date(j.discovered_at) = date('now') AND a.stage = 'scored'"
        "   AND a.verdict IN ('submit','hold')"
        "   AND a.id = (SELECT id FROM assessment a2 WHERE a2.job_id = j.id"
        "               ORDER BY a2.created_at DESC, a2.id DESC LIMIT 1)"
        ).fetchone()["n"]

    applied = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        "   AND date(submitted_at) = date('now')").fetchone()["n"]

    responses, _total_submitted = outcomes.callback_rate(conn)

    sparklines = {
        "discovered": sparkline_points(sparkline_values(conn,
            "SELECT date(discovered_at) day, COUNT(*) n FROM job"
            " WHERE discovered_at >= date('now', '-6 days') GROUP BY day")),
        "after_hard_filter": sparkline_points(sparkline_values(conn,
            "SELECT date(j.discovered_at) day, COUNT(*) n FROM job j"
            " JOIN assessment a ON a.job_id = j.id WHERE a.stage = 'scored'"
            "   AND a.id = (SELECT id FROM assessment a2 WHERE a2.job_id = j.id"
            "               ORDER BY a2.created_at DESC, a2.id DESC LIMIT 1)"
            "   AND j.discovered_at >= date('now', '-6 days') GROUP BY day")),
        "shortlisted": sparkline_points(sparkline_values(conn,
            "SELECT date(j.discovered_at) day, COUNT(*) n FROM job j"
            " JOIN assessment a ON a.job_id = j.id WHERE a.stage = 'scored'"
            "   AND a.verdict IN ('submit','hold')"
            "   AND a.id = (SELECT id FROM assessment a2 WHERE a2.job_id = j.id"
            "               ORDER BY a2.created_at DESC, a2.id DESC LIMIT 1)"
            "   AND j.discovered_at >= date('now', '-6 days') GROUP BY day")),
        "applied": sparkline_points(sparkline_values(conn,
            "SELECT date(submitted_at) day, COUNT(*) n FROM application"
            " WHERE status = 'submitted'"
            "   AND submitted_at >= date('now', '-6 days') GROUP BY day")),
        "responses": sparkline_points(sparkline_values(conn,
            "SELECT date(occurred_at) day, COUNT(*) n FROM outcome"
            " WHERE type IN ('screen','interview','offer')"
            "   AND occurred_at >= date('now', '-6 days') GROUP BY day")),
    }

    return {"discovered": discovered, "after_hard_filter": after_hard_filter,
            "shortlisted": shortlisted, "applied": applied,
            "responses": responses, "sparklines": sparklines}


def outcome_summary(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT id FROM application WHERE status = 'submitted'"
        "   AND date(submitted_at) >= date('now', 'start of month')").fetchall()
    applied = len(rows)
    effective = [outcomes.effective_outcome(conn, r["id"]) for r in rows]
    responses = sum(1 for o in effective if o in outcomes.CALLBACK_TYPES)
    interviews = sum(1 for o in effective if o in ("interview", "offer"))
    offers = sum(1 for o in effective if o == "offer")
    callback_rate = round(100 * responses / applied, 1) if applied else 0.0
    interview_rate = round(100 * interviews / applied, 1) if applied else 0.0
    return {"applied": applied, "responses": responses, "interviews": interviews,
            "offers": offers, "callback_rate": callback_rate,
            "interview_rate": interview_rate}


def score_distribution(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT weighted_score FROM assessment a WHERE stage = 'scored'"
        "   AND weighted_score IS NOT NULL"
        "   AND a.id = (SELECT id FROM assessment a2 WHERE a2.job_id = a.job_id"
        "               ORDER BY a2.created_at DESC, a2.id DESC LIMIT 1)"
        ).fetchall()
    total = len(rows)
    if total == 0:
        return {"total": 0, "high": 0, "good": 0, "fair": 0, "low": 0}
    buckets = {"high": 0, "good": 0, "fair": 0, "low": 0}
    for r in rows:
        s = r["weighted_score"]
        if s >= 80:
            buckets["high"] += 1
        elif s >= 60:
            buckets["good"] += 1
        elif s >= 40:
            buckets["fair"] += 1
        else:
            buckets["low"] += 1
    return {"total": total,
            **{k: round(100 * v / total, 1) for k, v in buckets.items()}}
