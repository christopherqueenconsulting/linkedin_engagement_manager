# Test Suite Documentation

This directory contains the comprehensive test suite for the LinkedIn Engagement Manager.

## Directory Structure

```
tests/
├── conftest.py              # Shared fixtures and pytest configuration
├── unit/                    # Unit tests with mocked dependencies
│   ├── app/                # Tests for application modules
│   ├── utilities/          # Tests for utility modules
│   │   ├── ai/            # AI helper tests
│   │   └── linkedin/      # LinkedIn integration tests
├── integration/            # Integration tests (real MySQL + Redis)
└── fixtures/              # Test data and fixtures
```

**There are TWO lanes, not three.** What happened to the browser lane is
[below](#there-is-no-e2e-lane-issue-1215).

## Test Categories

### Unit Tests (`tests/unit/`)

Fast, isolated tests that verify individual functions or classes with all external dependencies mocked.

**Characteristics:**
- Run in milliseconds
- No external dependencies (database, APIs, browser)
- All dependencies mocked
- High code coverage focus

**Example:**
```python
def test_format_year():
    """Test formatting year strings."""
    assert format_year("2020") == "20"
    assert format_year("2024") == "24"
```

### Integration Tests (`tests/integration/`)

Tests that verify multiple components work together correctly.

**Characteristics:**
- May require database, Redis, or other services
- Test interactions between modules
- Slower than unit tests
- Focus on data flow and component integration

**Example:**
```python
@pytest.mark.integration
def test_engagement_workflow():
    """Test complete engagement workflow."""
    # Tests multiple components working together
    pass
```

### There is no e2e lane (issue #1215)

`tests/e2e/` was deleted, along with `e2e-coverage.yml` and the `e2e` Codecov flag. **A change that
needs a real browser is graded by the read-only live probe, not by a CI lane** — run
`scripts/linkedin_live_validation.py` (the **linkedin-live-validation** skill; `docs/sdui-probe-coverage.md`).

**Why, with the measurement.** The lane claimed to be the browser tier and never was. Its last run
before deletion was **6 passed, 11 skipped, no browser**:

- The 6 that passed were pure `unittest.mock` tests — no browser, no database, no server. Every
  behaviour they asserted was **already covered in `tests/unit/`**: the carousel/video publish paths
  in `test_run_automation_posting.py`, `_is_local_image_path` in `utilities/linkedin/test_poster.py`,
  `API_URL_FINAL` in `utilities/test_env_constants_url.py`, orphan re-queueing in
  `app/test_run_scheduler.py`. Nothing was lost by deleting them.
- 5 skipped on `REPLICATE_API_TOKEN` / `CAPSOLVER_API_KEY`, which are not repo secrets and never
  have been, so those tests had **never executed in CI**. The one genuine browser test among them
  (the Playwright avatar page) had also gone stale: it authenticated by writing a `lem_session` key
  into `localStorage`, and sessions have been httpOnly cookies since #745/#914.
- 6 skipped because they need MySQL and the workflow declared **no service container**. Those were
  the only tests in the lane worth keeping, and they now live — and actually run — in
  `tests/integration/test_post_publish_workflow_db.py`.
- The whole job was `continue-on-error: true`, so none of that was ever visible as a failure. It
  still installed Chromium and booted a FastAPI server on every PR to run mocks.

Rebuilding it as ~15 genuinely browser-driven tests was the alternative, and was rejected: the thing
worth driving a browser against is LinkedIn, whose SDUI markup changes without notice. A CI browser
can only be pointed at a fixture of that markup, which grades our selectors against a copy that goes
stale precisely when the real page moves — while the live probe already grades the real page and
files one issue per drift.

**One thing the old lane's absence had been hiding**: nothing in CI ever imported the probe's
production symbols, so three restructure slices broke its lazy imports without anything going red.
`tests/unit/test_live_validation_lazy_imports.py` now resolves every deferred `cqc_lem` import in
that script, statically, on every PR.

## One workflow per lane — do not add a "run everything" workflow

Each lane has exactly one CI workflow, and that workflow owns its Codecov flag:

| Lane | Workflow | Check name | Codecov flag |
|---|---|---|---|
| unit | `unit-tests.yml` | `Unit Tests (Python 3.12)` — **required** | `unit` |
| integration | `integration-coverage.yml` | `Integration Tests` — **required** | `integration` |

Those two flags are the *only* ones `codecov.yml` declares, and both checks are required by branch
protection. `codecov.yml`'s `after_n_builds` counts **uploads, not lanes** — the unit lane is
sharded across two jobs that both upload under `unit`, so it is 3.

Neither the count nor the flag list is maintained by hand any more:
`tests/unit/test_codecov_upload_contract.py` derives both from the workflows that actually upload
and fails the build on a drift in either direction. That guard exists because the drift is invisible
where it matters — an `after_n_builds` set too high makes Codecov post **no status at all** rather
than a red one, and a flag left declared after its lane is gone carries that lane's last report
forward into the project number forever.

### The project number is unit + integration, and it clears the floor (#1340)

`#1340` reported the project number as inflated ~8 points by import-time coverage from the old e2e
lane: that lane collected coverage on the *pytest* process while driving a separate, uninstrumented
`uvicorn` over HTTP, so it credited every module its imports touched and none of the handlers it
called. Dropping its upload from a PR read as **94.98% → 86.86%**, which is what blocked `#1338`.

Deleting the lane outright (`#1215`) did not reproduce that drop. Measured on `main` at `6a145efa`
(2026-08-14), from these two lanes only:

| | measured | #1340 predicted without e2e |
|---|---|---|
| project | **95.06%** | 86.86% |
| FastAPI Endpoints | 94.98% | 72.51% |
| Celery Tasks | 92.61% | 86.58% |
| Utilities | 96.47% | 90.97% |
| LinkedIn Automation | 94.62% | 89.74% |

Per flag: `unit` 93.50%, `integration` 32.62%, whose union is that 95.06%; the `e2e` flag reports
**zero sessions and zero lines**, so nothing is being carried forward. The 86.86% was a property of
that PR's upload set, not of the code — so the old enforced 90% floor was a ratchet ~5 points
*below* what the two lanes earn. `#1340` had chosen to re-baseline it *down* to ~87%; that
measurement is why it was not.

`#1488` spent that headroom in the other direction instead:

| status | floor | measured baseline it was set against |
|---|---|---|
| `codecov/project` (**enforced**, 1% threshold) | **93%** | project 95.06% |
| per-component (`informational`) | **90%** | lowest component, Celery Tasks, 92.61% |

So a PR may lose ~3 points of project coverage before the enforced status goes red — enough to
absorb run-to-run flake and a restructure slice moving a module between packages, tight enough that
a genuine regression surfaces in the PR that caused it. Both floors are ratchets: raise them as the
baseline rises, never lower them. `tests/unit/test_codecov_upload_contract.py` asserts both — plus
the **effective** project floor, `target - threshold` = 92%, since widening the threshold lowers the
floor exactly as much as cutting the target does — so walking any of them back means moving the
assertion in the same commit and writing the reason into `codecov.yml`.

A general `Test Suite` workflow used to run alongside them, invoking pytest **three times** in one
job — `tests/unit`, then `tests/integration`, then `pytest tests/` with coverage, which re-ran both.
So every PR ran the unit lane 3× and the integration lane 3×, for ~13.7 min of the ~35 min of CI,
while gating nothing: it was not a required context and had no `merge_group` trigger, so it took no
part in queue validation.

Two things made it actively misleading rather than merely wasteful. It declared **no service
containers**, so its integration step had no MySQL or Redis and could never pass — which is why the
step carried `continue-on-error: true`. And it uploaded coverage under a flag `codecov.yml` does not
declare, so a fourth report merged into the project number from a run that excluded
`requires_database`, `requires_openai`, `requires_selenium` and `slow`.

If you want the whole suite in one command, run it locally. In CI, add tests to the lane that owns
them.

### The `slow` marker, and the one job that runs it

Every lane above selects with `-m "not slow"`, so until `slow-tests.yml` existed the 13
`slow`-marked tests ran **nowhere**. They are live probes against third-party services — Pexels,
Perplexity, and the Ollama/LiteLLM video path — which is exactly why a PR should not depend on
them, and exactly why something still has to.

`Nightly / Slow Tests` (`slow-tests.yml`, 03:00 UTC + `workflow_dispatch`) is that something. It has
no `pull_request` or `push` trigger, so it cannot become a gate.

**A live probe skips on a bad credential; it does not fail.** A missing key skips, and so does a
present-but-unauthorized one — a nightly that goes red for someone else's expired token is a nightly
people stop reading. `test_pexels_video.py` already worked that way; `test_perplexity.py` checked
only for *presence*, so a 401 failed the run, which surfaced the moment these tests were finally
given a schedule.

## Test Markers

Tests use markers to categorize and filter execution:

- `@pytest.mark.unit` - Unit tests
- `@pytest.mark.integration` - Integration tests
- `@pytest.mark.slow` - Slow-running tests
- `@pytest.mark.requires_openai` - Requires OpenAI API access
- `@pytest.mark.requires_database` - Requires database connection
- `@pytest.mark.requires_selenium` - Requires browser automation

## Running Tests

### Basic Test Execution

```bash
# Run all tests
poetry run pytest

# Run with verbose output
poetry run pytest -v

# Run specific test file
poetry run pytest tests/unit/utilities/test_db.py

# Run specific test
poetry run pytest tests/unit/utilities/test_db.py::TestDatabaseOperations::test_update_db_post_status
```

### Running by Category

```bash
# Run only unit tests
poetry run pytest tests/unit -v

# Run only integration tests (needs MySQL + Redis)
poetry run pytest tests/integration -v
```

### Running by Marker

```bash
# Run only unit tests (using marker)
poetry run pytest -m "unit" -v

# Run tests excluding slow tests
poetry run pytest -m "not slow" -v

# Run tests that don't require external services
poetry run pytest -m "not (requires_openai or requires_database or requires_selenium)" -v
```

### Coverage Reporting

```bash
# Generate coverage report
poetry run pytest --cov=src/cqc_lem --cov-report=html --cov-report=term

# View HTML coverage report
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

### Test Selection

```bash
# Run tests matching pattern
poetry run pytest -k "test_database" -v

# Run tests in specific module
poetry run pytest tests/unit/utilities/ai/ -v

# Stop on first failure
poetry run pytest -x

# Run failed tests from last run
poetry run pytest --lf
```

## Unit-Suite Performance

The unit lane is the merge-queue feedback loop, so it is kept deliberately fast. Issue #480 profiled
it and cut the CI-shaped run (`-m "not slow" --cov`) from **~49s to ~20s** with byte-identical
coverage. Two things keep it there:

### 1. The unit lane is hermetic (`tests/unit/conftest.py`)

Autouse guards make un-mocked external I/O fail *instantly* instead of dialling out:

| Guard | What it does | Why |
|---|---|---|
| `_no_real_llm_calls` | Raises `APIConnectionError` from the shared `ai.client.client` | A few tests never mocked the client. Each one spent ~1.3s in httpx connect + the OpenAI SDK's retry back-off before the production `except` branch ran. Four carousel tests alone cost 12.8s. |
| `_no_real_redis` | Makes `redis.Redis.from_url` raise, so `_redis_client()` returns `None` | CI has no Redis in the unit lane, so those call sites already took the fails-open branch. On a dev box running the compose stack the same tests would connect to the live broker and read/write real 429-breaker keys. |
| `_no_real_mysql` | Makes `mysql.connector.connect` **and `mysql.connector.pooling.connect`** raise `InterfaceError`, the `mysql.connector.Error` every reader already handles | A test that forgot to stub a DB collaborator opened a real connector call — 372 of them did: in CI that raised `TypeError: int() ... not 'NoneType'` (unset `MYSQL_PORT`), which is *not* a `mysql.connector.Error`, so it escaped every `except` and was captured as a production defect (#1496). On a dev box it reached the compose stack's live database. `pooling.py` holds its own `connect` binding, so guarding only `mysql.connector.connect` leaves `MySQLConnectionPool.add_connection()` dialling out for real (#555) — both names, or neither. |

Every guard reproduces the behaviour the tests already had in CI — they only remove the waiting and
the dev-box non-determinism. A test that wants a *working* LLM/Redis/MySQL handle patches it itself;
that patch nests inside the guard and wins.

**When adding tests:** mock the collaborator explicitly. If a test only passes because a guard failed
the call for it, that is accidental coverage — assert the failure path on purpose instead.

### 2. Coverage uses the `sysmon` core

`[tool.coverage.run] core = "sysmon"` in `pyproject.toml` selects coverage.py's `sys.monitoring`
tracer (CPython 3.12+, which this project requires). Measured on the unit suite: 33s with the default
C tracer vs 19s with sysmon, identical line-coverage totals.

### Known inherently-slow tests (leave them alone)

These are the slowest survivors. Each is slow because it does real work the test is specifically
there to verify — do not mock it away:

| Test | ~Cost | Why it stays |
|---|---|---|
| `test_date.py::test_purge_removes_unparseable_dates` | 1.0s | First unparseable input makes `dateparser` load its full language/locale set. One-time process cost, not per-test. |
| `test_admin_engagement_test_runs.py` setup | 0.9s | Builds the whole FastAPI app + OpenAPI schema. Already `scope="module"`, so it is paid once per file. |
| `test_geocoding.py::TestGeocodeCity` (3 tests) | 1.3s | Real `timezonefinder` lat/long→IANA lookups; the assertions are on real timezone output. |
| `test_carousel_image_selection.py::TestPillowComposition` | 1.5s | Real Pillow slide composition; the assertions inspect the rendered pixels. |

### Parallelism (`pytest-xdist`) — now on for this lane too

Both the unit and the **integration** lane run `-n 4 --dist loadfile`.

The unit lane used to be serial, on an issue-#480 benchmark showing `-n 4` at ~19s against ~20s
serial — worker startup and the coverage combine ate the gain. **That measurement was taken when
the suite was ~20s and 4,000-odd tests.** It is now 11,728 tests, and re-benchmarked at 11,731
tests on an 8-core box:

| invocation | wall clock |
|---|---|
| serial, `--cov` (what CI used to run) | 139s |
| `-n 4 --dist loadfile --cov` | **56s** |

The lesson is that the old finding was correct and expired, so keep the numbers with the claim:
the crossover is suite size, and a 20s suite really does lose to worker startup.

`-n 4` is pinned rather than `auto` because that is the width of a GitHub-hosted runner; the 56s
above had spare cores, so budget 70-90s in CI. **Do not raise it above 4.**

`--dist loadfile` is not interchangeable with the other modes here. It keeps a whole file on one
worker, which the module-scoped fixtures under `tests/unit/api/` depend on — `load` scatters
individual tests and rebuilds those fixtures per test, and `loadscope` groups methods by *class*,
so a module-scoped fixture used by two classes gets built twice on two workers. (The 40+
module-scoped `TestClient` fixtures that used to be the sharpest case are gone since #1214; the
shared `api_client` is function-scoped and costs ~1.7ms, so it no longer cares which mode runs.)

**Sharding is also a latent-order-dependency detector.** Anything that mutates global state without
restoring it stops being invisible, because "whatever ran before this test" is no longer one fixed
order. Two were found and fixed when this landed, both in the same shape — a test seeding or
consuming the global `random` generator, which decided whether a *production* `random.sample` in
`blog_source._from_sitemap` visited an unreadable page first, and therefore whether the skip branch
was covered at all. If a test's coverage or outcome moves under `-n 4`, suspect unrestored global
state before suspecting xdist.

### Re-profiling

```bash
# Rank the slowest tests (and slowest setup/teardown)
poetry run pytest tests/unit -m "not slow" --durations=50

# cProfile a single offender
poetry run pytest tests/unit/path/to/test_x.py::TestY::test_z --profile
```

## Integration-Suite Performance

The six required contexts run in parallel, so the slowest of them sets the wall clock for every
merge. That was this lane: 353.7s of pytest inside a 438s job. Issue #1185 took it apart.

### 1. Most of it was sleeping, not testing

Four tests were spending **311s of the 354s**, and none of them was doing work. Each fell through to
an LLM call the test never mocked, and there is no LiteLLM proxy on the runner — so every one paid
the production connect-retry schedule, which deliberately rides out ~24s of refused connections so a
proxy restart cannot lose a generation (issue #986).

Issue #1185 made that cheap by setting `LLM_CONNECT_RETRY_ATTEMPTS: 1` for the lane. **Issue #1188
made the calls stop**, which is a different thing: the three that were real are mocked
(`get_or_create_profile_synthesis` + `optimize_post_hook` in `test_content_plan`, the shared client
singleton in `test_carousel_creation`, which reaches the model a second time through
`carousel_creator.derive_image_query`), and the fourth turned out never to have made an LLM call at
all — its 30s is `celery_once` taking a real Redis lock for an unpatched `apply_async`, which is
§1c below (issue #1197).

Measured on an 8-core box against a dead LiteLLM port, which reproduces CI's numbers to within 1%
(fresh Redis before each run — a leaked `celery_once` lock survives into the next one and moves the
number by 30s):

| Configuration | Before #1188 | After |
|---|---|---|
| `-n 4 --dist loadfile --cov`, `LLM_CONNECT_RETRY_ATTEMPTS=1` — what CI runs | 53.0s | 50.6s |
| …the same with the retry env var removed | 185.1s | 45.7s |
| Serial, `LLM_CONNECT_RETRY_ATTEMPTS=1` | 72.7s | 58.7s |

The parallel wall clock barely moves because `--dist loadfile` was already hiding the cost behind
the longest file; the work removed shows up in the serial column and in the row where the retry
env var is gone. That row is also the answer to "is the env var still load-bearing" — it was worth
132s, and it is now worth nothing. It stays only as a backstop for fixture setup, which runs outside
the function-scoped guard below.

### 1b. An unmocked LLM call FAILS this lane (`_no_live_llm_calls`)

`tests/integration/conftest.py` patches `OpenAI.post` — the one method every endpoint (chat,
embeddings, images, speech) funnels through, on the base class, so `AttributedOpenAI` and the raw
`openai.OpenAI()` fallback in `ai_helper` are both covered — to refuse with `pytest.fail`.

It refuses with pytest's `Failed`, a **BaseException**, rather than the `APIConnectionError` the
unit lane's guard raises. LEM's LLM helpers are full of `except Exception` fallbacks, which is
exactly how an unmocked call sits in this lane for months looking like a passing test: the call
fails, the production fallback branch runs, the test goes green, and the only symptom is a slow
job. `Failed` is not catchable by any of them, so the test fails where the call is made and the
traceback names the helper that reached the network.

**When you add a test here, mock the LLM** — patch the `client` the module under test imported
(`mock_openai_client` is the house fixture) or the helper that wraps it. A test that legitimately
constructs a client and never calls it is untouched; a test that wants a *failing* call should mock
the failure it means to assert on rather than borrow this guard's.

### 1c. A contended celery-once lock FAILS this lane (`_no_contended_once_lock`)

The fourth test in §1 never called a model. `QueueOnce.apply_async` takes its dedup lock on the
**producer** side, and LEM configures that lock `blocking=True, blocking_timeout=30`
(`my_celery.py`). In production a worker picks the message up and releases it. Nothing in this lane
does, so the lock outlives the test that took it — and both tests in `test_link_first_comment.py`
publish post 10, i.e. the same `(user_id, post_id)` key for `auto_second_wave_comment`. The second
one slept the full thirty seconds (297 × `time.sleep(0.1)` inside `redis/lock.py`), got
`AlreadyQueued`, and — every LEM task sets `graceful: True` — dropped the dispatch silently. A green
test that took 30s and queued nothing.

Issue #1197 patched that dispatch (neither test asserts anything about the second wave) and added
the guard: `tests/integration/conftest.py` wraps `celery_once.backends.redis.Redis.raise_or_lock`
and `pytest.fail`s when the key is **already held**.

It fires on the contended acquire, not on the first one. A single uncontended lock costs ~3ms and
breaks nothing, so refusing that would be a rule people delete; blocking on a lock in a lane with no
consumer is never anything but a bug. Asking Redis whether the key is there is what makes the
refusal *immediate* rather than thirty seconds late, and it leaves no timing threshold to go flaky
on a loaded runner. The traceback names the `apply_async` call site, which is the whole question.

**When you add a test here, patch the task dispatch you are not asserting on** — `patch("<the
module whose globals the caller reads>.<task>")`, the same seam the sibling dispatches in that file
already use.

Measured on an 8-core box, dedicated MySQL + Redis, dead LiteLLM port, empty `.env`, **fresh Redis
before every run**:

| Configuration | Before #1197 | After |
|---|---|---|
| `-n 4 --dist loadfile --cov`, `LLM_CONNECT_RETRY_ATTEMPTS=1` — what CI runs | 51.3s | **25.6s** |
| Serial, `LLM_CONNECT_RETRY_ATTEMPTS=1` | 56.9s | **26.7s** |
| `tests/integration/test_link_first_comment.py` alone | 31.1s | **1.0s** |

Unlike §1, the parallel column moves by the whole cost: `--dist loadfile` had nothing longer left to
hide it behind.

**CI is the number that counts, and it is not the same box.** Unlike §1 this local rig did not
reproduce the runner to within 1% — the `Integration Tests` job measured **60.9s and 60.6s** on the
two `main` runs either side of this change, against 51.3s here. On the runner it went **60.9s →
33.4s**, same `311 passed, 1 skipped`. The saving is the same ~27s either way; only the baseline
differs, so take a before *and* an after on whichever box you use and never compare across two.

Instrumenting `raise_or_lock` across the whole lane found **two** real acquires, both from that one
file on that one key, and none anywhere else — so this was the only collision, and after the patch
the lane takes no celery-once lock at all. The shape to watch for is two tests in **one file**,
because `--dist loadfile` guarantees they share a worker; two files cannot collide today, since §2
gives each worker its own Redis logical database and the lock follows it there (verified: gw1's lock
landed in db1, not db0).

**The leak is the measurement trap.** A lock nothing released survives into the *next* run and moves
the number by 30s, so before/after timings taken against the same Redis are noise. Since #1197 that
shows up as this guard failing on the first dispatch instead — flush the test Redis and re-run.

### 2. Each worker owns a database (`tests/integration/conftest.py`)

The DB-touching tests key their rows on a per-file email constant and delete either side of the
test. That works while one process owns the server and breaks immediately when it does not — run
`test_comment_outcomes_db.py` at `-n 4 --dist load` without the fixture and 5 of its 11 tests fail.

So each xdist worker clones the migrated schema into `linkedin_manager_gw<N>` and points
`platform.db.connection.MYSQL_DATABASE` at it for the session, dropping it at the end. **A serial
run is untouched** — no `PYTEST_XDIST_WORKER`, no clone, same database as before.

It is a database rather than a per-test transaction because nothing here routes through one
connection: `db_cursor` opens its own per call and commits explicitly, and committing is what
several of these tests exist to prove (`ON DELETE CASCADE`, the UNIQUE single-use claim, the pool's
session reset). A rollback fixture would have to disable the behaviour under test.

Two consequences worth knowing:

- **Server-global state is still shared.** A per-worker database isolates rows, not the server. That
  is why `test_db_pooling.py` counts its connections off `information_schema.processlist` scoped to
  its own schema instead of the server-wide `Threads_connected`, which the other workers move.
- **The test user needs rights on the sibling databases.** CI grants
  `` `linkedin\_manager\_%` `` after migrating; without it the fixture fails loudly rather than
  quietly sharing one database, because a silent fallback under `-n 4` is the flaky suite it exists
  to prevent.

```bash
# The lane, the way CI runs it
poetry run pytest tests/integration -n 4 --dist loadfile
```

## Fixtures

Shared fixtures are defined in `conftest.py`:

### Environment Setup

- `setup_test_environment` - Sets environment variables for tests (auto-use)

### API Fixtures

- `api_client` — the ONE `TestClient` over `cqc_lem.api.main.app`, in both the unit and the
  integration lane (#1214). It patches nothing and it is function-scoped, so `app.dependency_overrides`
  composes with it and is restored afterwards. **Never construct a `TestClient` in a test module.**
  A test that needs extra state wraps this fixture in its own
  (`tests/integration/test_api_credential_gate.py::gated_client` is the shape), and a test that
  needs a dispatch or a DB read stubbed patches it where the handler READS it, for the length of
  its own request. `tests/unit/api/test_api_client_fixture.py` fails the build on a new ad-hoc
  construction and on a module-scope patch that leaks a mock into the API layer.

### Mock Fixtures

- `mock_openai_client` - Mocked OpenAI API client
- `mock_database_connection` - Mocked database connection
- `mock_selenium_driver` - Mocked Selenium WebDriver

### Sample Data Fixtures

- `sample_linkedin_profile` - Sample LinkedIn profile data
- `sample_post_data` - Sample post data
- `sample_message_data` - Sample message data

### Using Fixtures

```python
def test_with_fixture(sample_linkedin_profile):
    """Test using a fixture."""
    profile = LinkedInProfile(**sample_linkedin_profile)
    assert profile.full_name == "John Doe"
```

## Writing New Tests

### Test File Naming

- Use `test_*.py` pattern for test files
- Match source file names: `utilities/db.py` → `test_db.py`
- Place in the lane that owns it: `tests/unit/` (mock all I/O) or `tests/integration/` (real MySQL + Redis)

### Test Function Naming

- Use `test_*` pattern for test functions
- Be descriptive: `test_generate_ai_response_with_valid_input`
- Avoid generic names like `test_function`

### Test Structure

Follow the Arrange-Act-Assert pattern:

```python
def test_example():
    # Arrange: Set up test data
    input_data = {"key": "value"}
    
    # Act: Execute function
    result = function_to_test(input_data)
    
    # Assert: Verify results
    assert result == expected_value
```

### Test Documentation

Always include docstrings:

```python
def test_convert_datetime_to_local_tz():
    """Test converting UTC datetime to local timezone.
    
    This test verifies that UTC datetimes are correctly
    converted to the local system timezone.
    """
    # Test implementation
```

### Mocking Guidelines

Use mocks for external dependencies:

```python
from unittest.mock import patch, MagicMock

def test_with_mock():
    """Test with mocked external dependency."""
    with patch("module.external_call") as mock_call:
        mock_call.return_value = "mocked response"
        result = function_to_test()
        assert mock_call.called
```

## Test Coverage Goals

### Current Coverage

Run `poetry run pytest --cov=src/cqc_lem --cov-report=term` to see current coverage.

### Coverage Targets

- **Minimum**: 70% for core modules
- **Target**: 85%+ for core modules
- **Critical modules**: 90%+ (db.py, ai_helper.py, scrapper.py)

### Improving Coverage

1. Identify low-coverage modules:
   ```bash
   poetry run pytest --cov=src/cqc_lem --cov-report=html
   open htmlcov/index.html
   ```

2. Add tests for uncovered lines
3. Focus on critical paths first
4. Add edge case tests
5. Verify coverage improvement

## Continuous Integration

Tests run automatically via GitHub Actions on:
- Push to `main`, `develop`, or `copilot/**` branches
- All pull requests to `main` or `develop`

CI workflow includes:
- Unit test execution
- Integration test execution (when applicable)
- Code coverage reporting
- Linting checks

## Test Maintenance

### Regular Tasks

- Keep tests updated with code changes
- Remove obsolete tests
- Refactor duplicate test code into fixtures
- Update test documentation
- Monitor and improve coverage

### When Tests Fail

1. Don't ignore failing tests
2. Investigate the root cause
3. Fix the test or the code
4. Don't skip tests to make CI pass
5. Update tests if requirements changed

## Best Practices

### DO

✅ Write tests before implementation (TDD)
✅ Keep tests independent and isolated
✅ Use descriptive test names
✅ Mock external dependencies
✅ Test edge cases and error conditions
✅ Keep tests simple and focused
✅ Use fixtures for common setup
✅ Document complex test scenarios

### DON'T

❌ Write tests that depend on other tests
❌ Use real external services in unit tests
❌ Skip or ignore failing tests
❌ Write tests without assertions
❌ Test implementation details
❌ Create overly complex test setup
❌ Forget to clean up test data

## Troubleshooting

### Common Issues

**Import Errors**
- Ensure `PYTHONPATH` includes `src`
- Check `pyproject.toml` configuration
- Verify virtual environment is activated

**Missing Dependencies**
```bash
poetry install --with test
```

**Environment Variables**
- Check `.env` file exists
- Verify required variables in `conftest.py`
- Use `setup_test_environment` fixture

**Slow Tests**
- Mark slow tests with `@pytest.mark.slow`
- Run quick tests: `pytest -m "not slow"`
- Use mocks instead of real services
- See [Unit-Suite Performance](#unit-suite-performance) for the profiling workflow and the
  already-triaged offenders

## Resources

- [pytest documentation](https://docs.pytest.org/)
- [pytest-cov documentation](https://pytest-cov.readthedocs.io/)
- [Contributing Guidelines](../CONTRIBUTING.md)

## Questions?

If you have questions about testing:
1. Check this documentation
2. Review existing tests for examples
3. Ask in pull request or issue comments
