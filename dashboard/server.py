"""Live web dashboard: MJPEG video, WebSocket stats, and evaluation results.

The dashboard only displays what the Python peer is already doing; it never
touches the media path.
"""

import json
import logging
import os
import threading
import time

import cv2
from flask import Flask, Response, abort, jsonify, send_from_directory
from flask_sock import Sock

from .explain import MODE_NAMES, explain

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(os.path.dirname(HERE), "results")
PUSH_INTERVAL = 0.3


def build_payload(peer):
    snap = peer.snapshot()
    snap["explain"] = explain(snap)
    snap["mode_name"] = MODE_NAMES.get(snap["rx"]["mode"] or snap["config"]["fec"])
    return snap


def create_app(peer, results_dir=RESULTS_DIR):
    app = Flask(__name__, static_folder=os.path.join(HERE, "static"),
                static_url_path="/static")
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0     # always revalidate assets
    sock = Sock(app)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/stats")
    def stats():
        return jsonify(build_payload(peer))

    @sock.route("/ws")
    def ws(conn):
        while not peer.stop.is_set():
            conn.send(json.dumps(build_payload(peer)))
            time.sleep(PUSH_INTERVAL)

    def mjpeg(which):
        boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
        last = None
        while not peer.stop.is_set():
            with peer.lock:
                image = getattr(peer, f"{which}_image")
                jpeg = getattr(peer, f"{which}_jpeg")
            if image is not None and image is not last:
                last = image
                if jpeg is None:             # damaged frames have no clean JPEG
                    jpeg = cv2.imencode(".jpg", image,
                                        [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
                yield boundary + jpeg + b"\r\n"
            time.sleep(1 / 30)

    @app.get("/video.mjpg")
    def video():
        return Response(mjpeg("remote"),
                        mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.get("/local.mjpg")
    def local():
        return Response(mjpeg("local"),
                        mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.get("/results/<path:name>")
    def results(name):
        if not os.path.isfile(os.path.join(results_dir, name)):
            abort(404)
        resp = send_from_directory(results_dir, name)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return app


def start_dashboard(peer, host="127.0.0.1", port=8080):
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app = create_app(peer)
    t = threading.Thread(
        target=lambda: app.run(host=host, port=port, threaded=True,
                               use_reloader=False),
        name="dashboard", daemon=True)
    t.start()
    return t
