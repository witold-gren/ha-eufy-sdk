"""Talking through a camera's speaker: the browser's PCM, streamed on to /talk."""
# ruff: noqa: D101, D102, D107, INP001, S101, PLR2004

from __future__ import annotations

import asyncio
import json
import unittest

from aiohttp import ClientSession, ClientWebSocketResponse, web
from aiohttp.test_utils import TestServer

from custom_components.eufy_sdk.talk import (
    DEFAULT_RATE,
    bridge_outcome,
    relay_talk,
    talk_rate,
)


class FakeBridge:
    """A bridge `/talk/<sn>` that keeps what it gets and answers a chosen status."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.received = bytearray()
        self.queries: list[dict] = []

    async def talk(self, request: web.Request) -> web.Response:
        self.queries.append(dict(request.query))
        if self.status != 200:
            return web.Response(status=self.status)
        async for chunk in request.content.iter_any():
            self.received.extend(chunk)
        return web.json_response({"ok": True})


class StatefulBridge:
    """A bridge that says at once that the camera listens, then the outcome last."""

    def __init__(self, last: dict | None = None) -> None:
        self.last = last or {"ok": True, "seconds": 1.0}
        self.received = bytearray()
        self.connected_sent = asyncio.Event()

    async def talk(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"content-type": "application/x-ndjson"})
        await response.prepare(request)
        await response.write(b'{"state":"connected"}\n')
        self.connected_sent.set()
        async for chunk in request.content.iter_any():
            self.received.extend(chunk)
        await response.write(json.dumps(self.last).encode() + b"\n")
        await response.write_eof()
        return response


async def messages_until_outcome(ws: ClientWebSocketResponse) -> list[dict]:
    """Read the page's messages up to the outcome, the one with `ok`."""
    seen = []
    while True:
        message = json.loads((await ws.receive()).data)
        seen.append(message)
        if "ok" in message:
            return seen


Bridge = FakeBridge | StatefulBridge


class TalkRelayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session = ClientSession()

    async def asyncTearDown(self) -> None:
        await self.session.close()

    async def start(self, bridge: Bridge) -> tuple[TestServer, TestServer]:
        bridge_app = web.Application()
        bridge_app.router.add_post("/talk/{sn}", bridge.talk)
        bridge_server = TestServer(bridge_app)
        await bridge_server.start_server()
        url = str(bridge_server.make_url("/talk/T8410?format=s16le&rate=48000"))

        async def handler(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            outcome = await relay_talk(ws, self.session, url)
            if not ws.closed:
                await ws.send_str(json.dumps(outcome))
                await ws.close()
            return ws

        app = web.Application()
        app.router.add_get("/talk", handler)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        self.addAsyncCleanup(bridge_server.close)
        return server, bridge_server

    async def test_the_samples_reach_the_bridge_and_stop_ends_the_talk(self) -> None:
        bridge = FakeBridge()
        server, _ = await self.start(bridge)
        async with self.session.ws_connect(server.make_url("/talk")) as ws:
            await ws.send_bytes(b"\x01\x00" * 100)
            await ws.send_bytes(b"\x02\x00" * 50)
            await ws.send_str("stop")
            messages = await messages_until_outcome(ws)
        # An older bridge says nothing on the way: "connecting", then the outcome.
        assert messages == [{"state": "connecting"}, {"ok": True}]
        assert bytes(bridge.received) == b"\x01\x00" * 100 + b"\x02\x00" * 50
        assert bridge.queries == [{"format": "s16le", "rate": "48000"}]

    async def test_closing_the_socket_ends_the_talk_too(self) -> None:
        bridge = FakeBridge()
        server, _ = await self.start(bridge)
        async with self.session.ws_connect(server.make_url("/talk")) as ws:
            await ws.send_bytes(b"\x03\x00" * 10)
        for _ in range(50):
            if bridge.received:
                break
            await __import__("asyncio").sleep(0.02)
        assert bytes(bridge.received) == b"\x03\x00" * 10

    async def test_a_busy_camera_says_so(self) -> None:
        bridge = FakeBridge(status=409)
        server, _ = await self.start(bridge)
        async with self.session.ws_connect(server.make_url("/talk")) as ws:
            await ws.send_bytes(b"\x00\x00")
            await __import__("asyncio").sleep(0.1)
            await ws.send_bytes(b"\x00\x00")
            await ws.send_str("stop")
            messages = await messages_until_outcome(ws)
        assert messages[-1] == {"ok": False, "error": "busy", "status": 409}

    async def test_the_page_hears_that_the_camera_listens_while_it_still_talks(
        self,
    ) -> None:
        bridge = StatefulBridge()
        server, _ = await self.start(bridge)
        async with self.session.ws_connect(server.make_url("/talk")) as ws:
            assert json.loads((await ws.receive()).data) == {"state": "connecting"}
            await ws.send_bytes(b"\x01\x00" * 10)
            # "connected" arrives before the page stops talking.
            connected = json.loads((await asyncio.wait_for(ws.receive(), 5)).data)
            assert connected == {"state": "connected"}
            await ws.send_bytes(b"\x02\x00" * 10)
            await ws.send_str("stop")
            messages = await messages_until_outcome(ws)
        assert messages == [{"ok": True}]
        assert bytes(bridge.received) == b"\x01\x00" * 10 + b"\x02\x00" * 10

    async def test_an_end_after_the_start_comes_in_the_last_line(self) -> None:
        bridge = StatefulBridge(
            last={
                "error": "the camera ended the talkback",
                "status": 503,
                "seconds": 2.0,
            }
        )
        server, _ = await self.start(bridge)
        async with self.session.ws_connect(server.make_url("/talk")) as ws:
            await ws.send_bytes(b"\x01\x00")
            await ws.send_str("stop")
            messages = await messages_until_outcome(ws)
        assert messages[-1] == {"ok": False, "error": "closed", "status": 503}


class BridgeOutcomeTest(unittest.TestCase):
    def test_the_last_answer_of_the_bridge_reads_as_the_outcome(self) -> None:
        assert bridge_outcome({"ok": True, "seconds": 3}, 200) == {"ok": True}
        assert bridge_outcome({}, 200) == {"ok": True}
        assert bridge_outcome({"error": "x", "status": 499}, 200) == {
            "ok": False,
            "error": "failed",
            "status": 499,
        }
        assert bridge_outcome({"error": "busy"}, 409) == {
            "ok": False,
            "error": "busy",
            "status": 409,
        }


class TalkRateTest(unittest.TestCase):
    def test_the_rate_stays_within_what_a_microphone_gives(self) -> None:
        assert talk_rate("16000") == 16_000
        assert talk_rate("48000") == 48_000
        assert talk_rate("1") == DEFAULT_RATE
        assert talk_rate("lots") == DEFAULT_RATE
        assert talk_rate(None) == DEFAULT_RATE
