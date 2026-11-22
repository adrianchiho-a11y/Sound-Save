#!/usr/bin/env python3
"""Sound Save LAN helper.

Run this on each computer that should participate in direct Wi-Fi sync:
    py sound-save-helper.py

The helper only relays files between devices on the same local network.
It does not use Firebase or store files in a cloud service.
"""
import base64
import hashlib
import http.server
import json
import os
import socket
import socketserver
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

HTTP_PORT = 8765
DISCOVERY_PORT = 8766
APP_ORIGIN = "https://adrianchiho-a11y.github.io"
DATA_DIR = Path.home() / "SoundSave-LAN"
INBOX = DATA_DIR / "inbox"
INBOX.mkdir(parents=True, exist_ok=True)

state = {"uid_hash": "", "device_id": str(uuid.uuid4()), "name": socket.gethostname()}
peers = {}
lock = threading.Lock()


def uid_digest(uid):
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()


def discovery_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", DISCOVERY_PORT))
    while True:
        try:
            packet, address = sock.recvfrom(4096)
            data = json.loads(packet.decode("utf-8"))
            if data.get("kind") != "sound-save-lan" or data.get("device_id") == state["device_id"]:
                continue
            if not state["uid_hash"] or data.get("uid_hash") != state["uid_hash"]:
                continue
            with lock:
                peers[data["device_id"]] = {
                    "device_id": data["device_id"],
                    "name": data.get("name", "Sound Save device"),
                    "host": address[0],
                    "port": int(data.get("port", HTTP_PORT)),
                    "seen": time.time(),
                }
        except Exception:
            continue


def discovery_broadcast():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    while True:
        if state["uid_hash"]:
            packet = json.dumps({
                "kind": "sound-save-lan",
                "device_id": state["device_id"],
                "name": state["name"],
                "uid_hash": state["uid_hash"],
                "port": HTTP_PORT,
            }).encode("utf-8")
            try:
                sock.sendto(packet, ("255.255.255.255", DISCOVERY_PORT))
            except OSError:
                pass
        time.sleep(2)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def cors(self):
        origin = self.headers.get("Origin", "")
        if origin == APP_ORIGIN or origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-SoundSave-Account")
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def reply(self, status, data, content_type="application/json"):
        body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def authorized(self):
        supplied = self.headers.get("X-SoundSave-Account", "")
        return bool(state["uid_hash"] and supplied == state["uid_hash"])

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            return self.reply(200, {"ok": True, "configured": bool(state["uid_hash"]), "device": state["name"]})
        if not self.authorized():
            return self.reply(401, {"error": "Sign in to Sound Save and connect this helper first."})
        if parsed.path == "/peers":
            now = time.time()
            with lock:
                live = [p for p in peers.values() if now - p["seen"] < 8]
            return self.reply(200, {"peers": live, "device_id": state["device_id"]})
        if parsed.path == "/inbox":
            items = []
            for p in INBOX.glob("*.json"):
                try:
                    meta = json.loads(p.read_text("utf-8"))
                    audio = INBOX / (p.stem + ".audio")
                    if audio.exists():
                        items.append({**meta, "id": p.stem, "size": audio.stat().st_size})
                except Exception:
                    pass
            return self.reply(200, {"items": items})
        if parsed.path.startswith("/item/"):
            item_id = parsed.path.split("/", 2)[2]
            if not item_id.isalnum() and "-" not in item_id:
                return self.reply(400, {"error": "Invalid item id"})
            audio = INBOX / (item_id + ".audio")
            if not audio.is_file():
                return self.reply(404, {"error": "Not found"})
            return self.reply(200, audio.read_bytes(), "application/octet-stream")
        return self.reply(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 60 * 1024 * 1024:
            return self.reply(413, {"error": "Audio file exceeds the 60 MB per-file helper limit."})
        body = self.rfile.read(length)
        if parsed.path == "/configure":
            try:
                data = json.loads(body.decode("utf-8"))
                uid = data.get("uid", "")
                if not isinstance(uid, str) or len(uid) < 10:
                    return self.reply(400, {"error": "Invalid Google account session."})
                state["uid_hash"] = uid_digest(uid)
                return self.reply(200, {"ok": True})
            except Exception:
                return self.reply(400, {"error": "Invalid configuration."})
        if not self.authorized():
            return self.reply(401, {"error": "This device is not connected to the same Sound Save account."})
        if parsed.path == "/receive":
            try:
                meta_raw = self.headers.get("X-SoundSave-Metadata", "")
                meta = json.loads(base64.urlsafe_b64decode(meta_raw + "==").decode("utf-8"))
                item_id = str(meta.get("id") or uuid.uuid4())
                if not item_id.replace("-", "").isalnum():
                    return self.reply(400, {"error": "Invalid track id"})
                (INBOX / (item_id + ".audio")).write_bytes(body)
                (INBOX / (item_id + ".json")).write_text(json.dumps(meta), "utf-8")
                return self.reply(200, {"ok": True, "id": item_id, "bytes": len(body)})
            except Exception as exc:
                return self.reply(400, {"error": "Could not receive audio: " + str(exc)})
        if parsed.path == "/ack":
            try:
                item_id = json.loads(body.decode("utf-8")).get("id", "")
                for suffix in (".audio", ".json"):
                    try:
                        (INBOX / (item_id + suffix)).unlink()
                    except FileNotFoundError:
                        pass
                return self.reply(200, {"ok": True})
            except Exception:
                return self.reply(400, {"error": "Invalid acknowledgement"})
        return self.reply(404, {"error": "Not found"})


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    threading.Thread(target=discovery_listener, daemon=True).start()
    threading.Thread(target=discovery_broadcast, daemon=True).start()
    server = ThreadedServer(("0.0.0.0", HTTP_PORT), Handler)
    print("Sound Save LAN helper is running.")
    print("Keep this window open while syncing. Press Ctrl+C to stop.")
    print("Local helper: http://127.0.0.1:%d" % HTTP_PORT)
    print("Received files are staged in: %s" % INBOX)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSound Save LAN helper stopped.")
