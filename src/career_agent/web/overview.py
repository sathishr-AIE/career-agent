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


def source_performance(conn: sqlite3.Connection) -> list[dict]:
    # ponytail: response rate here treats "any callback-type outcome row
    # exists" as a response, skipping the derived-vs-manual tie-break
    # outcomes.effective_outcome() applies. Fine for a per-source
    # aggregate; upgrade to effective_outcome() per application if this
    # ever needs to match outcome_summary()'s numbers exactly.
    rows = conn.execute(
        "SELECT j.source,"
        "       COUNT(DISTINCT j.id) discovered,"
        "       COUNT(DISTINCT CASE WHEN a.stage='scored' THEN j.id END)"
        "         after_hard_filter,"
        "       COUNT(DISTINCT CASE WHEN a.stage='scored' AND a.verdict IN"
        "             ('submit','hold') THEN j.id END) shortlisted,"
        "       COUNT(DISTINCT ap.id) applied,"
        "       COUNT(DISTINCT CASE WHEN o.type IN"
        "             ('screen','interview','offer') THEN ap.id END) responded"
        "  FROM job j"
        "  LEFT JOIN assessment a ON a.job_id = j.id"
        "   AND a.id = (SELECT id FROM assessment a2 WHERE a2.job_id = j.id"
        "               ORDER BY a2.created_at DESC, a2.id DESC LIMIT 1)"
        "  LEFT JOIN application ap ON ap.job_id = j.id AND ap.status = 'submitted'"
        "  LEFT JOIN outcome o ON o.application_id = ap.id"
        " WHERE j.merged_into_job_id IS NULL"
        " GROUP BY j.source"
        " ORDER BY discovered DESC").fetchall()
    result = []
    for r in rows:
        pass_rate = (round(100 * r["after_hard_filter"] / r["discovered"], 1)
                     if r["discovered"] else 0.0)
        shortlist_rate = (round(100 * r["shortlisted"] / r["after_hard_filter"], 1)
                          if r["after_hard_filter"] else 0.0)
        response_rate = (round(100 * r["responded"] / r["applied"], 1)
                         if r["applied"] else 0.0)
        result.append({"source": r["source"], "discovered": r["discovered"],
                        "pass_rate": pass_rate, "shortlist_rate": shortlist_rate,
                        "applied": r["applied"], "response_rate": response_rate})
    return result


def recent_discoveries(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT j.id, j.title, j.company, j.source, j.location, j.url,"
        "       a.stage, a.verdict, a.weighted_score"
        "  FROM job j LEFT JOIN assessment a ON a.job_id = j.id"
        "   AND a.id = (SELECT id FROM assessment a2 WHERE a2.job_id = j.id"
        "               ORDER BY a2.created_at DESC, a2.id DESC LIMIT 1)"
        " WHERE j.merged_into_job_id IS NULL"
        " ORDER BY j.discovered_at DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for r in rows:
        if r["stage"] == "hard":
            gate = "fail"
        elif r["stage"] == "scored" and r["verdict"] == "skip":
            gate = "review"
        elif r["stage"] == "scored":
            gate = "pass"
        else:
            gate = "pending"
        result.append({**dict(r), "gate": gate})
    return result


def recent_outcomes(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT ap.id AS application_id, j.title, j.company, ap.submitted_at"
        "  FROM application ap JOIN job j ON j.id = ap.job_id"
        " WHERE ap.status = 'submitted'"
        " ORDER BY ap.submitted_at DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for r in rows:
        effective = outcomes.effective_outcome(conn, r["application_id"])
        label = CALLBACK_LABELS.get(effective, "Applied")
        result.append({"title": r["title"], "company": r["company"],
                        "label": label, "submitted_at": r["submitted_at"]})
    return result
