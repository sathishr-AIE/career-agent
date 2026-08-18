# Browser automation options: Agent Reach, Epify, and what actually replaces Playwright

Research date: 2026-08-18. Written to answer one question: can Agent Reach or Epify
replace Playwright as the way the career agent logs into LinkedIn and Naukri and
submits applications?

Short answer: no, for both, but for different reasons. Details below, plus what the
search did turn up that is worth using.

## Epify, which turned out to be Apify

There is no developer tool called Epify. Searching the name returns a hair removal
cream sold on Amazon, Walmart, and eBay, plus a Turkish cosmetics shop at epify.co.
Nothing in the AI, scraping, or automation space uses the name. Confirmed with
Sathish that the intended tool was **Apify**.

Apify is a hosted platform for web scraping. You rent pre-built scrapers, called
Actors, instead of writing and maintaining your own.

An earlier draft of this document said Apify covers discovery but not submission.
That was wrong. The Store also carries Actors that fill and submit job applications,
including LinkedIn Easy Apply, by running a browser with your session cookies. They
are covered in their own section below, along with what they do not solve.

### What it costs

Two layers stack. The plan gives you a monthly credit, then Actors draw it down.

| Plan | Monthly | Prepaid credit | Compute unit rate |
|---|---|---|---|
| Free | $0 | $5 | $0.20 |
| Starter | $29 | $29 | $0.20 |
| Scale | $199 | $199 | $0.16 |
| Business | $999 | $999 | $0.13 |

A compute unit is 1 GB of Actor RAM for one hour. Most Store Actors run in 256 to
512 MB for a few minutes, so a typical scraping run costs between $0.001 and $0.05
in compute. On top of that, individual Actors charge their own fee: per result, per
event, or a flat monthly rental.

For our volume the compute cost is noise. The Actor fees are what matter, and they
vary by more than a factor of 30 between listings that do the same job.

### LinkedIn job Actors

| Actor | Price | Notes |
|---|---|---|
| `valig/linkedin-jobs-scraper` | $0.40 / 1,000 jobs | 11K users, rated 4.7 |
| `practicaltools/linkedin-jobs` | $1.00 / 1,000 jobs | No login required. Descriptions cost $0.001 each and are opt-in. Zero results means zero charge. |
| `curious_coder/linkedin-jobs-scraper` | from $1.00 / 1,000 | Rich output including recruiter name and profile URL |
| `logical_scrapers/linkedin-jobs-scraper` | $5.00 / 1,000 | |
| `bebity/linkedin-jobs-scraper` | $29.99 / month rental | Only cheaper above roughly 3,000 jobs a month |

At 50 LinkedIn jobs a day with descriptions fetched, `practicaltools/linkedin-jobs`
runs about $0.10 a day, or $3 a month. The $29.99 rental makes no sense at our
volume.

### Naukri Actors

Naukri turned out to be better covered than expected, with at least six Actors
maintained by different authors.

`automation-lab/naukri-scraper` looks like the strongest option. It has 2,510 users
with a 100% run success rate, and it drives headless Chromium behind residential
proxies specifically to get past Naukri's Akamai bot protection, then intercepts the
internal search API. Output is unusually complete for our purposes: salary parsed
into INR minimum and maximum rather than a display string, skills as an array,
work mode, experience range, full description, and AmbitionBox company ratings with
review counts. That last field feeds the quality gate's opportunity-quality
dimension directly. It handles up to 5,000 jobs per run and takes about 80 seconds
for 20 jobs.

`unfenced-group/naukri-scraper` has one feature worth stealing regardless of which
Actor we pick: a `skipReposts` flag backed by cross-run memory of the last 50,000
job IDs. That is deduplication handled upstream, before we pay to transfer the data.

`parseforge/naukri-com-scraper` skips the browser entirely and hits Naukri's API,
which makes it faster and cheaper, but it has 214 users against 2,510 and free
accounts are capped at 10 items.

`memo23/naukri-scraper` covers Naukri and NaukriGulf in one run with 41 fields, at
4.05 stars from 2,656 users. Relevant only if Gulf roles interest you.

