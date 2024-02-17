import http
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs

import better_exceptions
import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from ulid import ULID

# Note: keep this *before* the call to get_logger
filtered = logging.DEBUG if os.getenv("STRUCTLOG_DEBUG") else logging.INFO
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(filtered))

logger = structlog.get_logger()

# So we *also* get nice tracebacks of un-handled exceptions, not just
# when using logger.exception
better_exceptions.hook()


@asynccontextmanager
async def lifespan(app: FastAPI) -> None:
    # Remove uvicorn access logs to avoid duplicated logs
    # Note: this means other stuff from uvicorn gets logged
    # with a different format, but we don't really care
    access_log = logging.root.manager.loggerDict.get("uvicorn.access")
    if access_log:
        access_log.disabled = True  # type: ignore[union-attr]
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
def index(crash: bool = False) -> str:
    # Here we simulate a long process that does a loop and several steps
    # It crashes if `crash` is set in the query string
    logger.info("Getting index")
    try:
        for arg in "abc":
            do_stuff(arg, crash=crash)
    except Exception as e:
        logger.exception(e)
        raise HTTPException(status_code=500, detail="Internal could not process stuff")
    return "index"


def do_stuff(arg: str, *, crash: bool) -> None:
    structlog.contextvars.bind_contextvars(arg=arg)
    step_one()
    step_two(crash=crash)
    step_three()


def step_one() -> None:
    logger.info("Step %d", 1)


def step_two(crash: bool = False) -> None:
    if crash:
        raise Exception("Kaboom")
    logger.info("Step %d", 2)


def step_three() -> None:
    logger.info("Step% d", 3)


@app.get("/health")
def health() -> str:
    return "ok"


@app.middleware("http")
async def log_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    # Note: we replicate functionnality from uvicorn logging implementation, but
    #   - we log the /health route with a 'debug' level, so it's skipped by default
    #   - we log the request right away, before processing it
    #      note : the request id can also come from a http header if a load balancer is used
    #   - we add the request_id to structlog's context variables
    #     to each subsequent logs
    #   - we log the response as a separate event
    request_id = str(ULID())
    structlog.contextvars.clear_contextvars()

    structlog.contextvars.bind_contextvars(request_id=request_id)

    client = get_client_addr(request)
    method = request.method
    path = request.url.path
    query_string = parse_query_string(request)
    is_health = path == "/health"

    request_event = f"<- {client} {method} {path} {query_string or ''}"

    if is_health:
        logger.debug(request_event)
    else:
        logger.info(request_event)

    response = await call_next(request)

    status_code = response.status_code
    status_phrase = get_status_phrase(status_code)
    response_event = f"-> {client} {status_code} {status_phrase}"
    if is_health and status_code == 200:
        logging.debug(response_event)
    elif status_code < 400:
        logger.info(response_event)
    else:
        logger.error(response_event)

    return response


def get_client_addr(request: Request) -> str:
    scope = request.scope
    client = scope.get("client")
    if not client:
        return ""
    id, addr = client
    return f"{id}:{addr}"


def parse_query_string(request: Request) -> dict[str, Any]:
    string = request.scope.get("query_string", "").decode()
    return parse_qs(string)


def get_status_phrase(status_code: int) -> str:
    try:
        return http.HTTPStatus(status_code).phrase
    except ValueError:
        return ""
