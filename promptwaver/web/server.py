"""aiohttp control surface — same shape as laserx3.

Serves the single-page UI and a websocket that (a) broadcasts engine state +
a preview frame ~20 Hz, and (b) receives control messages:

    {"type":"set", "key":"lfo_slow.rate", "value":0.08}
    {"type":"generate", "keyword":"aurora over a still lake"}
    {"type":"scene_load", "name":"water_flowing"}
    {"type":"scene_save", "name":"my mood"}
    {"type":"scene_delete", "name":"..."}
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
    app["clients"] = {}   # ws -> {"hq": bool}

    def _no_cache(resp):
        # The whole UI lives in one HTML file and changes across sessions
        # during development; without this, a browser tab left open (or even
        # just reopened) can silently keep serving an old cached copy —
        # looking exactly like "features went missing" even though the server
        # is fully up to date. Force a fresh fetch every load.
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp

    async def index(request):
        return _no_cache(web.FileResponse(os.path.join(_STATIC, "index.html")))

    async def output_page(request):
        # A bare, chrome-less fullscreen canvas — meant to be opened as its
        # own window and dragged onto a projector or second screen. No
        # controls, no laser/hardware dependency: it's just another websocket
        # client watching the same state/preview broadcast the control UI
        # does, so it works identically whether or not --laser is enabled.
        return _no_cache(web.FileResponse(os.path.join(_STATIC, "output.html")))

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        # ?hq=1 (the standalone output window) asks for a much less thinned
        # preview than the small in-page control-UI canvas needs — it's the
        # actual thing being watched, not just a status glance.
        request.app["clients"][ws] = {"hq": request.query.get("hq") == "1"}
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    reply = await _handle(engine, json.loads(msg.data))
                    if reply is not None:
                        await ws.send_str(json.dumps(reply))
        finally:
            request.app["clients"].pop(ws, None)
        return ws

    async def broadcaster(app):
        try:
            while True:
                if app["clients"]:
                    try:
                        state = engine.state()
                        preview_std = engine.preview()
                    except Exception as e:
                        # Never let a bad tick (e.g. a non-JSON-serialisable
                        # value slipping into state()) kill this task. Before
                        # this guard, an exception here escaped the while loop
                        # entirely and ended the broadcaster permanently and
                        # silently — the UI would show "linked" (the websocket
                        # itself is fine) but never receive another update for
                        # the rest of the session, with the error only surfacing
                        # in the terminal on process exit.
                        print(f"[promptwaver] broadcaster: skipped a bad state "
                              f"tick ({e}); continuing")
                        await asyncio.sleep(0.05)
                        continue
                    payload_std = json.dumps({"type": "state", "state": state, "preview": preview_std})
                    payload_hq = None   # built lazily, only if an hq client is actually connected
                    for ws, meta in list(app["clients"].items()):
                        try:
                            if meta.get("hq"):
                                if payload_hq is None:
                                    preview_hq = engine.preview(max_points=6000, stroke_thin=300)
                                    payload_hq = json.dumps({"type": "state", "state": state, "preview": preview_hq})
                                await ws.send_str(payload_hq)
                            else:
                                await ws.send_str(payload_std)
                        except Exception:
                            app["clients"].pop(ws, None)
                await asyncio.sleep(0.05)   # ~20 Hz
        except asyncio.CancelledError:
            pass

    async def on_start(app):
        app["broadcast_task"] = asyncio.create_task(broadcaster(app))

    async def on_cleanup(app):
        app["broadcast_task"].cancel()

    app.router.add_get("/", index)
    app.router.add_get("/output", output_page)
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
    elif t == "set_laser":
        engine.set_laser(bool(m.get("value")))
    elif t == "set_diagnostics":
        engine.set_diagnostics(bool(m.get("value")))
    elif t == "set_keystone":
        engine.set_keystone(h=m.get("h"), v=m.get("v"))
    elif t == "set_test_pattern":
        engine.set_test_pattern(bool(m.get("value")))
    elif t == "set_audio_disabled":
        fade = float(m.get("fade", 2.0))
        (engine.disable_audio if m.get("value") else engine.enable_audio)(fade=fade)
    elif t == "set_visuals_disabled":
        fade = float(m.get("fade", 2.0))
        (engine.disable_visuals if m.get("value") else engine.enable_visuals)(fade=fade)
    elif t == "generate":
        # run the (possibly networked) director off the event loop, then ack so
        # the UI can restore the Generate button from its throbber state
        await loop.run_in_executor(None, engine.generate_scene,
                                   m["keyword"], m.get("name"), m.get("audio"),
                                   m.get("size", "small"), m.get("warmth"),
                                   m.get("energy"), m.get("evolution"))
        return {"type": "generate_result", "ok": True,
                "source": engine.director.last_source,
                "error": engine.director.last_error}
    elif t == "set_audio":
        engine.set_audio_param(m["key"], m["value"])
    elif t == "apply_audio":
        await loop.run_in_executor(None, engine.apply_audio_to_scene,
                                   m["scene"], m.get("audio", ""), m.get("warmth"),
                                   m.get("energy"), m.get("evolution"))
        return {"type": "generate_result", "ok": True, "action": "apply_audio",
                "source": engine.director.last_source,
                "error": engine.director.last_error}
    elif t == "audio_config":
        done = engine.configure_audio(device=m.get("device"), blocksize=m.get("blocksize"),
                                      latency=m.get("latency"))
        # Wait for the actual reconfigure to finish (generous cap — a stream
        # stop/restart is normally well under a second) rather than guessing
        # a fixed delay; see configure_audio's docstring for the false-negative
        # this used to produce when a reconfigure ran long.
        await loop.run_in_executor(None, done.wait, 3.0)
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
        engine.update_current_scene(camera=m.get("camera", True), soundscape=m.get("soundscape", True))
    elif t == "scene_load":
        engine.load_scene(m["name"])
    elif t == "scene_save":
        engine.save_scene(m["name"])
    elif t == "scene_delete":
        engine.scenes.delete(m["name"])
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