Avoid `infinity_and_beyond/naukri-jobs-scraper`: 3 total users, 0 monthly.

### Actors that actually submit applications

These exist, and they change the plan.

`sunny_spade/linkedin-easy-apply-bot` is the closest fit. It authenticates with your
LinkedIn session cookies rather than a password, launches a real Chrome through
Browserbase on a residential IP, searches by keyword and location, filters postings,
fills every Easy Apply field from your profile data, and submits. Its input schema
already contains the controls we designed by hand: `dryRun` to find and filter
without submitting, `dailyApplicationCap` defaulting to 40, and
`minDelayBetweenAppsSeconds` and `maxDelayBetweenAppsSeconds` defaulting to 45 and
90. It needs your own Browserbase API key and project ID, so it adds a second
vendor. Version 0.1.

`giovannibiancia/linkedin-easy-apply` costs $25 a month and takes a different route:
your cookies plus a matching user agent, Apify Proxy, and Google Gemini generating
answers to whatever the form asks. It handles multi-step forms, dropdowns, radio
buttons, and checkboxes, and supports a company blacklist and a `maxApplications`
cap.

`loving_wishbone/seek-apply` drives Playwright against SEEK and handles login, cover
letter, resume upload, and screening questions. Australia only, so not useful to us,
but its input schema is worth reading: it accepts either a tailored resume URL or a
structured resume object that it renders to PDF inside the Actor. That is the
per-role tailoring problem, already solved by someone else, in a shape we could
copy.

`harvestlabs/job-search-assistant-ai-agent` scores listings against preferences and
writes cover letters, but it stops short of submitting.

Two gaps remain. **No Naukri auto-apply Actor exists**, only Naukri scrapers, so
Naukri submissions stay ours to solve. And every one of these Actors is community
published rather than Apify official, at low version numbers, with no user counts on
their Store pages. Treat reliability as unproven until we dry-run them.

### What Apify does not solve: captchas

Apify has no built-in captcha solving. Its own academy documentation routes you to a
third-party service:

> reCAPTCHAs can be solved using the Anti CAPTCHA reCAPTCHA Actor on the Apify
> platform (note that this method requires an account on anti-captcha.com).

So captcha solving means a separate paid vendor and a separate account. Apify's
engineering blog frames the preferred approach as avoidance rather than solving,
through trustworthy browser fingerprints and residential proxies. That is why
`sunny_spade`'s Actor routes through Browserbase: the goal is to never be shown a
captcha, not to answer one.

This is a real distinction. Avoidance works until it doesn't, and when it fails
there is no fallback except a human. Any design that assumes captchas are handled is
assuming a probability, not a guarantee.

### The cookie question

Every one of these apply Actors needs your live LinkedIn session cookies uploaded to
Apify, where a community-published Actor uses them on infrastructure neither of us
controls. Those cookies are equivalent to being logged in as you. This is a decision
to make deliberately rather than discover later.

The local alternative keeps the session on your machine and never transmits it. The
tradeoff is that you then own the form-filling code and its 63% to 78% accuracy
problem.

### Wiring it to the agent

Apify runs an MCP server, which the Claude Agent SDK speaks natively, so this needs
no adapter code. Two deployment options:

- Hosted at `https://mcp.apify.com`, authenticated by OAuth or a bearer token.
- Local over stdio via `npx @apify/actors-mcp-server` with `APIFY_TOKEN` set.

One constraint decides between them: rental Actors work only against the hosted
server. Since every Actor we would actually pick is pay-per-result rather than
rental, local stdio stays available, and it keeps the token on your machine.

The server exposes around 20 tools, including `search-actors`, `fetch-actor-details`,
`call-actor`, `get-actor-run`, and `get-dataset-items`. It reads an Actor's input
schema and generates a matching MCP tool, so the agent sees typed parameters without
us writing any binding code.

By default it loads the actors category, the docs category, and
`apify/rag-web-browser`. Apify's own documentation recommends against relying on
that default:

> For production use and stable interfaces, always explicitly specify the `tools`
> parameter.

