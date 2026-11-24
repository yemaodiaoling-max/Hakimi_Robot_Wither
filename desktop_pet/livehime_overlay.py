"""Bilibili 直播姬 & OBS 专用透明浏览器源服务 (450x520 Overlay).

Runs an internal ThreadingHTTPServer on 127.0.0.1:18970.
Endpoints:
  GET /live        -> Self-contained HTML5/CSS/JS page with transparent background
  GET /state       -> JSON snapshot of pet's live state & reactions
  GET /assets/<fn> -> Serves RGBA sprite pngs directly from desktop_pet/assets/
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
import urllib.parse

OVERLAY_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Hakimi Livehime Overlay</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; user-select: none; }
  body, html {
    width: 450px;
    height: 520px;
    background: transparent;
    overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", sans-serif;
  }
  #overlay-container {
    position: relative;
    width: 450px;
    height: 520px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: flex-end;
    padding-bottom: 24px;
  }
  
  /* Speech Bubble */
  #bubble-wrapper {
    position: absolute;
    top: 16px;
    left: 20px;
    right: 20px;
    min-height: 60px;
    max-height: 190px;
    display: flex;
    justify-content: center;
    align-items: flex-end;
    pointer-events: none;
    transition: opacity 0.25s cubic-bezier(0.2, 0.8, 0.2, 1), transform 0.25s cubic-bezier(0.2, 0.8, 0.2, 1);
    opacity: 0;
    transform: translateY(12px) scale(0.95);
  }
  #bubble-wrapper.visible {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
  #bubble-box {
    position: relative;
    background: #251d3a;
    border: 2px solid #8b5cf6;
    border-radius: 14px;
    padding: 10px 14px;
    color: #f5f3ff;
    box-shadow: 0 8px 24px rgba(139, 92, 246, 0.32);
    width: 100%;
    max-width: 400px;
  }
  #bubble-box::after {
    content: "";
    position: absolute;
    bottom: -9px;
    left: 50%;
    transform: translateX(-50%);
    width: 0;
    height: 0;
    border-left: 9px solid transparent;
    border-right: 9px solid transparent;
    border-top: 9px solid #8b5cf6;
  }
  #bubble-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 4px;
  }
  #bubble-title {
    font-size: 13px;
    font-weight: bold;
    color: #c084fc;
    letter-spacing: 0.5px;
  }
  #bubble-badge {
    font-size: 11px;
    color: #a78bfa;
    background: rgba(139, 92, 246, 0.25);
    border-radius: 6px;
    padding: 1px 6px;
  }
  #bubble-text {
    font-size: 14px;
    line-height: 1.5;
    color: #f5f3ff;
    word-break: break-word;
  }

  /* Pet Stage */
  #pet-stage {
    position: relative;
    width: 280px;
    height: 280px;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  #pet-sprite {
    width: 250px;
    height: 250px;
    object-fit: contain;
    filter: drop-shadow(0 10px 22px rgba(76, 29, 149, 0.38));
    transform-origin: center bottom;
    transition: filter 0.3s ease;
  }

  /* CSS Animations */
  /* 1. idle gentle float */
  .anim-idle {
    animation: idleFloat 3.2s ease-in-out infinite;
  }
  @keyframes idleFloat {
    0%, 100% { transform: translateY(0); }
    50% { transform: translateY(-8px); }
  }

  /* 2. talking rhythmic hop */
  .anim-talking {
    animation: talkingHop 0.48s ease-in-out infinite;
  }
  @keyframes talkingHop {
    0%, 100% { transform: translateY(0) scale(1, 1); }
    40% { transform: translateY(-14px) scale(0.97, 1.05); }
    70% { transform: translateY(0) scale(1.04, 0.96); }
  }

  /* 3. thinking gentle tilt sway */
  .anim-thinking {
    animation: thinkingSway 2.4s ease-in-out infinite;
  }
  @keyframes thinkingSway {
    0%, 100% { transform: rotate(-4deg) translateY(-2px); }
    50% { transform: rotate(4deg) translateY(-6px); }
  }

  /* 4. listening glowing mic ring */
  .anim-listening {
    animation: listeningPulse 1.2s ease-in-out infinite;
  }
  @keyframes listeningPulse {
    0%, 100% { transform: scale(1); filter: drop-shadow(0 0 16px rgba(239, 68, 68, 0.7)); }
    50% { transform: scale(1.03); filter: drop-shadow(0 0 28px rgba(244, 114, 182, 0.95)); }
  }

  /* 5. headpat squash and stretch */
  .anim-headpat {
    animation: headpatSquash 0.85s cubic-bezier(0.28, 0.84, 0.42, 1);
  }
  @keyframes headpatSquash {
    0% { transform: scale(1, 1); }
    18% { transform: scale(1.18, 0.82) translateY(12px); }
    42% { transform: scale(0.92, 1.1) translateY(-10px); }
    68% { transform: scale(1.06, 0.96) translateY(2px); }
    100% { transform: scale(1, 1) translateY(0); }
  }

  /* 6. poke startled jump and shake */
  .anim-poke {
    animation: pokeJump 0.8s cubic-bezier(0.2, 0.8, 0.2, 1);
  }
  @keyframes pokeJump {
    0% { transform: translateY(0) scale(1, 1); }
    20% { transform: translateY(-34px) scale(0.9, 1.15) rotate(-5deg); }
    35% { transform: translateY(-24px) rotate(5deg); }
    50% { transform: translateY(-12px) rotate(-3deg); }
    70% { transform: translateY(0) scale(1.08, 0.92); }
    85% { transform: translateY(-4px) scale(0.98, 1.02); }
    100% { transform: translateY(0) scale(1, 1) rotate(0); }
  }

  /* Mic halo ring around pet */
  #mic-ring {
    position: absolute;
    width: 255px;
    height: 255px;
    border-radius: 50%;
    border: 3px solid rgba(239, 68, 68, 0.6);
    box-shadow: 0 0 24px rgba(239, 68, 68, 0.5), inset 0 0 16px rgba(239, 68, 68, 0.3);
    opacity: 0;
    transform: scale(0.9);
    pointer-events: none;
    transition: opacity 0.3s ease, transform 0.3s ease;
  }
  #mic-ring.active {
    opacity: 1;
    transform: scale(1);
    animation: ringPulsing 1.4s ease-out infinite;
  }
  @keyframes ringPulsing {
    0% { transform: scale(0.96); box-shadow: 0 0 12px rgba(239, 68, 68, 0.4); }
    50% { transform: scale(1.04); box-shadow: 0 0 32px rgba(244, 114, 182, 0.7); }
    100% { transform: scale(0.96); box-shadow: 0 0 12px rgba(239, 68, 68, 0.4); }
  }

  /* Floating Emoji Particles */
  .particle {
    position: absolute;
    font-size: 24px;
    pointer-events: none;
    animation: particleFly 1.1s cubic-bezier(0.2, 0.8, 0.3, 1) forwards;
  }
  @keyframes particleFly {
    0% { opacity: 0; transform: translate(0, 0) scale(0.5); }
    20% { opacity: 1; transform: translate(var(--dx), -20px) scale(1.2); }
    100% { opacity: 0; transform: translate(calc(var(--dx) * 1.8), -90px) scale(0.8); }
  }
</style>
</head>
<body>
<div id="overlay-container">
  <div id="bubble-wrapper">
    <div id="bubble-box">
      <div id="bubble-header">
        <span id="bubble-title">🐱 哈基米</span>
        <span id="bubble-badge" style="display:none;"></span>
      </div>
      <div id="bubble-text"></div>
    </div>
  </div>

  <div id="pet-stage">
    <div id="mic-ring"></div>
    <img id="pet-sprite" src="/assets/pet_idle.png" alt="Hakimi">
    <div id="particles-layer"></div>
  </div>
</div>

<script>
  const elSprite = document.getElementById('pet-sprite');
  const elBubble = document.getElementById('bubble-wrapper');
  const elTitle = document.getElementById('bubble-title');
  const elBadge = document.getElementById('bubble-badge');
  const elText = document.getElementById('bubble-text');
  const elRing = document.getElementById('mic-ring');
  const elParticles = document.getElementById('particles-layer');

  let lastReactionId = 0;
  let reactionTimer = null;

  function spawnParticles(emojis, count = 5) {
    for (let i = 0; i < count; i++) {
      const p = document.createElement('div');
      p.className = 'particle';
      p.textContent = emojis[Math.floor(Math.random() * emojis.length)];
      const dx = (Math.random() * 140 - 70) + 'px';
      p.style.setProperty('--dx', dx);
      p.style.left = (120 + (Math.random() * 40 - 20)) + 'px';
      p.style.top = (110 + (Math.random() * 40 - 20)) + 'px';
      elParticles.appendChild(p);
      setTimeout(() => p.remove(), 1200);
    }
  }

  function applyAnimation(animMode) {
    elSprite.className = '';
    if (animMode === 'headpat') {
      elSprite.classList.add('anim-headpat');
    } else if (animMode === 'poke') {
      elSprite.classList.add('anim-poke');
    } else if (animMode === 'talking') {
      elSprite.classList.add('anim-talking');
    } else if (animMode === 'thinking') {
      elSprite.classList.add('anim-thinking');
    } else if (animMode === 'listening') {
      elSprite.classList.add('anim-listening');
    } else {
      elSprite.classList.add('anim-idle');
    }
  }

  async function pollState() {
    try {
      const res = await fetch('/state');
      if (!res.ok) return;
      const s = await res.json();

      // Update Sprite image
      const spriteName = s.sprite || 'idle';
      const targetSrc = `/assets/pet_${spriteName}.png`;
      if (elSprite.getAttribute('src') !== targetSrc) {
        elSprite.setAttribute('src', targetSrc);
      }

      // Mic ring
      if (s.is_recording) {
        elRing.classList.add('active');
      } else {
        elRing.classList.remove('active');
      }

      // Handle new reaction trigger
      if (s.reaction_id && s.reaction_id !== lastReactionId) {
        lastReactionId = s.reaction_id;
        if (s.reaction === 'headpat') {
          spawnParticles(['💖', '✨', '🌸', '🐾'], 6);
          applyAnimation('headpat');
          if (reactionTimer) clearTimeout(reactionTimer);
          reactionTimer = setTimeout(() => applyAnimation(s.anim_mode || 'idle'), 900);
        } else if (s.reaction === 'poke') {
          spawnParticles(['💢', '⚡', '💥', '🐾'], 6);
          applyAnimation('poke');
          if (reactionTimer) clearTimeout(reactionTimer);
          reactionTimer = setTimeout(() => applyAnimation(s.anim_mode || 'idle'), 850);
        }
      } else if (!reactionTimer) {
        applyAnimation(s.anim_mode || 'idle');
      }

      // Update Bubble
      if (s.bubble_visible && s.bubble_text) {
        elTitle.textContent = s.bubble_title || '🐱 哈基米';
        elText.textContent = s.bubble_text;
        if (s.badge_text) {
          elBadge.textContent = s.badge_text;
          elBadge.style.display = 'inline-block';
        } else {
          elBadge.style.display = 'none';
        }
        elBubble.classList.add('visible');
      } else {
        elBubble.classList.remove('visible');
      }
    } catch (e) {
      // transient network error, retry next tick
    }
  }

  setInterval(pollState, 120);
  pollState();
</script>
</body>
</html>
"""


