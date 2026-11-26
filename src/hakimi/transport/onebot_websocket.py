"""OneBot WebSocket message delivery and event framing."""

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import websockets


async def send_message(session_key: str, text: str, timeout: float, *, clients: dict, pending_acks: dict) -> dict:
    """Send through one connected socket and wait for that socket's acknowledgment."""
    kind, target_id = session_key.split(":", 1)
    echo = "agy_" + uuid.uuid4().hex
    payload = {
        "action": "send_group_msg" if kind == "group" else "send_private_msg",
        "params": {"group_id" if kind == "group" else "user_id": int(target_id), "message": text},
        "echo": echo,
    }
    # Select a single connection. Retrying a different socket can duplicate a send.
    websocket = max(clients, key=clients.get)
    future = asyncio.get_running_loop().create_future()
    pending_acks[echo] = (future, websocket)
    try:
        await asyncio.wait_for(websocket.send(json.dumps(payload, ensure_ascii=False)), timeout=timeout)
        response = await asyncio.wait_for(future, timeout=timeout)
        if response.get("status") == "ok" and response.get("retcode") == 0:
            return {"status": "success", "message_id": (response.get("data") or {}).get("message_id")}
        if response.get("status") == "failed" or response.get("retcode") not in (None, 0):
            return {"status": "rejected"}
        return {"status": "unknown"}
    except Exception:
        # Transport errors can occur after the platform accepted the message.
        return {"status": "unknown"}
    finally:
        pending_acks.pop(echo, None)
        if not future.done():
            future.cancel()


def resolve_onebot_ack(event: dict, websocket, pending_acks: dict) -> bool:
    echo = event.get("echo")
    if not isinstance(echo, str):
        return False
    waiting = pending_acks.get(echo)
    if not waiting or waiting[1] is not websocket:
        return False
    future = waiting[0]
    if not future.done():
        future.set_result(event)
    return True


async def receive_events(
    websocket, *, clients: dict, pending_acks: dict, incoming_events: asyncio.Queue,
    online_status_file: Path, resolve_ack: Callable[[dict, object], bool],
) -> None:
    """Queue incoming messages and resolve outstanding sends when the socket disconnects."""
    clients[websocket] = time.monotonic()
    print(f"\n[OneBot 已连接] QQ 客户端已接入桥接器! 当前连接数: {len(clients)}")
    try:
        async for raw_msg in websocket:
            try:
                event = json.loads(raw_msg)
                if not isinstance(event, dict) or resolve_ack(event, websocket):
                    continue
                if event.get("post_type") in ("message", "meta_event"):
                    try:
                        incoming_events.put_nowait(event)
                    except asyncio.QueueFull:
                        print("[接收队列已满] 本次事件未处理，请检查积压；未自动发送回复。")
            except Exception as e:
                print(f"解析 OneBot 消息出错: {e}")
    except websockets.ConnectionClosed:
        pass
    finally:
        clients.pop(websocket, None)
        for future, owner in list(pending_acks.values()):
            if owner is websocket and not future.done():
                future.set_result({"status": "unknown"})
        if not clients:
            online_status_file.unlink(missing_ok=True)
        print("[OneBot 断开] QQ 客户端连接已断开。")
