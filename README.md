# langchain-trace

A minimal FastAPI chat app traced end to end with OpenTelemetry. One endpoint,
one LangChain chain, one Claude call — and every span in Jaeger comes from
auto-instrumentation. There is no manual span code anywhere in this repo.

```
curl → FastAPI :8000 ──┐
                       │  OTLP/gRPC
                       ↓
              OTel Collector :4317  ──OTLP/gRPC──→  Jaeger :4317 (internal)
                                                          │
                                                    Jaeger UI :16686
```

## Quick start (no API key, no cost)

```bash
cp example.env .env          # ships with USE_FAKE_LLM=true
docker compose up --build -d
docker compose ps            # all three should be Up
```

Send a request:

```bash
curl -s localhost:8000/health

curl -s -X POST localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"In one sentence, what is distributed tracing?"}'
```

There is also a streaming endpoint over the identical chain — use
`--no-buffer` or curl will hide the incremental delivery from you:

```bash
curl -s --no-buffer -X POST localhost:8000/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message":"In one sentence, what is distributed tracing?"}'
```

Then open **http://localhost:16686**, pick **`langchain-chat`** in the service
dropdown, and hit Find Traces.

You should see this shape. The nesting is the whole point — if these spans show
up flat and unparented instead, see Troubleshooting.

```
POST /chat                          (Server, root)
├─ POST /chat http receive
└─ RunnableSequence.workflow
   ├─ execute_task ChatPromptTemplate
   ├─ FakeListChatModel.chat         (Client)
   └─ execute_task StrOutputParser
```

Click the `.chat` span to see the `gen_ai.*` attributes: `gen_ai.operation.name`,
`gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`.

## The two endpoints

| | `POST /chat` | `POST /chat/stream` |
|---|---|---|
| LangChain call | `chain.ainvoke()` | `chain.astream()` |
| Response | one JSON body | incremental `text/plain` |
| Client sees | nothing until complete | first token immediately |
| Latency you can measure | total generation only | first token *and* total |

Same chain, same model, same instrumentation — only the invocation differs.
That is deliberate: run one request through each and put the traces side by
side in Jaeger.

Measured on the fake model, same prompt (times relative to trace start):

```
POST /chat                                    0.00 →   5.50 ms
└─ RunnableSequence.workflow                  0.82 →   4.82
   ├─ execute_task ChatPromptTemplate         1.32 →   2.01
   ├─ FakeListChatModel.chat                  2.52 →   3.42
   └─ execute_task StrOutputParser            3.78 →   4.66
   + 2 × http send

POST /chat/stream                             0.00 → 106.52 ms
└─ RunnableSequence.workflow                 20.10 → 106.23
   ├─ execute_task ChatPromptTemplate        21.73 →  22.48
   ├─ FakeListChatModel.chat                 23.12 →  95.78   ← spans the whole stream
   └─ execute_task StrOutputParser           24.70 → 105.96
   + 113 × http send, one per token, 25.90 → 106.45
```

**Time to first token is directly readable in the streaming trace** — the
first `http send` lands at 25.90 ms against a total of 106.52 ms. The client
saw output four times sooner than the blocking endpoint would have allowed.
The non-streaming trace has no equivalent measurement; it can only tell you
total time. On real Claude calls those two numbers diverge much further.

Note `FakeListChatModel.chat` stays open 23 → 96 ms, covering the entire
generation rather than closing early. That's the span whose duration means
"time to produce everything", while the `http send` spans show delivery.

**Streaming multiplies span count.** ASGI emits one `http send` span per chunk
written, so a 113-character reply produced 113 spans. Real traffic would flood
your backend. Set `EXCLUDE_ASGI_SPANS=true` in `.env` and recreate to drop the
send/receive spans, leaving the server span and the LangChain chain. Left off
by default — seeing the spam once is the lesson.

With `FakeListChatModel` the streaming is genuinely async; it defines
`_astream` natively. See *Async, honestly* below for where the fake model is
**not** a faithful stand-in.

## Switching on real Claude

The fake model exercises the entire tracing pipeline for free, but three things
only exist on a real call: the outbound HTTP span, real latency, and token
counts.

