# Autonomous QA Agent

Points a real browser at a URL, lets a local LLM (via Ollama) read the page's
accessibility snapshot, decide what to test, and drive the browser through
Playwright to actually run those tests — then writes a report.

## How it works

1. Playwright opens the page and captures `page.aria_snapshot(mode="ai")` — a
   compact accessibility tree with `[ref=eN]` ids, instead of the raw DOM.
2. The LLM is shown that snapshot and proposes 3–8 concrete test cases.
3. For each test case, the LLM drives the page step by step using tools
   (`browser_click`, `browser_fill`, `browser_select_option`, ...), addressing
   elements by their `ref`. After every action it's shown a fresh snapshot,
   plus any new console errors, JS errors, and failed network requests, so it
   can react to what actually happened.
4. Actions that look destructive (submit, pay, delete, logout, confirm, ...,
   in English and Russian) are flagged by a keyword guardrail and require
   confirmation before running (`--auto-confirm-destructive` to skip the
   prompt; running non-interactively without that flag auto-denies them).
5. The agent stays on the page's origin — if a click navigates off-site, it's
   detected and reverted. If a click opens a new tab/window instead
   (`target="_blank"`, `window.open`, ...), that tab is inspected — its
   url/title/snapshot are captured — and then closed; testing continues on
   the original tab. This shows up as a `new_tabs` field in the tool result,
   usually the same step, occasionally the next one (event dispatch latency),
   which the agent is instructed not to mistake for the new tab never having
   opened.
6. Each test case defaults to `reset_before=True`: before it runs, the agent
   restores cookies *and* localStorage/sessionStorage to a baseline captured
   right after the initial load (or after the precondition, see below), then
   re-navigates and reloads. Just re-navigating isn't enough on its own — a
   language switch or any other client-side-persisted state stored via
   localStorage would otherwise silently survive a plain reload, since it's
   the same browser context/storage; this is what actually gets cleared.
   Uncheck `reset_before` per-case in the web UI's review panel for cases
   that are intentionally a multi-step chain — or turn on "fresh browser per
   test case" (below) for the strongest form of isolation.
7. An optional one-time **precondition** (e.g. "log in with these
   credentials") runs once before planning, driven by the same tool-calling
   loop as a real test case. Whatever state it reaches becomes the new
   "start" that every `reset_before` case returns to and the baseline that
   gets restored — so the login *does* survive resets (it's the deliberate
   baseline), while state a test case changes on top of it does not.
8. Check "fresh browser per test case" (`--fresh-browser-per-case`) for
   isolation stronger than cookie/localStorage restoration: the whole browser
   is closed and relaunched before every case, not just reset within the same
   session. Slower (a fresh browser process per case), but rules out any
   leakage reset_before doesn't reach (cache, IndexedDB, service workers,
   ...). If a precondition is set, it re-runs for every case's fresh browser
   rather than once — `reset_before` is moot in this mode, since a fresh
   browser is already a full reset regardless of the per-case setting.
