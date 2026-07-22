"""aiohttp control surface — same shape as laserx3.

Serves the single-page UI and a websocket that (a) broadcasts engine state +
a preview frame ~20 Hz, and (b) receives control messages:

    {"type":"set", "key":"lfo_slow.rate", "value":0.08}
    {"type":"generate", "keyword":"aurora over a still lake"}
    {"type":"scene_load", "name":"water_flowing"}
    {"type":"scene_save", "name":"my mood"}
    {"type":"scene_delete", "name":"..."}
    {"type":"trigger"} / {"type":"release"}
"""

from __future__ import annotations

import asyncio
import json
import os

from aiohttp import web, WSMsgType

_STATIC = os.path.join(os.path.dirname(__file__), "static")


def make_app(engine) -> web.Application:
    app = web.Application()
    app["engine"] = engine
    app["clients"] = set()

    async def index(request):
        resp = web.FileResponse(os.path.join(_STATIC, "index.html"))
        # The whole UI lives in this one HTML file and changes across sessions
        # during development; without this, a browser tab left open (or even
        # just reopened) can silently keep serving an old cached copy —
        # looking exactly like "features went missing" even though the server
        # is fully up to date. Force a fresh fetch every load.
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        request.app["clients"].add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    reply = await _handle(engine, json.loads(msg.data))
                    if reply is not None:
                        await ws.send_str(json.dumps(reply))
        finally:
            request.app["clients"].discard(ws)
        return ws

    async def broadcaster(app):
        try:
            while True:
                if app["clients"]:
                    try:
                        payload = json.dumps({
                            "type": "state",
                            "state": engine.state(),
                            "preview": engine.preview(),
                        })
                    except Exception as e:
                        # Never let a bad tick (e.g. a non-JSON-serialisable
                        # value slipping into state()) kill this task. Before
                        # this guard, an exception here escaped the while loop
                        # entirely and ended the broadcaster permanently and
                        # silently — the UI would show "linked" (the websocket
                        # itself is fine) but never receive another update for
                        # the rest of the session, with the error only surfacing
                        # in the terminal on process exit.
                        print(f"[laserflow] broadcaster: skipped a bad state "
                              f"tick ({e}); continuing")
                        await asyncio.sleep(0.05)
                        continue
                    for ws in list(app["clients"]):
                        try:
                            await ws.send_str(payload)
                        except Exception:
                            app["clients"].discard(ws)
                await asyncio.sleep(0.05)   # ~20 Hz
        except asyncio.CancelledError:
            pass

    async def on_start(app):
        app["broadcast_task"] = asyncio.create_task(broadcaster(app))

    async def on_cleanup(app):
        app["broadcast_task"].cancel()

    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static/", _STATIC)
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    return app


async def _handle(engine, m: dict):
    t = m.get("type")
    loop = asyncio.get_event_loop()
    if t == "set":
        engine.set_param(m["key"], m["value"])
    elif t == "set_active":
        engine.set_active(bool(m.get("value")))
    elif t == "blank":
        engine.blank()
    elif t == "generate":
        # run the (possibly networked) director off the event loop, then ack so
        # the UI can restore the Generate button from its throbber state
        await loop.run_in_executor(None, engine.generate_scene,
                                   m["keyword"], m.get("name"), m.get("audio"))
        return {"type": "generate_result", "ok": True,
                "source": engine.director.last_source,
                "error": engine.director.last_error}
    elif t == "set_audio":
        engine.set_audio_param(m["key"], m["value"])
    elif t == "apply_audio":
        await loop.run_in_executor(None, engine.apply_audio_to_scene,
                                   m["scene"], m.get("audio", ""))
        return {"type": "generate_result", "ok": True, "action": "apply_audio",
                "source": engine.director.last_source,
                "error": engine.director.last_error}
    elif t == "audio_config":
        engine.configure_audio(device=m.get("device"), blocksize=m.get("blocksize"),
                               latency=m.get("latency"))
        await asyncio.sleep(0.3)   # let the enqueued reconfigure (and its size ladder) land
        diag = engine.synth.diagnostics() if getattr(engine.synth, "online", False) else None
        return {"type": "audio_config_result",
                "ok": bool(getattr(engine.synth, "online", False)),
                "applied": engine._audio_cfg,
                "requested_blocksize": diag.get("requested_blocksize") if diag else None,
                "error": diag.get("error") if diag else engine.audio_error}
    elif t == "rescan_audio_devices":
        await loop.run_in_executor(None, engine.rescan_audio_devices)
    elif t == "set_model":
        engine.set_model(m.get("value", "haiku"))
    elif t == "set_effort":
        engine.set_effort(m.get("value", "med"))
    elif t == "scene_update":
        engine.update_current_scene()
    elif t == "scene_load":
        engine.load_scene(m["name"])
    elif t == "scene_save":
        engine.save_scene(m["name"])
    elif t == "scene_delete":
        engine.scenes.delete(m["name"])
    elif t == "trigger":
        engine.trigger()
    elif t == "release":
        engine.release()
    elif t == "set_api_key":
        engine.director.set_api_key(m.get("key", ""))
        return {"type": "api_result", "action": "save", "ok": engine.director.online,
                "detail": "key saved" if engine.director.online else "key saved (package missing?)"}
    elif t == "test_api_key":
        # a real network call — keep it off the event loop
        result = await loop.run_in_executor(None, engine.director.test)
        return {"type": "api_result", "action": "test", **result}
    return None


def run(engine, host="0.0.0.0", port=8080):
    web.run_app(make_app(engine), host=host, port=port, print=None)
