"""本地假 HTTP 服务：在 127.0.0.1 的随机端口上按路由表应答 JSON，并记下每个请求。

供假 OpenRouter、假 GitHub、假上游与进程内的假网关使用；fixture 见 `conftest.py`。
"""

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

type Handler = Callable[["Request"], tuple[int, object]]


@dataclass(frozen=True)
class Request:
    """假服务收到的一个请求；请求头名一律小写，`body` 为解析后的 JSON，没有请求体时为
    None。"""

    method: str
    path: str
    headers: dict[str, str]
    body: object


@dataclass
class FakeService:
    """按 `(方法, 路径)` 应答的本地 HTTP 服务；路由匹配不看查询串。"""

    url: str
    routes: dict[tuple[str, str], Handler] = field(default_factory=dict)
    requests: list[Request] = field(default_factory=list)

    def reply(self, method: str, path: str, payload: object, status: int = 200) -> None:
        """登记一条固定应答。"""
        self.routes[(method, path)] = lambda _request: (status, payload)


def _handler_class(service: FakeService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            request = Request(
                method=method,
                path=self.path,
                headers={key.lower(): value for key, value in self.headers.items()},
                body=json.loads(raw) if raw else None,
            )
            service.requests.append(request)
            route = service.routes.get((method, urlsplit(self.path).path))
            if route is None:
                status, payload = 404, {"error": "no route"}
            else:
                status, payload = route(request)
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

    return Handler


@contextmanager
def running_service() -> Iterator[FakeService]:
    """运行一个假服务，退出上下文时关闭。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    service = FakeService(url=f"http://127.0.0.1:{server.server_address[1]}")
    server.RequestHandlerClass = _handler_class(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield service
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
