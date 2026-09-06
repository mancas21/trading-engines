"""
Minimal Health/Metrics Server for Quant Alert Engine
Run in background thread via start_health_server().
"""
import threading
import time
from contextlib import asynccontextmanager
from typing import Dict, Any, Callable

try:
    from fastapi import FastAPI, Response
    from pydantic import BaseModel
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False
    FastAPI = None  # type: ignore
    Response = None  # type: ignore
    BaseModel = object  # type: ignore

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False
    psutil = None  # type: ignore

import logging
logger = logging.getLogger(__name__)

_start_time = time.time()
_health_checks: Dict[str, Callable] = {}
_server_thread: Any = None


if _FASTAPI_AVAILABLE:
    class HealthResponse(BaseModel):  # type: ignore
        status: str
        timestamp: float
        uptime_seconds: float
        checks: Dict[str, Any]

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore
        yield

    app = FastAPI(title="Quant Alert Engine Health", lifespan=lifespan)  # type: ignore

    @app.get("/health", response_model=HealthResponse)  # type: ignore
    async def health():
        results: Dict[str, Any] = {}
        overall = "healthy"
        for name, check_fn in _health_checks.items():
            try:
                result = check_fn()
                if isinstance(result, dict):
                    results[name] = result
                    if result.get("status") == "unhealthy":
                        overall = "unhealthy"
                    elif result.get("status") == "degraded" and overall == "healthy":
                        overall = "degraded"
                else:
                    results[name] = {"status": "healthy" if result else "unhealthy"}
                    if not result and overall == "healthy":
                        overall = "unhealthy"
            except Exception as e:
                results[name] = {"status": "unhealthy", "error": str(e)}
                overall = "unhealthy"
        return HealthResponse(
            status=overall,
            timestamp=time.time(),
            uptime_seconds=time.time() - _start_time,
            checks=results,
        )

    @app.get("/metrics")  # type: ignore
    async def metrics():
        lines = [
            f'quant_engine_uptime_seconds {time.time() - _start_time}',
        ]
        if _PSUTIL_AVAILABLE:
            try:
                lines.append(f'quant_engine_memory_bytes {psutil.Process().memory_info().rss}')
                lines.append(f'quant_engine_cpu_percent {psutil.Process().cpu_percent()}')
            except Exception:
                pass
        for name, check_fn in _health_checks.items():
            try:
                result = check_fn()
                if isinstance(result, dict) and "metrics" in result:
                    for k, v in result["metrics"].items():
                        safe = str(k).replace(" ", "_").replace("-", "_")
                        lines.append(f'quant_engine_{name}_{safe} {v}')
            except Exception:
                pass
        return Response(content="\n".join(lines) + "\n", media_type="text/plain")  # type: ignore
else:
    app = None  # type: ignore


def register_check(name: str, check_fn: Callable):
    """Register a health check function. Returns True/False or dict."""
    _health_checks[name] = check_fn
    logger.debug("Registered health check: %s", name)


def start_health_server(host: str = "127.0.0.1", port: int = 8080):
    """Start health server in background daemon thread. No-op if FastAPI missing.
    Default host is 127.0.0.1 (localhost only); set to 0.0.0.0 via config to expose LAN (fix #7).
    """
    global _server_thread
    if not _FASTAPI_AVAILABLE:
        logger.warning("FastAPI not installed — health server disabled")
        return None
    if _server_thread is not None and _server_thread.is_alive():
        logger.debug("Health server already running")
        return _server_thread
    # Validate host: only localhost or explicit LAN bind
    if host not in ("127.0.0.1", "localhost", "0.0.0.0"):
        logger.warning(f"Health server host '{host}' is non-standard; prefer 127.0.0.1 or 0.0.0.0")
    try:
        import uvicorn
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        _server_thread = threading.Thread(target=server.run, daemon=True, name="health-server")
        _server_thread.start()
        logger.info("Health server started on http://%s:%s/health", host, port)
        return _server_thread
    except Exception as e:
        logger.error("Failed to start health server: %s", e, exc_info=True)
        return None
