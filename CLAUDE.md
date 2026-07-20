# CLAUDE.md

A learning project for agentic app observability, tracing and evals. FastAPI →
LangChain → Claude, traced with OpenTelemetry into Jaeger.

Because it is a learning project, prefer explaining *why* over shipping fast,
and say when something is unverified rather than asserting it.

## Non-negotiables

**No manual span creation.** Every span must come from auto-instrumentation.
Adding `tracer.start_span(...)` defeats the entire purpose of the project. If
something isn't traced, fix the instrumentation setup, not the app code.

**Instrumentation ordering is load-bearing.** `setup_telemetry(app)` runs at
import time in `app/main.py`; the chain is built later in the lifespan handler.
The LangChain instrumentor patches
`langchain_core.callbacks.BaseCallbackManager.__init__`, so any chain
constructed before that call silently produces zero spans — no error, just
silence. Never move chain construction above `setup_telemetry()`.

**Only the collector publishes 4317.** Jaeger listens on 4317 too, but stays on
the compose network as `jaeger:4317`. Adding it to Jaeger's `ports:` collides
and the stack won't start.

**Never write a real API key anywhere but `.env`.** It's gitignored. Don't echo
it, don't put it in a command, don't paste it into a transcript. `example.env`
keeps `ANTHROPIC_API_KEY` empty on purpose.

**No `--reload` in the Dockerfile.** The reloader forks and the child loses the
parent's instrumentation.

## Stack choices, already settled

- **OpenLLMetry** (`opentelemetry-instrumentation-langchain`), not OpenInference.
  Chosen for standard `gen_ai.*` semantic conventions, which work against any
  OTLP backend. OpenInference's `openinference.*` namespace is Arize's dialect
  and only pays off with Phoenix. Don't switch without a reason.
- **LangChain v1** (`langchain>=1.0.0`), matching the sibling project
  `../lca-langchainV1-essentials/python/`.
- **`claude-haiku-4-5-20251001`** — cheapest current model. The only thing in
  this project that costs money is Anthropic tokens; keep it that way.
- **Jaeger v2** accepts OTLP natively. `COLLECTOR_OTLP_ENABLED` is a stale v1
  artifact — don't add it back from a tutorial.

## The fake model

`USE_FAKE_LLM=true` swaps `ChatAnthropic` for `FakeListChatModel`. Both are
`BaseChatModel`, so chain, endpoints and instrumentation are identical — only
the leaf differs. This lets the whole tracing pipeline be tested with no key
and no spend, and it should stay that way.

Known asymmetry: `FakeListChatModel` has no native `_agenerate` (it inherits
`SimpleChatModel`'s `run_in_executor` fallback), so fake mode is thread-pooled
rather than socket-async. Fine for everything except concurrency benchmarks.
`_astream` *is* native, so streaming is genuinely async in both modes.

## Verifying changes

Fake mode covers everything structural, free and without a key:

```bash
docker compose up --build -d
curl -s -X POST localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"message":"test"}'
curl -s 'localhost:16686/api/traces?service=langchain-chat&limit=5'
```

**Acceptance criterion is span nesting, not span count.** A correct trace has
`POST /chat` as root with `RunnableSequence.workflow` beneath it and the
chain steps under that. Flat, unparented spans mean FastAPI instrumentation
didn't attach — an ordering bug in `app/main.py`, never a collector config
problem.

`docker compose logs otel-collector` prints every span with full attributes via
the `debug` exporter. Check there before suspecting Jaeger.

## Streaming

`/chat/stream` uses `chain.astream()` + `StreamingResponse`. Verified
incremental: first byte at 12.5 ms, last at 94.7 ms for a 113-char reply.

ASGI emits one `http send` span per chunk, so a streamed reply produces
roughly one span per token — 122 spans versus 8 for the blocking endpoint.
`EXCLUDE_ASGI_SPANS=true` drops send/receive spans (122 → 5). Default is
false on purpose: the span flood is a lesson worth seeing once.

Don't repeat the claim that child spans outlast the parent in streaming
traces. Measured, they don't — the FastAPI server span stays open until the
body finishes writing and correctly encloses everything.

## Currently unverified

- Phase 2 (real Claude: httpx span to `api.anthropic.com`, `gen_ai.usage.*`
  token counts, populated `gen_ai.request.model`) is blocked on a
  user-supplied API key. Everything else is tested in fake mode.
