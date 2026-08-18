
# Authentication Setup — Career Agent (Claude Agent SDK + Subscription)

## Goal

Run the career agent's Claude calls against the Claude Pro/Max subscription
quota instead of a separate pay-per-token API key, using Anthropic's
officially supported mechanism for this.

## Do NOT

- Do not read, copy, or print `~/.claude/.credentials.json`,
  `%USERPROFILE%\.claude\.credentials.json`, or the macOS Keychain entry
  Claude Code uses for its own `/login` session.
- Do not construct or fake an `ANTHROPIC_API_KEY` from any subscription
  credential.
- Do not log, echo, commit, or store the resulting token anywhere except
  the local environment variable / secrets mechanism described below.
- Do not build a multi-user service on this token. It is licensed for
  individual use through Claude Code / the Agent SDK. Route all traffic
  in this project through one person's (the developer's) quota only.

## Supported method: `claude setup-token`

1. Confirm Claude Code CLI is installed and up to date:

   ```bash
   npm install -g @anthropic-ai/claude-code
   claude --version
   ```
2. Generate a long-lived subscription token via the official command.
   This opens a browser window for explicit login/consent — it does not
   read any existing stored session:

   ```bash
   claude setup-token
   ```

   Requires an active Pro, Max, Team, or Enterprise plan. The token is
   printed once to the terminal; it is not saved anywhere by the CLI.
3. Store it as an environment variable for the agent process only —
   e.g. in a local `.env` file that is git-ignored, or in Windows
   Credential Manager / an OS-level secrets store if you want it
   protected at rest:

   ```
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
   ```
4. Make sure `ANTHROPIC_API_KEY` is **not** also set in the same
   environment — if both are present, `ANTHROPIC_API_KEY` takes
   precedence and the subscription token will be silently ignored.

   ```bash
   unset ANTHROPIC_API_KEY   # bash
   Remove-Item Env:ANTHROPIC_API_KEY   # PowerShell
   ```
5. In the Python Agent SDK code, do not pass an API key explicitly —
   let the SDK pick up `CLAUDE_CODE_OAUTH_TOKEN` from the environment
   the same way the Claude Code CLI does. Verify this on startup with a
   one-line auth check (call a trivial tool-less prompt and confirm it
   succeeds) rather than by inspecting the token itself.

## Known limitations to design around

- The token can only make model requests — it cannot open Remote
  Control sessions or fetch claude.ai connectors. Locally-configured
  MCP servers still work, so this doesn't affect the
  Playwright/SQLite/FastAPI tools in the career agent design.
- Confirm whether the installed Agent SDK version invokes the CLI in
  "bare mode" — bare mode does not read `CLAUDE_CODE_OAUTH_TOKEN`. If
  it does, an API key or `apiKeyHelper` is required instead for this
  setup, and that changes the billing model back to pay-per-token.
- Token is valid for about a year; plan for `claude setup-token` to be
  re-run and the env var rotated before then. A scheduled task should
  fail loudly (not silently fall back to no auth) if the token expires.

## Rotation / revocation

If the token is ever exposed (committed to a repo, logged, shared),
revoke it immediately by logging out (`/logout` in Claude Code) and
running `claude setup-token` again to issue a new one.
