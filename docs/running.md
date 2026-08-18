# Running the agent

## First run

1. `python -m venv .venv` then `.venv\Scripts\activate`
2. `pip install -e ".[dev]"`
3. `playwright install chromium`
4. `claude setup-token`, then put the token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`
5. Add `APIFY_TOKEN` to `.env`
6. Confirm the API key is unset: `Remove-Item Env:ANTHROPIC_API_KEY`
7. Add at least 10 rows to the `fact` table. The gate refuses to run below that.
8. `career-agent run --max-score 3` and read the log

## Daily use

Nothing runs on its own until you ask it to.

- `career-agent run` performs one pass: discover, filter, score.
- `career-agent serve` opens the dashboard on http://localhost:8000 and tells you
  whether a scheduled run exists.
- `.\scripts\install-scheduler.ps1` opts in to a daily 08:00 run.
- `Unregister-ScheduledTask -TaskName CareerAgentDaily` opts back out.

## Changing what it searches for

Edit `career_brief.toml`. `search_locations` decides what each source is asked
for; `locations` decides what the filter accepts on the way back. There is no
command-line flag for either, deliberately.

## Pausing

`sqlite3 data/career.db "INSERT INTO event (type, payload) VALUES ('pause','on')"`
Clear it with payload `off`.
