"""aiohttp control surface — same shape as laserx3.

Serves the single-page UI and a websocket that (a) broadcasts engine state +
a preview frame ~20 Hz, and (b) receives control messages:

    {"type":"set", "key":"lfo_slow.rate", "value":0.08}
    {"type":"generate", "keyword":"aurora over a still lake"}
    {"type":"scene_load", "name":"water_flowing"}
    {"type":"scene_save", "name":"my mood"}
    {"type":"scene_delete", "name":"..."}
    {"type":"midi_learn", "key":"voice#0.level"}   (next CC binds; resend to cancel)
    {"type":"midi_unmap", "key":"voice#0.level"}
    {"type":"midi_mode", "key":"...", "mode":"catch"|"absolute"|"relative"}
    {"type":"midi_port", "name":"..."}             (empty name disconnects)
    {"type":"midi_pin"}                            (freeze slots into the scene)
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from aiohttp import web, WSMsgType

_STATIC = os.path.join(os.path.dirname(__file__), "static")
# promptwaver/web/server.py -> the repo root, where about.md and settings.json live
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_app(engine) -> web.Application:
    app = web.Application()
    app["engine"] = engine
    app["clients"] = {}   # ws -> {"hq": bool}

    _NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate",
                 "Pragma": "no-cache"}

    def _no_cache(resp):
        # The UI changes across sessions during development; without this, a
        # browser tab left open (or even just reopened) can silently keep
        # serving an old cached copy — looking exactly like "features went
        # missing" even though the server is fully up to date. Force a fresh
        # fetch every load.
        resp.headers.update(_NO_CACHE)
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

    async def kiosk_page(request):
        # The public-installation surface: one button, a microphone, and the
        # visitor's world. Always served — when the kiosk toggle is off the
        # page renders a "kiosk mode is off" state over a live canvas, so it
        # doubles as a second output window rather than 404ing.
        return _no_cache(web.FileResponse(os.path.join(_STATIC, "kiosk.html")))

    async def kiosk_settings_page(request):
        # OPERATOR page, not a visitor one — it tunes what the kiosk asks
        # Claude for. Kept off the main control UI because it is a separate
        # job done once at install time, and off /kiosk because a visitor must
        # never reach it.
        return _no_cache(web.FileResponse(os.path.join(_STATIC, "kiosk-settings.html")))

    async def about(request):
        """Serve about.md as text for the About modal to render.

        Read per request rather than cached at startup so editing the file
        shows up on the next open without a restart — it's a document, not a
        hot path, and it's opened by hand a handful of times a session.
        """
        path = os.path.join(_PROJECT_ROOT, "about.md")
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            text = ("# About\n\nCreate `about.md` in the project root and it "
                    "will be shown here.")
        except Exception as e:
            text = f"# About\n\nCould not read about.md: {e}"
        return _no_cache(web.json_response({"text": text}))

    async def welcome(request):
        """Serve welcome.md as text for the Welcome modal to render."""
        path = os.path.join(_PROJECT_ROOT, "welcome.md")
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            text = ("# Welcome to PromptWaver\n\nCreate `welcome.md` in the project root "
                    "and it will be shown here.")
        except Exception as e:
            text = f"# Welcome\n\nCould not read welcome.md: {e}"
        return _no_cache(web.json_response({"text": text}))

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        # ?hq=1 (the standalone output window) asks for a much less thinned
        # preview than the small in-page control-UI canvas needs — it's the
        # actual thing being watched, not just a status glance.
        # `local` decides whether this socket may send operator commands while
        # kiosk mode is armed — see _handle. Recorded at connect time because
        # the request (and so the peer address) isn't available later.
        peer = request.transport.get_extra_info("peername") if request.transport else None
        host = peer[0] if peer else ""
        request.app["clients"][ws] = {
            "hq": request.query.get("hq") == "1",
            "local": host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"),
        }
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    reply = await _handle(engine, json.loads(msg.data), request.app, ws,
                                          request.app["clients"].get(ws))
                    if reply is not None:
                        await ws.send_str(json.dumps(reply))
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            request.app["clients"].pop(ws, None)
        return ws

    async def broadcaster(app):
        # Target period, not a post-work pause. Sleeping a flat 0.05 *after*
        # the work made the real period `work + 50ms`, so 20Hz was
        # unreachable by construction: ~23ms of state/preview/serialise on a
        # dense scene put the output window at ~13Hz. Sleeping the remainder
        # of the budget instead holds a true 20Hz whenever the work fits, and
        # degrades to "as fast as the work allows" when it doesn't.
        period = 0.05
        try:
            while True:
                tick_start = time.monotonic()
                if app["clients"]:
                    try:
                        state = engine.state()
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
                        await asyncio.sleep(period)
                        continue
                    # BOTH payloads are built lazily, and each costs real time
                    # on a dense scene (the hq preview alone is the single
                    # most expensive step in this loop). Building the std one
                    # unconditionally spent that on a payload nobody read
                    # whenever the projector window was the only client.
                    payload_std = None
                    payload_hq = None
                    for ws, meta in list(app["clients"].items()):
                        try:
                            if meta.get("hq"):
                                if payload_hq is None:
                                    preview_hq = engine.preview(max_points=3000, stroke_thin=150)
                                    payload_hq = json.dumps({"type": "state", "state": state, "preview": preview_hq})
                                await ws.send_str(payload_hq)
                            else:
                                if payload_std is None:
                                    preview_std = engine.preview()
                                    payload_std = json.dumps({"type": "state", "state": state, "preview": preview_std})
                                await ws.send_str(payload_std)
                        except Exception:
                            app["clients"].pop(ws, None)
                # Sleep only the unused remainder of the budget. asyncio.sleep(0)
                # still yields to the event loop, so an over-budget tick can't
                # starve the websocket handlers.
                await asyncio.sleep(max(0.0, period - (time.monotonic() - tick_start)))
        except asyncio.CancelledError:
            pass

    async def on_start(app):
        app["broadcast_task"] = asyncio.create_task(broadcaster(app))

    async def on_cleanup(app):
        app["broadcast_task"].cancel()

    app.router.add_get("/", index)
    app.router.add_get("/output", output_page)
    app.router.add_get("/kiosk", kiosk_page)
    app.router.add_get("/kiosk-settings", kiosk_settings_page)
    app.router.add_get("/about", about)
    app.router.add_get("/welcome", welcome)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static/", _STATIC)

    # /static/ needs the same treatment, and used to miss it. The UI stopped
    # being "one HTML file" when the WebGL renderer moved out to
    # static/renderer.js: the page itself was always re-fetched while the
    # renderer beside it could come from cache. That combination is worse than
    # either alone — you get a NEW page driving an OLD renderer, so a freshly
    # added control appears, moves, updates its readout and sends its value,
    # and nothing is drawn differently. It reads as a broken feature rather
    # than a stale file, which is exactly the wrong place to go looking.
    async def _no_cache_static(request, response):
        if request.path.startswith("/static/"):
            response.headers.update(_NO_CACHE)
    app.on_response_prepare.append(_no_cache_static)
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    return app


# Commands the kiosk page itself sends. Everything else is an operator command
# and is restricted to loopback while kiosk mode is armed.
_KIOSK_COMMANDS = {"kiosk_press", "kiosk_release", "kiosk_cancel",
                   "kiosk_confirm", "kiosk_retry"}


async def _handle(engine, m: dict, app=None, ws=None, meta=None):
    t = m.get("type")
    loop = asyncio.get_event_loop()

    # While the installation is live the server is often on a venue network,
    # and this websocket has no authentication of any kind — it accepts
    # set_api_key, scene_delete and every parameter in the instrument. Armed,
    # it only listens to strangers about the kiosk itself. The operator can
    # still drive everything from a browser on the kiosk machine.
    if engine.kiosk.enabled and meta is not None and not meta.get("local"):
        if t not in _KIOSK_COMMANDS:
            return {"type": "kiosk_result", "ok": False,
                    "detail": "kiosk mode is armed — operator controls are "
                              "restricted to this machine"}

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
        # The director call may sit on the network for 1-3+ minutes (longer on
        # slower models). It runs off the event loop via run_in_executor either
        # way, but AWAITING that call inline here — as this used to — blocks
        # this connection's own message loop (the `async for msg in ws` below)
        # for the same duration: every other message from this browser tab
        # (a slider drag, mute, scene switch) would sit unprocessed until
        # generation finished, even though the state broadcaster (a separate
        # task) keeps the canvas/meters updating the whole time — which is
        # exactly the "looks alive, nothing responds" gap. Backgrounding it as
        # its own task lets this connection keep reading and dispatching other
        # messages while generation is in flight; the reply still lands on the
        # same websocket once it's done, just asynchronously rather than as
        # this handler's return value.
        async def _run_generate():
            try:
                await loop.run_in_executor(None, engine.generate_scene,
                                           m["keyword"], m.get("name"), m.get("audio"),
                                           m.get("size", "small"), m.get("warmth"),
                                           m.get("energy"), m.get("evolution"),
                                           m.get("kind", "3d"))
                # Cost rides on the ack rather than being read out of `state`: the
                # state broadcast is a separate ~20Hz loop, so the client would
                # otherwise be reading whatever snapshot happened to precede this
                # reply — which is the pre-generation one, where the cost is None.
                reply = {"type": "generate_result", "ok": True,
                         "source": engine.director.last_source,
                         "error": engine.director.last_error,
                         "cost": engine.director.last_cost,
                         "expansion": engine.director.last_expansion}
                await ws.send_str(json.dumps(reply))
            except (ConnectionResetError, asyncio.CancelledError):
                pass    # the tab closed/reloaded mid-generation — nothing to reply to
        asyncio.create_task(_run_generate())
        return None
    elif t == "cancel_generation":
        # Only takes effect between stream chunks (see SceneDirector.cancel /
        # _stream_or_call) — a request already fully received when this
        # arrives finishes normally. Safe to call with nothing running: it's
        # just a flag, cleared at the start of the next generate() either way.
        engine.director.cancel()
    elif t == "estimate_generation":
        # Answered from the director so the browser never carries its own copy
        # of the price table — the number quoted next to the slider and the
        # number the cost gate enforces are the same calculation.
        return {"type": "generation_estimate",
                **engine.director.estimate(int(m.get("nodes") or 0), m.get("kind", "3d"))}
    elif t == "set_cost_cap":
        engine.director.set_cost_cap(m.get("value") or 0)
    elif t == "set_mod_delay":
        ms = m.get("ms")
        engine.set_mod_delay(m.get("mode", "auto"),
                             None if ms is None else float(ms) / 1000.0)
    elif t == "set_audio":
        engine.set_audio_param(m["key"], m["value"])
    elif t == "apply_audio":
        # Same reasoning as "generate" above — background it so this
        # connection's message loop isn't blocked for the duration.
        async def _run_apply_audio():
            try:
                await loop.run_in_executor(None, engine.apply_audio_to_scene,
                                           m["scene"], m.get("audio", ""), m.get("warmth"),
                                           m.get("energy"), m.get("evolution"))
                reply = {"type": "generate_result", "ok": True, "action": "apply_audio",
                         "source": engine.director.last_source,
                         "error": engine.director.last_error,
                         "cost": engine.director.last_cost}
                await ws.send_str(json.dumps(reply))
            except (ConnectionResetError, asyncio.CancelledError):
                pass
        asyncio.create_task(_run_apply_audio())
        return None
    elif t == "audio_config":
        done = engine.configure_audio(device=m.get("device"), blocksize=m.get("blocksize"),
                                      latency=m.get("latency"), channels=m.get("channels"))
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
    elif t == "midi_port":
        ok = engine.midi.open_port(m.get("name", ""))
        return {"type": "midi_result", "action": "port", "ok": ok,
                "error": engine.midi.error}
    elif t == "midi_learn":
        # Same key again (or no key) cancels — the UI's learn button is a
        # toggle, so clicking the pulsing one backs out of learn mode.
        engine.midi.arm_learn(m.get("key"))
    elif t == "midi_unmap":
        engine.midi.unmap(m["key"])
    elif t == "midi_mode":
        engine.midi.set_mode(m["key"], m.get("mode", "catch"))
    elif t == "midi_pin":
        result = (engine.clear_midi_pins() if m.get("clear")
                  else engine.pin_midi_map())
        return {"type": "midi_result", "action": "pin", **result}
    elif t == "set_model":
        engine.set_model(m.get("value", "haiku"))
    elif t == "set_effort":
        engine.set_effort(m.get("value", "med"))
    elif t == "mod_add":
        engine.add_route(m.get("source", "audio_level"), m.get("dest", ""),
                         float(m.get("depth", 0.3)))
    elif t == "mod_remove":
        engine.remove_route(int(m.get("index", -1)))
    elif t == "scene_update":
        engine.update_current_scene(camera=m.get("camera", True), soundscape=m.get("soundscape", True))
    elif t == "scene_load":
        engine.load_scene(m["name"])
    elif t == "scene_save":
        engine.save_scene(m["name"])
    elif t == "scene_delete":
        engine.scenes.delete(m["name"])
    elif t == "show_title":
        if app:
            title = ""
            if engine.scenes.current:
                title = engine.scenes.current.spec.name
            for ws in list(app["clients"].keys()):
                try:
                    await ws.send_str(json.dumps({"type": "show_title", "title": title}))
                except Exception:
                    pass
    elif t == "set_kiosk":
        # Must stay reachable from loopback, or arming the mode from a remote
        # browser would lock you out of disarming it. The gate above already
        # refuses this command from anywhere else.
        #
        # Backgrounded for the same reason "generate" is, and it bites harder
        # here: arming loads a speech model, which on a cold cache means a
        # ~150MB download. Awaiting that inline stops this connection's own
        # `async for msg in ws` loop, and aiohttp only answers websocket pings
        # while that loop is iterating — so the browser's keepalive times out
        # and the socket dies mid-arm, looking exactly like a crash.
        async def _run_set_kiosk():
            try:
                ok, detail = await loop.run_in_executor(
                    None, engine.set_kiosk, bool(m.get("value")), m.get("attract"))
                await ws.send_str(json.dumps({"type": "kiosk_result", "action": "toggle",
                                              "ok": ok, "detail": detail}))
            except (ConnectionResetError, asyncio.CancelledError):
                pass
        asyncio.create_task(_run_set_kiosk())
        return None
    elif t == "set_kiosk_gen":
        # Deliberately NOT in _KIOSK_COMMANDS: this is an operator control, so
        # while armed it is loopback-only like every other one.
        return {"type": "kiosk_result", "action": "gen", "ok": True,
                "gen": engine.kiosk.set_gen(m.get("gen") or {})}
    elif t == "kiosk_scenes":
        # Answered on request rather than ridden along in `state()`: listing
        # reads every archived file, and state() runs 20x a second.
        return {"type": "kiosk_scenes", "scenes": engine.kiosk.archive_list()}
    elif t == "kiosk_scenes_delete":
        gone = engine.kiosk.archive_delete(m.get("names"), bool(m.get("all")))
        return {"type": "kiosk_scenes", "deleted": gone,
                "scenes": engine.kiosk.archive_list()}
    elif t == "kiosk_press":
        return {"type": "kiosk_result", "action": "press",
                "ok": engine.kiosk.press()}
    elif t == "kiosk_release":
        pcm = engine.kiosk.release()
        if pcm is None:
            return {"type": "kiosk_result", "action": "release", "ok": False}
        # Transcription + generation together run for the better part of a
        # minute. Backgrounded for the same reason "generate" is (see above):
        # so this connection keeps reading. No ack is sent when it finishes —
        # the phase rides in the 20Hz state broadcast, which every client sees
        # and which a page refreshed mid-generation picks straight back up.
        async def _run_kiosk():
            try:
                await loop.run_in_executor(None, engine.kiosk.run, pcm)
            except (ConnectionResetError, asyncio.CancelledError):
                pass
        asyncio.create_task(_run_kiosk())
        return {"type": "kiosk_result", "action": "release", "ok": True}
    elif t == "kiosk_confirm":
        # Generation is a minute-ish of network, so background it exactly like
        # kiosk_release does; phase rides the state broadcast either way.
        async def _run_confirm():
            try:
                await loop.run_in_executor(None, engine.kiosk.confirm)
            except (ConnectionResetError, asyncio.CancelledError):
                pass
        asyncio.create_task(_run_confirm())
        return None
    elif t == "kiosk_retry":
        return {"type": "kiosk_result", "action": "retry", "ok": engine.kiosk.retry()}
    elif t == "kiosk_cancel":
        engine.director.cancel()
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
