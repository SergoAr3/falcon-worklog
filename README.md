# Worklog

A small, local web app for tracking employee work hours. Add an employee, describe a task, start a timer, and stop it when the work is done. Reports show each person's sessions and total tracked time for a selected date range.

![Worklog interface](docs/preview.png)

**Stack:** Python 3.10+ (standard library), SQLite, HTML, CSS, JavaScript. No packages to install, no account needed.

## Run locally

```bash
python3 app.py
```

Open **http://127.0.0.1:8000** in your browser.

The database is created automatically at `data/tracker.sqlite3`. The records, including unfinished timers, remain available after you stop and restart the server. To choose another database or port:

```bash
python3 app.py --port 8080 --db ./worklog.sqlite3
```

You can also set `TIME_TRACKER_DB` to change the default database path.

### Try it

1. Add an employee (for example, Alex Morgan).
2. Select the employee, type a task, and start the timer.
3. Stop the timer, or leave it running and restart the server. An active session resumes from its original start time.
4. Adjust the **From** and **To** dates and click **Apply** to review entries and totals.
5. Start another session for the same employee to record a second task.

For a populated example, run this once **before** creating any employees:

```bash
python3 -m scripts.seed_demo
```

It creates three fictional employees with sample tasks. The seed script won't modify a database that already contains employees. All data in the demo and automated tests is fictional.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests cover:

- adding employees, starting and stopping sessions, and starting a new session after stopping one;
- preventing overlapping active timers for one employee, including two concurrent starts;
- rejecting a repeated stop without changing the saved record;
- restoring a running timer from the database after reopening the app;
- empty reports (including employees with zero entries);
- reports with multiple employees and work sessions spanning midnight;
- a consistent report snapshot when another connection creates an employee and starts a timer between report queries;
- date and input validation;
- HTTP requests and persistence across a real HTTP server restart.

Frontend regression tests require Node.js 24 and use its built-in test runner:

```bash
node --check static/app.js
node --test tests/test_frontend.js
```

They check responses arriving out of order, obsolete request failures, and errors from the latest request. They execute the application JavaScript with a minimal DOM test double; they are not browser end-to-end tests.

No packages or separate test framework are required. Node.js is only needed for these tests, not to run the application. Run commands from the project directory. On systems where Python 3 is named `python`, use that command instead of `python3`.

The workflow in `.github/workflows/tests.yml` runs Python tests on Python 3.10 and 3.14, plus the JavaScript checks on Node.js 24, on pushes and pull requests. Its first GitHub run can only be verified after the repository is published.

## How it works

The Python server serves a small JSON API and three static files. SQLite stores employees and time entries in two tables. Each time entry has a start timestamp and an optional end timestamp; a missing end timestamp means the timer is still running.

A partial unique index makes it impossible for an employee to have two running sessions. Timer writes use an immediate SQLite transaction so competing requests cannot silently create duplicates. The browser updates the clock every second; SQLite timestamps, rather than the browser counter, determine the recorded time.

Reports read employees and entries in one explicit SQLite read transaction. The frontend applies only the latest report request's result and ignores obsolete responses and errors.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/employees` | Employees and current active sessions |
| `POST` | `/api/employees` | Add an employee (`name`) |
| `POST` | `/api/timer/start` | Start (`employee_id`, `description`) |
| `POST` | `/api/timer/stop` | Stop (`employee_id`) |
| `GET` | `/api/report?from=YYYY-MM-DD&to=YYYY-MM-DD` | Records and totals within the date range |

Dates in reports are **UTC**, and both selected dates are included. If a session crosses the boundary of the selected period, only the portion inside that period is counted. Running sessions are counted up to the time the report is generated. Click **Apply** again to refresh the report.

This is a deliberately small local application. It has no authentication or permissions, and the server binds to `127.0.0.1` by default. It isn't intended to be exposed directly to the public internet.

## AI-assisted development

The final review and fixes were performed with **OpenAI Codex** in this conversation: inspecting the code, reproducing two report races, adding regression tests, implementing fixes, and preparing the test workflow. The exact model identifier displayed in the author's Codex session still needs confirmation before submission.

Actual requests from this conversation (excerpts):

> проанализируй текующий проект

> Добавь регрессионные тесты для обеих ошибок.

> Не меняй стек, дизайн и существующую архитектуру без необходимости. Не добавляй новые функции.

### Author clarification required before submission

- Confirm the AI tools and exact model identifiers used for the initial implementation and this final revision. The previous README claimed **ChatGPT (GPT-6)** for the initial design and code, but the available files and conversation do not establish that history.
- Supply actual initial-development prompts if available. The previous English prompt examples could not be verified as requests that were really used and have been removed.
- Confirm whether the previously listed fixes (missing zero-second sessions, unclosed SQLite connections, and an incorrect midnight fixture) happened during initial development. The current code contains the resulting behavior, but no earlier versions were available to verify those changes. They are not presented below as confirmed history.

### Issues found and corrected

- **Inconsistent report reads.** Employees and entries were read in separate SQLite snapshots. Creating an employee and starting a timer between the queries caused a `KeyError`. An explicit read transaction now keeps both queries on the same snapshot. A regression test commits real writes through a second connection between the queries.
- **Out-of-order report responses.** An older response could overwrite a newer date range, and an obsolete failure could surface after a newer success. A request counter now limits updates and errors to the latest request. Regression tests control the response order. Both report regressions were reproduced with failing tests before the fixes were applied.

The main design choices—single active session per employee, durable timestamps, and overlap-based date reports—are also covered by the automated tests.