Worth following, and for a second reason beyond stability. Leaving `search-actors`
exposed lets the agent browse a 30,000-Actor store and start runs we never
sanctioned, each one costing money. Pinning the tool list to the two or three Actors
we chose turns an open-ended spending surface into a fixed one.

Sources: [Apify pricing](https://apify.com/pricing),
[apify/apify-mcp-server](https://github.com/apify/apify-mcp-server),
[automation-lab/naukri-scraper](https://apify.com/automation-lab/naukri-scraper),
[practicaltools/linkedin-jobs](https://apify.com/practicaltools/linkedin-jobs)

## Agent Reach

Two unrelated open source projects use this name. Neither replaces Playwright for
this project, and it is worth being clear about which one is which, because they get
conflated in social media posts.

### Panniantong/Agent-Reach

This is the one with the star count people quote. It gives agents read and search
access to about 14 platforms, including Twitter, Reddit, YouTube, GitHub, and
Bilibili. Each platform is a TOML manifest wrapping an existing CLI, and the tool
installs them the way Homebrew installs packages.

The maintainer describes the scope directly, in a comment closing pull request #42:

> Agent Reach is an installer + doctor tool. After setup, agents call upstream tools
> directly.

That is the whole story. It installs and health-checks other people's tools. It has
no browser engine, no form filling, and no job boards. For the career agent it would
be a research tool, useful if you later want the agent to read Reddit threads about
a company before applying. It has nothing to do with submitting applications.

Source: [Panniantong/Agent-Reach](https://github.com/Panniantong/Agent-Reach)

### tenlifejosh/agentreach

Different project, much closer to what you had in mind, and the interesting one. It
solves the session problem: you log into a site once in a visible browser, it
captures the cookies, encrypts them in a local vault, and replays them in headless
sessions from then on. Encryption is Fernet with a machine-specific key derived
through PBKDF2 at 480,000 iterations, and nothing leaves the disk.

Two things rule it out as a Playwright replacement.

First, it is built on Playwright. Installation is literally:

```bash
pip install agentreach
playwright install chromium
```

It harvests cookies from a real login and injects them into a Playwright context.
It is a session management layer sitting on top of Playwright, not an alternative to
it. Adopting it means keeping Playwright and adding a dependency.

Second, the supported platforms are Amazon KDP, Etsy, Gumroad, Pinterest, Reddit, X,
and Nextdoor. No LinkedIn. No Naukri. No job boards of any kind. Each platform needs
a hand-written driver that encodes that site's forms, selectors, and quirks, so
adding LinkedIn means writing that driver yourself, at which point you are back to
writing Playwright code.

Its own README also warns about exactly the risk this project carries:

> Platforms with advanced bot detection (Twitter/X, Reddit, Amazon KDP) may require
> session refresh more frequently. These platforms actively fingerprint headless
> browsers and may invalidate sessions within hours to days.

LinkedIn fingerprints at least as aggressively as those three, and LinkedIn support
appears in this project only as a planned v1.0 feature, not a shipped driver.

On captchas specifically, since that came up as a reason to adopt it: the
documentation never mentions captchas. Its anti-detection story is one dependency,
`playwright-stealth`, and the project qualifies even that, saying it is used to
reduce bot detection but is not guaranteed. Nothing here bypasses a captcha.

The session pattern is still worth stealing. Harvest once from a real login, store it
encrypted, replay it headless, and expect to re-harvest often. That is a good design
for the LinkedIn connector regardless of which library drives the browser.

Source: [tenlifejosh/agentreach](https://github.com/tenlifejosh/agentreach)

## What does replace Playwright

Three projects genuinely compete with Playwright for agent-driven browsing, and they
all attack the same problem: a raw DOM dump costs thousands of tokens per page, and
the agent has to orchestrate every low-level click itself.

**Stagehand v4** from Browserbase. Now ships as a browser extension rather than
driving over CDP from outside, which cuts round-trip time. Browserbase's own
benchmarks claim it runs twice as fast as Playwright and uses about 80% fewer
tokens. It adds self-healing actions and iframe support, both of which matter on
Workday-style forms. Treat vendor benchmarks as vendor benchmarks.

**vercel-labs/agent-browser**. A native Rust CLI with a persistent daemon talking
straight to Chrome over CDP. Its `snapshot` command returns an accessibility tree
where every element gets a stable ref like `@e1`, costing roughly 200 to 400 tokens
against 3,000 to 5,000 for a full DOM. It exposes both an MCP server and a plain
CLI, and it explicitly supports Windows. Since the agent will have Bash, it could
drive this through shell commands with no Python binding at all.

**Geometra MCP** takes a stranger approach, replacing the render pipeline entirely
and streaming layout geometry as JSON. Its README names filling job applications as
a target use case. Younger and more experimental than the other two.

Sources: [Stagehand v4](https://www.browserbase.com/changelog/stagehand-v4),
[vercel-labs/agent-browser](https://github.com/vercel-labs/agent-browser),
[Agent-Pattern-Labs/geometra](https://github.com/Agent-Pattern-Labs/geometra)

## Prior art worth reading before we build

The search turned up several projects doing almost exactly what this one is trying
to do. Two findings from them are worth acting on.

`Aditya-00a/Instaply` has converged on nearly the same architecture as our plan:
local-first, runs on your laptop with your IP and your real Chrome profile, SQLite
audit trail, Windows Task Scheduler for the loop, deterministic rules handling about
80% of form fields with an LLM covering the rest. The notable difference is that it
refuses to auto-submit. It pauses at the captcha and at the final Submit button,
every time, by design. Given that we are planning an auto mode, their reasoning is
worth reading before we commit to ours.

`AbhishekMandapmalvi/AutoApply` runs Playwright against LinkedIn, Indeed,
Greenhouse, Lever, Workday, and Ashby, with three apply modes: `full_auto`,
`review`, and `watch`. Close to the auto and manual split we discussed, with an
extra observe-only mode we had not considered.

`anton-karlovskiy/job-agent` is built on browser-use rather than raw Playwright and
publishes accuracy numbers for form filling: 78% with browser-use's hosted model,
63.3% with the open-source one. If those numbers generalize, roughly a third of
agentic form fills go wrong, which is a strong argument for the dry-run mode and the
hold-for-review queue.

That same project's comparison table also notes that AIHawk, the most popular tool
in this space at around 30,000 stars, is archived and unmaintained, and states
plainly that LinkedIn Easy Apply bans automation.

Sources: [Instaply](https://github.com/Aditya-00a/Instaply),
[AutoApply](https://github.com/AbhishekMandapmalvi/AutoApply),
[job-agent](https://github.com/anton-karlovskiy/job-agent)

## Where this leaves the design

Three source lanes, and they carry different risk.

Discovery is settled. Apify Actors over MCP remove the most fragile code we would
otherwise own, scraping runs from Apify's IPs with no login involved, and the whole
discovery side costs a few dollars a month. The Free plan's $5 credit covers the
first weeks of testing.

Submission splits by destination:

- **ATS boards** (Greenhouse, Lever, Ashby) are public forms with no login and
  usually no captcha. Local Playwright handles these, and they are the safest place
  to let auto mode actually run.
- **LinkedIn Easy Apply** now has two viable paths. Hand your session cookies to
  `sunny_spade/linkedin-easy-apply-bot` and let Apify plus Browserbase carry the
  detection risk, or keep the session local and write the form filling ourselves.
  The first is faster to working software and puts your live session on someone
  else's infrastructure. The second is slower and keeps the credential at home.
- **Naukri** has no auto-apply Actor at all, so it is ours either way, or it stays
  in the manual queue.

Captchas are unsolved on every path. Apify avoids them with residential proxies and
clean fingerprints rather than answering them, and points at a third-party paid
service when avoidance fails. Whatever we build needs a defined behavior for the
moment a captcha appears, because it will.

Agent Reach contributes nothing to either lane. Neither project supports a job
board, one is built on Playwright rather than replacing it, and neither addresses
captchas.
