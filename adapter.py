"""
NapCat (OneBot 11) Platform Adapter for Hermes Agent.

A plugin-based gateway adapter that hosts a reverse WebSocket server for
NapCat to connect to (inbound events) and calls NapCat's HTTP API for
outbound messages.

Configuration in config.yaml::

    gateway:
      platforms:
        napcat:
          enabled: true
          extra:
            ws_host: 0.0.0.0
            ws_port: 18800
            http_api: http://127.0.0.1:3000
            access_token: ""              # optional bearer token
            self_id: ""                   # bot QQ number, auto-detected if empty
            allowed_users: []             # QQ numbers permitted in DMs
            allow_all_users: false
            group_allowlist: []           # group IDs to listen in
            require_mention: true         # group: only respond when @mentioned
            qq_text_limit: 4500           # chunk size for long messages
            home_channel: ""              # e.g. "private:123456" or "group:789"

Or via environment variables:
    NAPCAT_WS_HOST, NAPCAT_WS_PORT, NAPCAT_HTTP_API, NAPCAT_ACCESS_TOKEN,
    NAPCAT_SELF_ID, NAPCAT_ALLOWED_USERS, NAPCAT_ALLOW_ALL_USERS,
    NAPCAT_GROUP_ALLOWLIST, NAPCAT_REQUIRE_MENTION, NAPCAT_HOME_CHANNEL

Protocol reference: https://github.com/botuniverse/onebot-11
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy / core-imported types
# ---------------------------------------------------------------------------

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_url,
    cache_audio_from_url,
)
from gateway.config import Platform, PlatformConfig

# Outbound chunk limit when fragmenting long replies.
QQ_TEXT_LIMIT = 4500
# Default reverse WS bind port.
DEFAULT_WS_PORT = 18800


# ---------------------------------------------------------------------------
# OneBot 11 helpers
# ---------------------------------------------------------------------------

def text_segment(text: str) -> Dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


def image_segment(file_url: str) -> Dict[str, Any]:
    return {"type": "image", "data": {"file": file_url}}


def at_segment(qq: str | int) -> Dict[str, Any]:
    return {"type": "at", "data": {"qq": str(qq)}}


def reply_segment(message_id: str | int) -> Dict[str, Any]:
    return {"type": "reply", "data": {"id": str(message_id)}}


def record_segment(file_url: str) -> Dict[str, Any]:
    return {"type": "record", "data": {"file": file_url}}


def video_segment(file_url: str) -> Dict[str, Any]:
    return {"type": "video", "data": {"file": file_url}}


def extract_text_from_segments(segments: List[Dict[str, Any]]) -> str:
    """Flatten OneBot 11 message segments into a plain string.

    @mentions render as ``@<qq>`` so the agent retains who was addressed.
    Images / records / videos are intentionally skipped — they are surfaced
    via ``media_urls`` instead.
    """
    out: List[str] = []
    for seg in segments or []:
        stype = seg.get("type")
        data = seg.get("data") or {}
        if stype == "text":
            out.append(str(data.get("text", "")))
        elif stype == "at":
            out.append(f"@{data.get('qq', '')}")
        elif stype == "face":
            out.append("")  # face IDs not human-readable; drop
    return "".join(out).strip()


def extract_image_urls(segments: List[Dict[str, Any]]) -> List[str]:
    urls: List[str] = []
    for seg in segments or []:
        if seg.get("type") == "image":
            data = seg.get("data") or {}
            url = data.get("url") or data.get("file")
            if url:
                urls.append(str(url))
    return urls


def extract_record_url(segments: List[Dict[str, Any]]) -> Optional[str]:
    for seg in segments or []:
        if seg.get("type") == "record":
            data = seg.get("data") or {}
            url = data.get("url") or data.get("file")
            if url:
                return str(url)
    return None


def has_mention_of(segments: List[Dict[str, Any]], self_id: str) -> bool:
    if not self_id:
        return False
    for seg in segments or []:
        if seg.get("type") == "at" and str((seg.get("data") or {}).get("qq", "")) == str(self_id):
            return True
    return False


def strip_mention_of(
    segments: List[Dict[str, Any]], self_id: str
) -> List[Dict[str, Any]]:
    if not self_id:
        return list(segments or [])
    return [
        seg
        for seg in (segments or [])
        if not (seg.get("type") == "at" and str((seg.get("data") or {}).get("qq", "")) == str(self_id))
    ]


def chunk_text(text: str, limit: int) -> List[str]:
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        # Prefer breaking on newline / space
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


def _parse_target(chat_id: str) -> Tuple[str, str]:
    """Parse ``chat_id`` into ``(kind, id)``.

    Accepts:
        ``123456``                → ("private", "123456")
        ``private:123456``        → ("private", "123456")
        ``group:789``             → ("group", "789")
        ``napcat:...``            → strip prefix, recurse
    """
    raw = (chat_id or "").strip()
    raw = raw.removeprefix("napcat:")
    if raw.startswith("group:"):
        return "group", raw[len("group:") :]
    if raw.startswith("private:"):
        return "private", raw[len("private:") :]
    # Bare numeric id — treat as private.
    return "private", raw


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class NapCatAdapter(BasePlatformAdapter):
    """Hosts an OneBot 11 reverse WebSocket server for NapCat."""

    def __init__(self, config: PlatformConfig, **kwargs):
        super().__init__(config=config, platform=Platform("napcat"))

        extra = getattr(config, "extra", {}) or {}

        # Reverse WS server config
        self.ws_host: str = os.getenv("NAPCAT_WS_HOST") or extra.get("ws_host", "0.0.0.0")
        self.ws_port: int = int(
            os.getenv("NAPCAT_WS_PORT") or extra.get("ws_port", DEFAULT_WS_PORT)
        )
        self.ws_path: str = extra.get("ws_path", "/onebot/v11/ws")

        # HTTP API client config
        self.http_api: str = (
            os.getenv("NAPCAT_HTTP_API") or extra.get("http_api", "")
        ).rstrip("/")
        self.access_token: str = (
            os.getenv("NAPCAT_ACCESS_TOKEN") or extra.get("access_token", "")
        )
        self.self_id: str = str(
            os.getenv("NAPCAT_SELF_ID") or extra.get("self_id", "") or ""
        )

        # Auth and routing
        env_users = os.getenv("NAPCAT_ALLOWED_USERS", "")
        allowed = (
            [u.strip() for u in env_users.split(",") if u.strip()]
            if env_users
            else (extra.get("allowed_users") or [])
        )
        self.allowed_users: set[str] = {str(u) for u in allowed}
        self.allow_all_users: bool = (
            (os.getenv("NAPCAT_ALLOW_ALL_USERS", "").lower() in ("1", "true", "yes"))
            if os.getenv("NAPCAT_ALLOW_ALL_USERS")
            else bool(extra.get("allow_all_users", False))
        )

        env_groups = os.getenv("NAPCAT_GROUP_ALLOWLIST", "")
        group_list = (
            [g.strip() for g in env_groups.split(",") if g.strip()]
            if env_groups
            else (extra.get("group_allowlist") or [])
        )
        self.group_allowlist: set[str] = {str(g) for g in group_list}

        self.require_mention: bool = (
            (os.getenv("NAPCAT_REQUIRE_MENTION", "").lower() in ("1", "true", "yes"))
            if os.getenv("NAPCAT_REQUIRE_MENTION")
            else bool(extra.get("require_mention", True))
        )
        self.qq_text_limit: int = int(extra.get("qq_text_limit", QQ_TEXT_LIMIT))

        # Runtime state
        self._site = None
        self._runner = None
        self._http_session: Optional[Any] = None
        self._active_ws = None  # active aiohttp.web.WebSocketResponse from NapCat

    @property
    def name(self) -> str:
        return "NapCat"

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self) -> bool:
        if not self.http_api:
            self._set_fatal_error(
                "config_missing",
                "NAPCAT_HTTP_API must be configured (e.g. http://127.0.0.1:3000)",
                retryable=False,
            )
            return False

        try:
            import aiohttp
            from aiohttp import web
        except ImportError:
            self._set_fatal_error(
                "deps_missing",
                "aiohttp is required for the NapCat adapter (install hermes[messaging])",
                retryable=False,
            )
            return False

        # Prevent two profiles binding the same identity
        if not self._acquire_platform_lock(
            "napcat",
            f"{self.ws_host}:{self.ws_port}",
            f"NapCat reverse WS {self.ws_host}:{self.ws_port}",
        ):
            return False

        # Shared HTTP client for API calls
        timeout = aiohttp.ClientTimeout(total=15)
        self._http_session = aiohttp.ClientSession(timeout=timeout)

        # Build a reverse WS server
        app = web.Application()
        app.router.add_get(self.ws_path, self._ws_handler)
        # Some NapCat builds use the root path; accept both.
        app.router.add_get("/", self._ws_handler)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.ws_host, self.ws_port)
        try:
            await self._site.start()
        except OSError as exc:
            logger.error("NapCat: failed to bind %s:%s — %s", self.ws_host, self.ws_port, exc)
            await self._close_http()
            self._set_fatal_error("bind_failed", str(exc), retryable=True)
            return False

        # Probe HTTP API for bot identity if self_id not set.
        # The HTTP API may not be reachable yet (NapCat still booting / not
        # logged in), so probe failure is non-fatal — we simply log it and
        # let the reverse-WS handshake surface self_id later via X-Self-ID.
        if not self.self_id:
            try:
                info = await self._call_api("get_login_info", {})
                if info and isinstance(info, dict):
                    self.self_id = str(info.get("user_id", "") or "")
                    logger.info(
                        "NapCat: detected bot QQ=%s nickname=%s",
                        self.self_id,
                        info.get("nickname"),
                    )
            except Exception as exc:
                logger.warning(
                    "NapCat: HTTP API probe failed (%s) — will recover when NapCat connects via reverse WS",
                    exc,
                )

        self._mark_connected()
        logger.info(
            "NapCat: reverse WS listening on ws://%s:%s%s (http_api=%s)",
            self.ws_host,
            self.ws_port,
            self.ws_path,
            self.http_api,
        )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        try:
            ws = self._active_ws
            if ws is not None and not ws.closed:
                await ws.close()
        except Exception:
            pass
        self._active_ws = None
        try:
            if self._site is not None:
                await self._site.stop()
        except Exception:
            pass
        try:
            if self._runner is not None:
                await self._runner.cleanup()
        except Exception:
            pass
        self._site = None
        self._runner = None
        await self._close_http()
        self._release_platform_lock()

    async def _close_http(self) -> None:
        try:
            if self._http_session is not None and not self._http_session.closed:
                await self._http_session.close()
        except Exception:
            pass
        self._http_session = None

    # ── Reverse WS handler ────────────────────────────────────────────────

    async def _ws_handler(self, request):
        from aiohttp import web, WSMsgType

        # Bearer token check (NapCat sends it as `Authorization: Bearer <token>`
        # or via the `access_token` query param).
        if self.access_token:
            auth = request.headers.get("Authorization", "")
            token_qs = request.query.get("access_token", "")
            supplied = ""
            if auth.startswith("Bearer "):
                supplied = auth[7:].strip()
            elif token_qs:
                supplied = token_qs.strip()
            if supplied != self.access_token:
                logger.warning("NapCat: rejected WS connection — bad access token")
                return web.Response(status=401, text="unauthorized")

        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        # ``X-Self-ID`` carries the bot QQ on connect (OneBot 11 convention).
        header_self = request.headers.get("X-Self-ID")
        if header_self and not self.self_id:
            self.self_id = str(header_self)
            logger.info("NapCat: bot QQ identified as %s via WS handshake", self.self_id)

        self._active_ws = ws
        logger.info("NapCat: WS client connected (peer=%s)", request.remote)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except Exception:
                        logger.debug("NapCat: invalid JSON from WS")
                        continue
                    asyncio.create_task(self._dispatch_event(payload))
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("NapCat: WS error: %s", ws.exception())
        finally:
            logger.info("NapCat: WS client disconnected")
            if self._active_ws is ws:
                self._active_ws = None
        return ws

    async def _dispatch_event(self, payload: Dict[str, Any]) -> None:
        try:
            post_type = payload.get("post_type")
            if post_type == "meta_event":
                return  # heartbeat / lifecycle — ignore
            if post_type != "message":
                return
            await self._handle_message_event(payload)
        except Exception as exc:
            logger.exception("NapCat: error dispatching event: %s", exc)

    async def _handle_message_event(self, event: Dict[str, Any]) -> None:
        message_type = event.get("message_type")  # "private" | "group"
        is_group = message_type == "group"
        segments = event.get("message") or []
        if isinstance(segments, str):
            # String-format message — wrap as text segment for uniformity.
            segments = [text_segment(segments)]

        sender = event.get("sender") or {}
        sender_id = str(event.get("user_id", "") or sender.get("user_id", ""))
        sender_name = (
            sender.get("card") or sender.get("nickname") or f"qq:{sender_id}"
        )
        group_id = str(event.get("group_id", "") or "")
        self_id = str(event.get("self_id", "") or self.self_id or "")
        if self_id and not self.self_id:
            self.self_id = self_id

        # Ignore self-messages
        if sender_id and self_id and sender_id == self_id:
            return

        # Group: enforce allowlist + mention requirement
        if is_group:
            if self.group_allowlist and group_id not in self.group_allowlist:
                return
            if self.require_mention and not has_mention_of(segments, self_id):
                return
            segments = strip_mention_of(segments, self_id)
        else:
            if not self.allow_all_users and self.allowed_users:
                if sender_id not in self.allowed_users:
                    logger.debug("NapCat: dropping DM from unauthorized QQ=%s", sender_id)
                    return

        text = extract_text_from_segments(segments)
        image_urls = extract_image_urls(segments)
        record_url = extract_record_url(segments)

        # Skip events that carry nothing actionable.
        if not text and not image_urls and not record_url:
            return

        # Resolve media → local cache so vision/STT tools can use them.
        media_paths: List[str] = []
        media_types: List[str] = []
        msg_type = MessageType.TEXT

        for url in image_urls[:4]:  # cap to avoid runaway downloads
            try:
                ext = ".jpg"
                low = url.split("?", 1)[0].lower()
                for candidate in (".png", ".gif", ".webp", ".jpeg", ".jpg"):
                    if low.endswith(candidate):
                        ext = candidate
                        break
                path = await cache_image_from_url(url, ext=ext)
                if path:
                    media_paths.append(path)
                    media_types.append("image")
                    msg_type = MessageType.PHOTO
            except Exception as exc:
                logger.warning("NapCat: failed to cache QQ image: %s", exc)

        if record_url:
            try:
                # NapCat usually serves silk-encoded voice; let the audio cache
                # store it raw and downstream STT decide how to handle it.
                ext = ".silk"
                low = record_url.split("?", 1)[0].lower()
                for candidate in (".silk", ".amr", ".mp3", ".ogg", ".wav", ".m4a"):
                    if low.endswith(candidate):
                        ext = candidate
                        break
                path = await cache_audio_from_url(record_url, ext=ext)
                if path:
                    media_paths.append(path)
                    media_types.append("audio")
                    msg_type = MessageType.VOICE
            except Exception as exc:
                logger.warning("NapCat: failed to cache QQ voice: %s", exc)

        # Reply context (best-effort): pull quoted message text via get_msg.
        reply_to_id: Optional[str] = None
        reply_to_text: Optional[str] = None
        for seg in segments:
            if seg.get("type") == "reply":
                rid = (seg.get("data") or {}).get("id")
                if rid:
                    reply_to_id = str(rid)
                    try:
                        quoted = await self._call_api("get_msg", {"message_id": int(rid)})
                        if isinstance(quoted, dict):
                            reply_to_text = extract_text_from_segments(quoted.get("message") or [])
                    except Exception:
                        pass

        chat_id = f"group:{group_id}" if is_group else sender_id
        chat_name = f"QQ group {group_id}" if is_group else sender_name
        chat_type = "group" if is_group else "dm"

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=sender_id,
            user_name=sender_name,
        )

        event_obj = MessageEvent(
            text=text or ("[image]" if image_urls else "[voice]" if record_url else ""),
            message_type=msg_type,
            source=source,
            raw_message=event,
            message_id=str(event.get("message_id", "")),
            media_urls=media_paths,
            media_types=media_types,
            reply_to_message_id=reply_to_id,
            reply_to_text=reply_to_text,
            timestamp=_dt.datetime.fromtimestamp(int(event.get("time") or time.time())),
        )

        await self.handle_message(event_obj)

    # ── Outbound (BasePlatformAdapter API) ────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        kind, target_id = _parse_target(chat_id)
        try:
            target_int = int(target_id)
        except ValueError:
            return SendResult(success=False, error=f"Invalid QQ target id: {chat_id}")

        last_id: Optional[str] = None
        last_error: Optional[str] = None
        chunks = chunk_text(content or "", self.qq_text_limit) or [""]
        for i, chunk in enumerate(chunks):
            segments: List[Dict[str, Any]] = []
            if i == 0 and reply_to:
                segments.append(reply_segment(reply_to))
            if chunk:
                segments.append(text_segment(chunk))
            if not segments:
                continue
            res = await self._send_segments(kind, target_int, segments)
            if not res.success:
                last_error = res.error
                # Stop on the first failure to avoid spamming.
                return SendResult(success=False, error=last_error)
            last_id = res.message_id

        return SendResult(success=True, message_id=last_id, error=last_error)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """QQ / OneBot 11 has no typing indicator — no-op."""
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        kind, target_id = _parse_target(chat_id)
        return {
            "chat_id": chat_id,
            "name": chat_id,
            "type": "group" if kind == "group" else "dm",
        }

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        kind, target_id = _parse_target(chat_id)
        try:
            target_int = int(target_id)
        except ValueError:
            return SendResult(success=False, error=f"Invalid QQ target id: {chat_id}")
        segments: List[Dict[str, Any]] = []
        if reply_to:
            segments.append(reply_segment(reply_to))
        segments.append(image_segment(image_url))
        if caption:
            segments.append(text_segment(caption[: self.qq_text_limit]))
        return await self._send_segments(kind, target_int, segments)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """NapCat accepts ``file://`` URLs for local files."""
        url = image_path if "://" in image_path else f"file://{image_path}"
        return await self.send_image(chat_id, url, caption=caption, reply_to=reply_to, metadata=metadata)

    async def send_voice(
        self,
        chat_id: str,
        voice_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        kind, target_id = _parse_target(chat_id)
        try:
            target_int = int(target_id)
        except ValueError:
            return SendResult(success=False, error=f"Invalid QQ target id: {chat_id}")
        url = voice_path if "://" in voice_path else f"file://{voice_path}"
        return await self._send_segments(kind, target_int, [record_segment(url)])

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        kind, target_id = _parse_target(chat_id)
        try:
            target_int = int(target_id)
        except ValueError:
            return SendResult(success=False, error=f"Invalid QQ target id: {chat_id}")
        url = video_path if "://" in video_path else f"file://{video_path}"
        return await self._send_segments(kind, target_int, [video_segment(url)])

    async def send_document(
        self,
        chat_id: str,
        doc_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        kind, target_id = _parse_target(chat_id)
        try:
            target_int = int(target_id)
        except ValueError:
            return SendResult(success=False, error=f"Invalid QQ target id: {chat_id}")
        url = doc_path if "://" in doc_path else f"file://{doc_path}"
        name = os.path.basename(doc_path)
        action = "upload_group_file" if kind == "group" else "upload_private_file"
        key = "group_id" if kind == "group" else "user_id"
        try:
            await self._call_api(action, {key: target_int, "file": url, "name": name})
            return SendResult(success=True)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    # ── HTTP API plumbing ─────────────────────────────────────────────────

    async def _send_segments(
        self, kind: str, target_id: int, segments: List[Dict[str, Any]]
    ) -> SendResult:
        if kind == "group":
            params: Dict[str, Any] = {"group_id": target_id, "message": segments}
            action = "send_group_msg"
        else:
            params = {"user_id": target_id, "message": segments}
            action = "send_private_msg"
        try:
            data = await self._call_api(action, params)
            mid = ""
            if isinstance(data, dict):
                mid = str(data.get("message_id", "") or "")
            return SendResult(success=True, message_id=mid, raw_response=data)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    async def _call_api(self, action: str, params: Dict[str, Any]) -> Any:
        if not self.http_api:
            raise RuntimeError("NapCat HTTP API URL not configured")
        if self._http_session is None or self._http_session.closed:
            import aiohttp

            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        url = f"{self.http_api}/{action}"
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        async with self._http_session.post(
            url, data=json.dumps(params), headers=headers
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"OneBot API {action} HTTP {resp.status}: {body[:200]}"
                )
            payload = await resp.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"OneBot API {action}: unexpected response: {payload!r}")
        if payload.get("retcode", 0) != 0:
            raise RuntimeError(
                f"OneBot API {action} failed: retcode={payload.get('retcode')} status={payload.get('status')}"
            )
        return payload.get("data")


# ---------------------------------------------------------------------------
# Plugin registration helpers
# ---------------------------------------------------------------------------


def check_requirements() -> bool:
    """Check whether NapCat is configured."""
    http_api = os.getenv("NAPCAT_HTTP_API", "")
    return bool(http_api)


def validate_config(config: PlatformConfig) -> bool:
    extra = getattr(config, "extra", {}) or {}
    http_api = os.getenv("NAPCAT_HTTP_API") or extra.get("http_api", "")
    return bool(http_api)


def is_connected(config: PlatformConfig) -> bool:
    return validate_config(config)


def _env_enablement() -> dict | None:
    """Seed PlatformConfig.extra from env vars during gateway config load."""
    http_api = os.getenv("NAPCAT_HTTP_API", "").strip()
    if not http_api:
        return None
    seed: Dict[str, Any] = {"http_api": http_api}
    ws_host = os.getenv("NAPCAT_WS_HOST", "").strip()
    if ws_host:
        seed["ws_host"] = ws_host
    ws_port = os.getenv("NAPCAT_WS_PORT", "").strip()
    if ws_port:
        try:
            seed["ws_port"] = int(ws_port)
        except ValueError:
            pass
    token = os.getenv("NAPCAT_ACCESS_TOKEN", "").strip()
    if token:
        seed["access_token"] = token
    self_id = os.getenv("NAPCAT_SELF_ID", "").strip()
    if self_id:
        seed["self_id"] = self_id
    require_mention = os.getenv("NAPCAT_REQUIRE_MENTION", "").strip().lower()
    if require_mention:
        seed["require_mention"] = require_mention in ("1", "true", "yes")
    allow_all = os.getenv("NAPCAT_ALLOW_ALL_USERS", "").strip().lower()
    if allow_all:
        seed["allow_all_users"] = allow_all in ("1", "true", "yes")

    home = os.getenv("NAPCAT_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("NAPCAT_HOME_CHANNEL_NAME", home),
        }
    return seed


def interactive_setup() -> None:
    """Interactive ``hermes gateway setup`` flow for NapCat."""
    from hermes_cli.setup import (
        prompt,
        prompt_yes_no,
        save_env_value,
        get_env_value,
        print_header,
        print_info,
        print_warning,
        print_success,
    )

    print_header("NapCat (QQ via OneBot 11)")
    existing = get_env_value("NAPCAT_HTTP_API")
    if existing:
        print_info(f"NapCat: already configured (http_api: {existing})")
        if not prompt_yes_no("Reconfigure NapCat?", False):
            return

    print_info("Hermes will host a reverse WebSocket server for NapCat to connect to,")
    print_info("and call NapCat's HTTP API for outbound messages.")
    print()

    http_api = prompt(
        "NapCat HTTP API base URL (e.g. http://127.0.0.1:3000)",
        default=existing or "http://127.0.0.1:3000",
    )
    if not http_api:
        print_warning("NapCat HTTP API is required — skipping setup")
        return
    save_env_value("NAPCAT_HTTP_API", http_api.strip())

    ws_host = prompt(
        "Reverse WS bind host (default 0.0.0.0)",
        default=get_env_value("NAPCAT_WS_HOST") or "0.0.0.0",
    )
    save_env_value("NAPCAT_WS_HOST", ws_host.strip() or "0.0.0.0")

    ws_port = prompt(
        f"Reverse WS bind port (default {DEFAULT_WS_PORT})",
        default=get_env_value("NAPCAT_WS_PORT") or str(DEFAULT_WS_PORT),
    )
    try:
        save_env_value("NAPCAT_WS_PORT", str(int(ws_port)) if ws_port else str(DEFAULT_WS_PORT))
    except ValueError:
        save_env_value("NAPCAT_WS_PORT", str(DEFAULT_WS_PORT))

    if prompt_yes_no("Use an access token for WS/HTTP authentication?", True):
        token = prompt("Access token", password=True, default=get_env_value("NAPCAT_ACCESS_TOKEN") or "")
        if token:
            save_env_value("NAPCAT_ACCESS_TOKEN", token)

    self_id = prompt(
        "Bot QQ number (leave blank to auto-detect)",
        default=get_env_value("NAPCAT_SELF_ID") or "",
    )
    if self_id:
        save_env_value("NAPCAT_SELF_ID", self_id.strip())

    print()
    print_info("Access control")
    allow_all = prompt_yes_no("Allow any QQ user to DM the bot?", False)
    if allow_all:
        save_env_value("NAPCAT_ALLOW_ALL_USERS", "true")
        save_env_value("NAPCAT_ALLOWED_USERS", "")
        print_warning("Open DMs — anyone can talk to the bot.")
    else:
        save_env_value("NAPCAT_ALLOW_ALL_USERS", "false")
        allowed = prompt(
            "Allowed QQ numbers (comma-separated)",
            default=get_env_value("NAPCAT_ALLOWED_USERS") or "",
        )
        if allowed:
            save_env_value("NAPCAT_ALLOWED_USERS", allowed.replace(" ", ""))
            print_success("DM allowlist saved")

    groups = prompt(
        "Group IDs to listen in (comma-separated, blank = all)",
        default=get_env_value("NAPCAT_GROUP_ALLOWLIST") or "",
    )
    save_env_value("NAPCAT_GROUP_ALLOWLIST", groups.replace(" ", "") if groups else "")

    require_mention = prompt_yes_no("Require @bot mention in groups?", True)
    save_env_value("NAPCAT_REQUIRE_MENTION", "true" if require_mention else "false")

    home = prompt(
        "Home channel for cron delivery (e.g. private:123456 or group:789)",
        default=get_env_value("NAPCAT_HOME_CHANNEL") or "",
    )
    if home:
        save_env_value("NAPCAT_HOME_CHANNEL", home.strip())

    print()
    print_success("NapCat configuration saved to ~/.hermes/.env")
    print_info("Restart the gateway for changes to take effect: hermes gateway restart")


def register(ctx):
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="napcat",
        label="QQ (NapCat)",
        adapter_factory=lambda cfg: NapCatAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["NAPCAT_HTTP_API"],
        install_hint="pip install aiohttp  (already bundled with hermes[messaging])",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="NAPCAT_HOME_CHANNEL",
        allowed_users_env="NAPCAT_ALLOWED_USERS",
        allow_all_env="NAPCAT_ALLOW_ALL_USERS",
        max_message_length=QQ_TEXT_LIMIT,
        emoji="🐧",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting on QQ via NapCat (OneBot 11). QQ supports plain text, "
            "images, voice and short videos but does NOT render markdown — strip "
            "markdown formatting and prefer concise prose. Long messages are auto-"
            "split at ~4500 characters. In groups, you only see messages that "
            "@mention you; in DMs every message reaches you. Address users by their "
            "card/nickname when possible; @<qqNumber> tags show who was originally "
            "addressed in the incoming message."
        ),
    )

    # Register ~55 agent tools so the LLM can drive QQ natively:
    # like / poke / mute / kick / pin / OCR / upload / forward / etc.
    # Tool registration is best-effort: a failure here must not stop the
    # platform adapter from loading.
    try:
        from .tools import register_all_tools

        count = register_all_tools()
        logger.info("NapCat: registered %d QQ agent tools", count)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("NapCat: failed to register agent tools: %s", exc)