def _create_handler_class(server_instance: LivehimeServer):
    class OverlayHTTPHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Silence default stdio request logging
            pass

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path in {"", "/", "/live"}:
                content = OVERLAY_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(content)
                return

            if path == "/state":
                state_json = json.dumps(server_instance.get_state(), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(state_json)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.end_headers()
                self.wfile.write(state_json)
                return

            if path.startswith("/assets/"):
                filename = os.path.basename(path)
                file_path = server_instance.assets_dir / filename
                if file_path.exists() and file_path.is_file():
                    content = file_path.read_bytes()
                    mime = "image/png" if filename.lower().endswith(".png") else "application/octet-stream"
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "public, max-age=3600")
                    self.end_headers()
                    self.wfile.write(content)
                    return

            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not Found")

    return OverlayHTTPHandler


class LivehimeServer:
    """Lightweight local HTTP server for Bilibili Livehime & OBS Browser Source."""

    def __init__(self, port: int = 18970, assets_dir: Path | None = None):
        self.port = port
        self.assets_dir = assets_dir or (Path(__file__).resolve().parent / "assets")
        self._lock = threading.Lock()
        self._state = {
            "sprite": "idle",
            "bubble_visible": False,
            "bubble_title": "🐱 哈基米",
            "bubble_text": "",
            "badge_text": "",
            "is_recording": False,
            "reaction": "",
            "reaction_id": 0,
            "anim_mode": "idle",
            "timestamp": time.time(),
        }
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def update_state(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                if k in self._state:
                    self._state[k] = v
            self._state["timestamp"] = time.time()

    def trigger_reaction(self, reaction_name: str, anim_mode: str | None = None):
        with self._lock:
            self._state["reaction"] = reaction_name
            self._state["reaction_id"] += 1
            if anim_mode:
                self._state["anim_mode"] = anim_mode
            self._state["timestamp"] = time.time()

    def get_state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def start(self):
        if self._server is not None:
            return
        handler_cls = _create_handler_class(self)
        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), handler_cls)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
        except Exception as exc:
            print(f"[LivehimeOverlay] Failed to start server on port {self.port}: {exc}")

    def stop(self):
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