1. Get a key from [console.anthropic.com](https://console.anthropic.com). Your
   Claude Code subscription does not include one; API usage is billed separately.
2. Edit `.env` (gitignored — never commit it):
   ```
   USE_FAKE_LLM=false
   ANTHROPIC_API_KEY=sk-ant-...
   ```
3. `docker compose up -d --force-recreate app`
4. Re-run the curl above.

Now the trace additionally contains an **HTTPX client span for
`api.anthropic.com`** and the LLM span carries `gen_ai.usage.input_tokens` /
`gen_ai.usage.output_tokens`, with `gen_ai.request.model` populated instead of
`unknown`.

Model is `claude-haiku-4-5-20251001` — the cheapest current model. Lab traffic
costs cents, not dollars.

## Things worth trying

- **Compare fake vs. real side by side.** Run one request in each mode and diff
  the span trees in Jaeger. The only structural difference is the HTTPX span.
- **Break the nesting on purpose.** In `app/main.py`, move `setup_telemetry(app)`
  to *after* the chain is built. Spans go flat. This is the single most common
  first-time OTel failure, and seeing it deliberately is cheaper than debugging
  it later.
- **Watch the wire format.** `docker compose logs -f otel-collector` prints every
  span with full attributes, courtesy of the `debug` exporter.
- **Turn off content capture.** Uncomment `TRACELOOP_TRACE_CONTENT=false` in
  `.env` and recreate. Prompt and completion text disappears from spans — the
  switch you'd flip in production.
- **Add a second chain step** and watch a new span appear with no code changes
  to the tracing setup.
- **Diff the two endpoints.** One request to `/chat`, one to `/chat/stream`,
  then open both traces in Jaeger and compare where each span starts and ends.

## Async, honestly

The real Claude path is async the whole way down — verified, not assumed:

```
async def chat()                    FastAPI coroutine
  └─ await chain.ainvoke()          async Runnable
     └─ ChatAnthropic._agenerate()  overridden on ChatAnthropic
        └─ await self._acreate()
           └─ AsyncAnthropic        → httpx.AsyncClient (real socket I/O)
```

The event loop is free during the API call, so one worker can hold many Claude
calls in flight.

**`FakeListChatModel` is not fully async, and this is the one place the fake
misleads.** It doesn't define `_agenerate`, so it inherits `SimpleChatModel`'s,
which is `await run_in_executor(None, self._generate, ...)` — a thread-pool
hop. The event loop isn't blocked and concurrency still works, but each request
burns a pool thread instead of parking on a socket.

It doesn't matter for correctness or for tracing. It matters if you
**load-test concurrency in fake mode**: you'd hit the default executor ceiling
(`min(32, cpu+4)` threads) for reasons that have nothing to do with your app.
Benchmark against the real model.

Streaming is the exception — `FakeListChatModel` *does* define `_astream`
natively, so `/chat/stream` is a genuine async generator in both modes.

Useful check when adding any new component: does the class define its own
`_agenerate`, or inherit the `run_in_executor` fallback? That one question
tells you whether it's truly async or quietly thread-pooled.

Span export is deliberately **not** async — `BatchSpanProcessor` batches and
ships on a background thread, so exporting never adds to request latency.

## Layout

| Path | What it does |
|---|---|
| `app/main.py` | FastAPI app, `/chat` + `/chat/stream` + `/health`, `build_model()` toggle |
| `app/telemetry.py` | All OTel wiring — tracer provider, exporter, instrumentors |
| `otel-collector-config.yaml` | OTLP receiver → batch → Jaeger + debug exporters |
| `docker-compose.yml` | The three services |
| `example.env` | Template; copy to `.env` |

## How the tracing actually works

`opentelemetry-instrumentation-langchain` (OpenLLMetry) patches
`langchain_core.callbacks.BaseCallbackManager.__init__`. Because every LangChain
package shares `langchain-core`, that single patch traces chains, models and
tools without touching your code.

**Ordering is load-bearing.** `setup_telemetry()` runs at import in
`app/main.py`; the chain is built later in the lifespan handler. A chain built
before instrumentation silently produces no spans.

Three instrumentors matter alongside it:

- **FastAPI** — creates the root server span. Without it LangChain spans have no
  parent and Jaeger shows no waterfall.
- **HTTPX** — the outbound call to Anthropic, so network time is separable from
  LangChain overhead.
- **Logging** — injects `trace_id` into log lines so a log can be traced back.

Attributes follow the OpenTelemetry **GenAI semantic conventions** (`gen_ai.*`),
which is why this works against any OTLP backend — Grafana, Datadog, Honeycomb —
not just Jaeger.

## Troubleshooting

**Jaeger service dropdown is empty.** Check the Collector first:
`docker compose logs otel-collector | grep -c "Span #"`. Spans there but not in
Jaeger means the `jaeger:4317` link; no spans means the app isn't exporting.

**Spans are flat instead of nested.** FastAPI instrumentation didn't attach.
Check the ordering in `app/main.py`, not the Collector config.

**Stack won't start, port 4317 in use.** Only the Collector publishes 4317.
Jaeger listens on it too, but on the compose network only — don't add it to
Jaeger's `ports`.

**`gen_ai.request.model` says `unknown`.** You're in fake mode. Expected.

**Traces vanished after restart.** Jaeger stores in memory here. Intentional.

**`/chat/stream` looks like it returns all at once.** Add `--no-buffer` to
curl. Without it curl buffers the body and hides the incremental delivery.

**Hundreds of `http send` spans in a streaming trace.** One per chunk written,
by design of the ASGI instrumentation. Set `EXCLUDE_ASGI_SPANS=true` in `.env`.

## Shutting down

```bash
docker compose down
```
