import sqlite3

CANDIDATE_SQL = """
SELECT j.id AS job_id, j.company, j.title, a.weighted_score
  FROM job j
  JOIN assessment a ON a.job_id = j.id
 WHERE j.merged_into_job_id IS NULL
   AND a.verdict IN ('submit','hold')
   AND NOT EXISTS (
       SELECT 1 FROM application ap
        WHERE ap.job_id = j.id
          AND ap.status IN ('in_flight','submitted','held_unknown',
                             'failed_permanent')
   )
 ORDER BY j.priority ASC NULLS LAST, a.weighted_score DESC
 LIMIT 1
"""


def get_run_state(conn: sqlite3.Connection, kind: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM run_state WHERE kind = ?", (kind,)).fetchone()


def set_run_state(conn: sqlite3.Connection, kind: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE run_state SET {cols}, updated_at = datetime('now')"
        " WHERE kind = ?", (*fields.values(), kind))
    conn.commit()


def next_candidate(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(CANDIDATE_SQL).fetchone()


def queue_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) n FROM job j JOIN assessment a ON a.job_id = j.id"
        " WHERE j.merged_into_job_id IS NULL"
        "   AND a.verdict IN ('submit','hold')"
        "   AND NOT EXISTS (SELECT 1 FROM application ap"
        "                    WHERE ap.job_id = j.id"
        "                      AND ap.status IN ('in_flight','submitted',"
        "                                        'held_unknown','failed_permanent'))"
    ).fetchone()["n"]