9. Each step's screenshot is captured immediately after that action runs,
   not only once at the end of the case — so it shows the page as it looked
   right when something happened, not whatever the page had moved on to by
   the time the case finished (a later action, or the next case's reset).
10. A markdown + JSON report is written to `reports/<timestamp>__<url-slug>/`.

## Setup

```
pip install -r requirements.txt
```

Chrome or Edge must already be installed (the agent drives your system Chrome
via Playwright's `channel="chrome"` by default — no separate browser download
needed). Pass `--channel chromium` to use Playwright's own bundled Chromium
instead (requires `playwright install chromium`).

Ollama must be running locally with the target model pulled, e.g.:

```
ollama pull hf.co/richardyoung/Qwen3-14B-abliterated-GGUF:Q4_K_M
```

## Usage

```
python main.py --url https://example.com
```

Options:

- `--model` — Ollama model name (default: `hf.co/richardyoung/Qwen3-14B-abliterated-GGUF:Q4_K_M`)
- `--ollama-host` — default `http://localhost:11434`
- `--headed` — show the browser window (default: headless)
- `--channel` — `chrome` (default), `msedge`, or `chromium`
- `--max-cases` — max test cases to plan (default 6)
- `--max-steps` — max tool-call steps per test case (default 10)
- `--num-ctx` — Ollama context window size (default 16384; the model's own
  max is 40960 — raising this increases VRAM/RAM usage for the KV cache)
- `--auto-confirm-destructive` — auto-approve guardrail-flagged actions
- `--focus` — optional note steering what the agent should prioritize testing,
  e.g. `--focus "the checkout form and its validation"` (shapes both which
  test cases get planned and how they're executed)
- `--planner-prompt-file` / `--executor-prompt-file` — path to a text file
  overriding the built-in planner/executor system prompt. Substituted
  directly (not `str.format()`), so no need to escape literal `{`/`}`; the
  planner prompt may contain a `{max_cases}` placeholder, the executor
  prompt `{title}`/`{goal}`/`{focus_section}`.
- `--precondition` — a one-time setup goal run before planning, e.g.
  `--precondition "log in with username demo and password demo123"`. Always
  auto-confirms guardrail-flagged actions (it's a step you authored
  yourself, not the agent's own exploration) — a login button often matches
  the "submit" keyword.
- `--fresh-browser-per-case` — close and relaunch the browser before every
  test case instead of resetting within the same session (see point 8 above)
- `--output` — report output directory (default `reports/`)

## Web UI

```
python run_web.py
```

Opens a local web app at `http://127.0.0.1:8000` — pick a protocol
(`https://`/`http://`/`file://`) and enter the rest of the URL, pick the
model/browser/limits from the form, optionally add a free-text comment
telling the agent what to focus on and/or a precondition (e.g. a login),
check "Fresh browser per test case" for the strongest isolation, hit "Run",
and watch progress stream in live — per-step log (each step's own
screenshot thumbnail, click to open full-size), per-case pass/fail/blocked
cards, findings. Runs are written to the same `reports/` directory as the
CLI, so both share history; each run's folder is named
`<timestamp>__<url-without-scheme>` so it's recognizable in a file listing,
not just a bare timestamp.

"Advanced settings" (collapsed by default) lets you override the planner's
and/or executor's system prompt for that run — "Load default text" loads the
current built-in prompt into the box as a starting point to edit; leave a
box empty to use the default unchanged.

An active run can be stopped with the "Stop" button — this is *cooperative*
cancellation, checked between steps/cases (and while paused for review, see
below), not an instant kill: the in-flight LLM call or browser action
finishes first, so it can take a few seconds.

Check "Review test cases before running" to pause the run right after
planning: it stops on the page with the proposed cases shown as editable
cards — edit a case's title/goal (the goal field doubles as a free-text
comment to the agent), toggle its "reset to initial state" checkbox, remove
one with ✕, or add your own with "+ Add case". Nothing executes until you
click "Approve & run"; execution then runs exactly the list you approved,
not the original LLM proposal. A review left untouched for 30 minutes
auto-cancels the run.

Past runs are grouped in the sidebar by the resource they tested (the exact
URL), newest group first. Within each group:
- click the run to reload it from its saved `report.json`
- ▶ to re-run it with the exact same settings (new run_id, same config)
- ✕ to permanently delete its report and screenshots (blocked while a run is
  still active)
- a checkbox to select it for comparison — check exactly two runs **of the
  same resource** and click "Compare selected" to see a regression/fixed/
  changed diff (matched by case title) between them; checkboxes on other
  resources' runs grey out as soon as one is selected, since comparing runs
  of different pages wouldn't mean anything

### Test suites

"Save as suite" (in the review panel before approving, or in the results
panel after a run finishes) saves the current case list — title, goal, and
reset-before-this-test — under a name you pick. Saved suites appear in the
sidebar's "Suites" list: click the name to open it, showing its cases as
the same editable cards as the review panel — edit title/goal/reset,
remove one, add a new one with "+ Add case", then "Save changes" to update
the suite in place (same id, so its run history stays linked) — plus every
run that was ever started from it, each clickable through to its report; ▶
starts a new run using exactly those cases, **skipping LLM planning
entirely** — faster, cheaper (no planning call), and deterministic run over
run, since the same cases execute every time instead of being re-invented;
✕ deletes the suite. Suites are plain JSON files under `suites/`.

## Scope (v1)

The agent stays on the single page/origin it's given. It can interact with
anything on that page (forms, menus, tabs, same-origin links) but will not
crawl the rest of the site, and destructive-looking actions are gated behind
the guardrail above.
