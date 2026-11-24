"""Local model gateway: one loopback HTTPS endpoint for every Hakimi module.

QQ replies, video watching and the desktop pet all talk to ``https://127.0.0.1:18765`` using the
Gemini ``generateContent`` wire protocol with a local bearer key.  The gateway forwards to the
upstream you configure in the setup panel (Google AI Studio by default, or any Gemini-compatible
endpoint), maps Hakimi's internal model tiers to real model names, and keeps a local spend ledger
with an optional monthly cap.  See ``docs/gateway.md``.
"""
