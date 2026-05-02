# Shipping `loguru-betterstack` in one evening with Claude Code

A real-time write-up of how this package went from "I have a gap in my logging stack" to a public v0.1.1 release on GitHub in about five hours, using Claude Code as a working partner. Concrete prompts, concrete decisions, no glamour.

I run a Polymarket copy-trading bot on a Hetzner VPS. It's been live since late April, mirroring a watched wallet's trades into mine with bankroll caps and slippage controls. The whole thing is async Python on top of [Loguru](https://github.com/Delgan/loguru) — `logger.add(...)` for stdout + file, `logger.bind(wallet=...)` for per-wallet context, exceptions caught with `logger.exception` so tracebacks ship structured.

That works locally. What I wanted was hosted log search, so I can pull up "every BUY in the last 6h where slippage > 5%" without SSH-ing into the box and grepping. [Better Stack](https://betterstack.com/logs) Logtail looked right — HTTP ingestion, fast search, free tier. The catch: their official Python SDK targets the standard library `logging` module. Loguru records have a richer shape (bound context, file/line/process metadata, exceptions formatted by default), and bridging through `logging.Handler` flattens all of that. So: write a native Loguru sink for Better Stack. Small enough to ship tonight, public on GitHub, MIT.

I set a few self-imposed bounds before I started. Single dependency, Loguru itself — no `httpx`, no `aiohttp`. The package has to be cheap to drop into any bot. Non-blocking, because the host's `logger.info(...)` cannot wait on the network and observability that crashes the host is worse than no observability. Real tests, not `unittest.mock.patch("urllib.request.urlopen")` — stand up a real HTTP server on localhost and inspect what arrives. Source budget around 250 LOC; if it grew past that, the design was wrong. Public release the same evening — tag, GitHub release, install URL works.

I worked with Claude Code in my terminal. The pattern was straightforward: I'd describe the thing in product terms, Claude would draft, I'd push back on the parts that didn't match my taste, we'd iterate. A few moments worth flagging.

The thread + queue design landed first try. My opening prompt was something like *"Write a Loguru sink that ships records to Better Stack's HTTP ingestion endpoint. Non-blocking from the caller's perspective. Batching by count and by time. Drop on backpressure rather than block. Give me close() and atexit registration."* Claude's first draft used a `queue.Queue` with `maxsize`, a single background daemon thread, and a flush loop that wakes on either `queue.get(timeout=...)` or a wall-clock deadline. That's exactly the pattern I'd have written from scratch. Kept verbatim.

The interesting part is what I rejected from that draft. It had a `requests` import; I asked for `urllib.request` instead, because zero deps was a hard rule. It rewrote without complaining. It had `logger.exception` calls inside the worker thread; I asked for plain `print(..., file=sys.stderr)` instead — the sink is *for* Loguru, so having it call back into Loguru on its own failures is a reentrancy footgun. It had retries with exponential backoff; I asked it to drop them for v0.1.0, because a retry policy worth shipping is a separate conversation, and the first version is allowed to be honest about that. AI gets the architecture right when the prompt is concrete, but the *what to leave out* call is mine.

The first test draft used `unittest.mock.patch` to swap `urllib.request.urlopen`. That kind of test passes locally and tells you almost nothing. I asked Claude to instead spin up a tiny `http.server.HTTPServer` on a random port in a fixture, point the sink at it via the `host` parameter, and assert on what arrived. One revision later, the fixture was clean:

```python
@pytest.fixture
def http_server():
    _CapturingHandler.received = []
    _CapturingHandler.headers_seen = []
    server = HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
```

This caught a real bug immediately. My first `host`-parsing logic used `host.lstrip("https://")` — a stripper, not a prefix remover. `lstrip` takes a *character set*, so it ate any leading `h`/`t`/`p`/`s`/`:` rather than the prefix. The mock test would have happily passed. The real-server test threw an SSL error because the URL the sink built was malformed. Fixed `host` parsing, tests went green, kept the bug story in the commit history.

The Python 3.9 vs. modern type hints story is another one worth telling. CI ran four matrix jobs (3.9, 3.10, 3.11, 3.12) and all four failed. The fix was non-obvious. I'd written:

```python
class _CapturingHandler(BaseHTTPRequestHandler):
    received: list[dict] = []
    headers_seen: list[dict] = []
```

`from __future__ import annotations` was at the top of the file, so I'd expected `list[dict]` to be deferred. But that pragma doesn't help with class-attribute *defaults* — those aren't annotations, they're real expressions evaluated at class-body time. On 3.9, `list[dict]` raises `TypeError: 'type' object is not subscriptable`. I described the symptom; Claude diagnosed it in two turns and proposed two fixes (`typing.List` for compatibility, or just drop the type annotation since it's a class attribute default). I went with the second, because ruff's UP rules then complain about `typing.List`. Sometimes the right answer is "don't annotate this".

By the time I was happy with v0.1.1, the branch had six commits — the v0.1.0 release, the v0.1.1 changes, then a chain of CI debugging commits as I wrestled with a separate billing-locked-Actions-account issue. Public history I didn't want to ship. I asked Claude to walk me through a soft-reset-and-squash that I could verify before pushing:

```bash
git reset --soft <v0.1.0-sha>     # collapse all post-release commits
git add -A
git commit -m "feat: v0.1.1 — ..."  # one clean commit
git tag -d v0.1.1
git tag -a v0.1.1 -m "..."         # re-tag at new HEAD
git push --force origin main
git push --force origin v0.1.1
```

Force-push is destructive; I wanted Claude to flag that explicitly before I ran it, not bury it. It did.

There were three honest spots where AI did less well. Maintenance judgment was the first. The "drop typing.List vs. fight ruff UP rules" call was mine — Claude proposed both with no preference. Calls like that need someone who knows the codebase's lint config and where the team draws the "fight the linter or move on" line. AI doesn't have that taste yet; you either supply it or end up with code that satisfies the machines and confuses the humans.

The second was CI billing diagnosis. GitHub Actions kept failing with no log output. I burned about twenty minutes pushing CI debug commits — verbose output, explicit `pip list`, etc. — before I actually opened the Actions UI in the browser and saw "The job was not started because your account is locked due to a billing issue." Claude can't see GitHub's account billing screen, and the API endpoints I'd given it returned 404 for the log blobs. Diagnostic work that requires platform-level UI access is still a human job.

The third was deployment friction. Wiring the package into the actual bot on the VPS — refactoring `main.py` and `copy_mirror.py` to share a `logging_config.py` module, env-gating the `BetterStackSink` behind `BETTERSTACK_SOURCE_TOKEN`, scp + restart — went smoothly with Claude. But the *decision* of "should this be a shared module or duplicated config" was mine. Same for "do I want the sink active by default or only when the env var is set". Defaults are policy decisions, not implementation decisions.

For the numbers: about five hours of wall clock end to end, including the bot integration, two GitHub releases, README + CHANGELOG + this writeup. Around 250 LOC in `handler.py` and 200 in tests. Two releases tagged on real commits. Six tests passing locally on Python 3.11. CI scaffold present but set to manual-trigger until I unblock GitHub Actions billing. One external dependency — Loguru — and stdlib `urllib.request` for the actual HTTP work.

Shipping speed isn't about velocity for its own sake. It's about *time to closed loop* — between recognizing a real gap (Loguru users want Better Stack ingestion, the official SDK doesn't fit) and having a public artifact someone else can install. Five hours to a v0.1.1 release with tests, README, CHANGELOG, and a working bot integration is the loop running fast.

The role of AI in that loop: take the architecture from prompt to draft in minutes instead of hours, run lint and tests in tight feedback, catch syntax-level bugs immediately. The role of the human: scope decisions, taste calls, when to ship vs. when to keep iterating, when to walk away from a debugging rabbit hole. I'm not selling AI as magic. I'm selling the loop — prompt, ship, verify, push, public artifact at the end. That's how I work.

— Cristian · [github.com/coffeenjoyer17](https://github.com/coffeenjoyer17) · [mirrorpoly.xyz](https://mirrorpoly.xyz)
