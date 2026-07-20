"""Minimal FastAPI chat app, traced end to end by auto-instrumentation only."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from langchain_core.language_models import BaseChatModel, FakeListChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

from app.telemetry import setup_telemetry

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# MODEL = "claude-haiku-4-5-20251001"
MODEL = "gemini-2.5-flash-lite"


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    response: str


def build_model() -> BaseChatModel:
    """Return the fake model or a real Claude client, per USE_FAKE_LLM.

    Both are BaseChatModel, so the chain, the endpoint and the instrumentation
    are identical either way -- only the leaf differs. That is what makes the
    fake mode a trustworthy test of the tracing pipeline rather than a test of
    a different code path.
    """
    if os.getenv("USE_FAKE_LLM", "false").lower() == "true":
        log.info("using FakeListChatModel (no API key, no network, no spend)")
        return FakeListChatModel(
            responses=[
                "Distributed tracing follows a single request across every "
                "service it touches, so you can see where the time went."
            ]
        )

    # from langchain_anthropic import ChatAnthropic
    from langchain_google_genai import ChatGoogleGenerativeAI

    # log.info("using ChatAnthropic model=%s", MODEL)
    log.info("using Gemini model=%s", MODEL)

    return ChatGoogleGenerativeAI(model=MODEL, temperature=0, max_tokens=512)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The model is built here, after setup_telemetry() has already run at import
    # time, so the instrumentor's callback-manager patch is in place.
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "You are a concise assistant."),
            ("human", "{message}"),
        ]
    )
    # The prompt template earns its place: it makes this a multi-span chain in
    # Jaeger rather than a single bare LLM call.
    app.state.chain = prompt | build_model() | StrOutputParser()
    yield


app = FastAPI(title="langchain-trace", lifespan=lifespan)

# Runs at import, before lifespan builds the chain. Ordering is load-bearing.
setup_telemetry(app)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """Blocking: waits for the full generation, returns one JSON body."""
    result = await app.state.chain.ainvoke({"message": req.message})
    return ChatResponse(response=result)


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Streaming counterpart to /chat -- same chain, same instrumentation.

    Compare the two traces in Jaeger. The span shape is the same; the timing
    is not. Time to first token is readable here as the first "http send"
    span, and has no equivalent in the blocking endpoint.

    Note ASGI emits one "http send" span per chunk, so this produces a span
    per token. See EXCLUDE_ASGI_SPANS in telemetry.py.
    """

    async def token_stream():
        # astream yields incremental chunks. StrOutputParser is stream-aware,
        # so each chunk arrives already unwrapped to a plain string.
        async for chunk in app.state.chain.astream({"message": req.message}):
            yield chunk

    # text/plain rather than SSE to keep curl output readable. A browser client
    # would normally want text/event-stream.
    return StreamingResponse(token_stream(), media_type="text/plain")
