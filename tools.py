"""
NapCat (OneBot 11) Agent Tools for Hermes Agent.

Registers ~50 QQ-control tools so the LLM can drive QQ natively:
like/poke users, manage groups (mute/kick/admin), pin messages, OCR images,
upload files, fetch history, react with emoji, etc.

All tools share a single ``_NapCatClient`` (lazy ``aiohttp.ClientSession``)
and read connection info from env vars at first call so they survive a
gateway restart without re-registering.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)

TOOLSET = "qq-napcat"
DEFAULT_TIMEOUT_S = 15.0
UPLOAD_TIMEOUT_S = 60.0


# ---------------------------------------------------------------------------
# HTTP client (lazy singleton, env-driven)
# ---------------------------------------------------------------------------


class _NapCatClient:
    """Lazy aiohttp ClientSession + OneBot HTTP API helper."""

    def __init__(self) -> None:
        self._session: Optional[Any] = None

    def _read_config(self) -> tuple[str, str]:
        http_api = (os.getenv("NAPCAT_HTTP_API") or "").rstrip("/")
        token = os.getenv("NAPCAT_ACCESS_TOKEN") or ""
        if not http_api:
            raise RuntimeError(
                "NAPCAT_HTTP_API env var not set — configure NapCat HTTP API base URL"
            )
        return http_api, token

    async def _get_session(self):
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_S)
            )
        return self._session

    async def call(
        self,
        action: str,
        params: Dict[str, Any] | None = None,
        *,
        timeout_s: Optional[float] = None,
    ) -> Any:
        """POST to NapCat HTTP API; return ``data`` field on success."""
        import aiohttp

        http_api, token = self._read_config()
        url = f"{http_api}/{action}"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        session = await self._get_session()
        req_timeout = (
            aiohttp.ClientTimeout(total=timeout_s) if timeout_s else None
        )
        async with session.post(
            url,
            data=json.dumps(params or {}),
            headers=headers,
            timeout=req_timeout,
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
                f"OneBot API {action} failed: "
                f"retcode={payload.get('retcode')} status={payload.get('status')} "
                f"msg={payload.get('message') or payload.get('msg') or ''}"
            )
        return payload.get("data")


_client = _NapCatClient()


# ---------------------------------------------------------------------------
# Schema + handler helpers
# ---------------------------------------------------------------------------


def _schema(
    name: str,
    description: str,
    properties: Dict[str, Dict[str, Any]],
    required: List[str],
) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def _num(desc: str, **extra: Any) -> Dict[str, Any]:
    return {"type": "number", "description": desc, **extra}


def _str(desc: str, **extra: Any) -> Dict[str, Any]:
    return {"type": "string", "description": desc, **extra}


def _bool(desc: str) -> Dict[str, Any]:
    return {"type": "boolean", "description": desc}


def _arr(desc: str, item_type: str = "object") -> Dict[str, Any]:
    return {
        "type": "array",
        "description": desc,
        "items": {"type": item_type},
    }


def _ok(text: str) -> str:
    return text


def _err(exc: Exception) -> str:
    return f"Error: {type(exc).__name__}: {exc}"


def _check() -> bool:
    return bool(os.getenv("NAPCAT_HTTP_API"))


def _register(
    name: str,
    description: str,
    properties: Dict[str, Dict[str, Any]],
    required: List[str],
    handler: Callable[[Dict[str, Any]], Awaitable[str]],
    *,
    emoji: str = "🐧",
) -> None:
    """Convenience wrapper around ``registry.register`` for QQ tools."""
    registry.register(
        name=name,
        toolset=TOOLSET,
        schema=_schema(name, description, properties, required),
        handler=lambda args, **kw: handler(args),
        check_fn=_check,
        requires_env=["NAPCAT_HTTP_API"],
        is_async=True,
        description=description,
        emoji=emoji,
    )


def _parse_target(target: str) -> tuple[bool, int]:
    """Parse 'group:123' / 'private:456' → (is_group, id)."""
    raw = (target or "").strip().removeprefix("napcat:")
    if raw.startswith("group:"):
        return True, int(raw[6:])
    raw = raw.removeprefix("private:")
    return False, int(raw)


def register_all_tools() -> int:
    """Register every NapCat agent tool. Returns the count registered."""
    _register_query_tools()
    _register_interaction_tools()
    _register_message_tools()
    _register_history_tools()
    _register_essence_tools()
    _register_friend_tools()
    _register_group_admin_tools()
    _register_group_notice_tools()
    _register_group_file_tools()
    _register_request_tools()
    _register_misc_tools()
    _register_napcat_extension_tools()
    _register_hermes_napcat_composite()
    return len(registry.get_tool_names_for_toolset(TOOLSET))


def _register_hermes_napcat_composite() -> None:
    """Expose qq_* tools to hermes via a ``hermes-napcat`` composite toolset.

    Hermes resolves the default toolset for a *plugin* platform as
    ``hermes-<platform>`` (see ``hermes_cli.tools_config._get_platform_tools``).
    Without a ``hermes-napcat`` composite, the gateway never asks the registry
    for our ``qq-napcat`` tools and the LLM stays blind to them.

    We mutate ``toolsets.TOOLSETS`` in-process to add:
      - ``hermes-napcat``  →  Hermes core tools + every qq_* tool
      - registers ``hermes-napcat`` as an alias to ``qq-napcat`` in the
        tool registry, so cron / send_message paths also resolve it

    Failures here are non-fatal (the qq-napcat toolset itself is still
    registered) — they just mean the user has to enable tools manually.
    """
    qq_tool_names = list(registry.get_tool_names_for_toolset(TOOLSET))

    try:
        import toolsets as _ts_mod
    except Exception as exc:  # pragma: no cover
        logger.warning("NapCat: cannot import hermes toolsets module: %s", exc)
        return

    core_tools: List[str] = []
    for attr in ("_HERMES_CORE_TOOLS", "HERMES_CORE_TOOLS"):
        if hasattr(_ts_mod, attr):
            core_tools = list(getattr(_ts_mod, attr))
            break

    combined = list(dict.fromkeys(core_tools + qq_tool_names))

    composite = {
        "description": "NapCat (QQ via OneBot 11) toolset — Hermes core + "
                       f"{len(qq_tool_names)} QQ control tools",
        "tools": combined,
        "includes": [],
    }

    if hasattr(_ts_mod, "TOOLSETS") and isinstance(_ts_mod.TOOLSETS, dict):
        _ts_mod.TOOLSETS["hermes-napcat"] = composite
        # Also pull napcat into the union "hermes-gateway" toolset so users
        # whose config still points at hermes-gateway pick up qq_* tools.
        gw = _ts_mod.TOOLSETS.get("hermes-gateway")
        if isinstance(gw, dict):
            includes = list(gw.get("includes") or [])
            if "hermes-napcat" not in includes:
                includes.append("hermes-napcat")
                gw["includes"] = includes

    try:
        registry.register_toolset_alias("hermes-napcat", TOOLSET)
    except Exception:
        pass

    logger.info(
        "NapCat: registered hermes-napcat composite toolset "
        "(%d core + %d qq tools)",
        len(core_tools), len(qq_tool_names),
    )


# ---------------------------------------------------------------------------
# Batch 1: Query tools (user / group / list / honor)
# ---------------------------------------------------------------------------


def _register_query_tools() -> None:
    # qq_get_user_info
    async def _get_user_info(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_stranger_info",
                                   {"user_id": a["user_id"], "no_cache": True})
            lines = [
                f"QQ: {d.get('user_id')}",
                f"Nickname: {d.get('nickname', 'unknown')}",
            ]
            for key, label in (("sex", "Sex"), ("age", "Age"), ("sign", "Signature"),
                               ("level", "Level"), ("login_days", "Login days"),
                               ("qid", "QID")):
                v = d.get(key)
                if v:
                    lines.append(f"{label}: {v}")
            return "\n".join(lines)
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_user_info",
        "Get a QQ user's profile info (nickname, age, sex, signature, level). "
        "When a user @mentions someone, extract the QQ number from @QQNumber.",
        {"user_id": _num("Target QQ number")},
        ["user_id"],
        _get_user_info,
        emoji="👤",
    )

    # qq_get_group_info
    async def _get_group_info(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_group_info",
                                   {"group_id": a["group_id"], "no_cache": True})
            lines = [
                f"Group: {d.get('group_id')}",
                f"Name: {d.get('group_name', 'unknown')}",
            ]
            if d.get("member_count"):
                lines.append(f"Members: {d['member_count']}")
            if d.get("max_member_count"):
                lines.append(f"Max members: {d['max_member_count']}")
            return "\n".join(lines)
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_group_info",
        "Get QQ group information including name and member count.",
        {"group_id": _num("Target group number")},
        ["group_id"],
        _get_group_info,
        emoji="👥",
    )

    # qq_get_group_member_info
    async def _get_group_member_info(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_group_member_info",
                                   {"group_id": a["group_id"],
                                    "user_id": a["user_id"],
                                    "no_cache": True})
            from datetime import datetime
            lines = [
                f"QQ: {d.get('user_id')}",
                f"Nickname: {d.get('nickname', 'unknown')}",
            ]
            for key, label in (("card", "Card"), ("role", "Role"),
                               ("title", "Title"), ("level", "Level")):
                v = d.get(key)
                if v:
                    lines.append(f"{label}: {v}")
            for key, label in (("join_time", "Joined"),
                               ("last_sent_time", "Last active")):
                v = d.get(key)
                if v:
                    lines.append(f"{label}: {datetime.fromtimestamp(int(v)).isoformat()}")
            return "\n".join(lines)
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_group_member_info",
        "Get a specific member's info in a QQ group (card, role, join time, etc).",
        {"group_id": _num("Group number"), "user_id": _num("Target QQ number")},
        ["group_id", "user_id"],
        _get_group_member_info,
        emoji="🪪",
    )

    # qq_get_friend_list
    async def _get_friend_list(_: Dict[str, Any]) -> str:
        try:
            friends = await _client.call("get_friend_list", {}) or []
            lines = [
                f"{f.get('user_id')} | {f.get('nickname', '')}"
                + (f" ({f['remark']})" if f.get("remark") else "")
                for f in friends
            ]
            return f"Friends ({len(friends)}):\n" + "\n".join(lines)
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_friend_list",
        "Get the bot's full friend list (QQ number, nickname, remark).",
        {},
        [],
        _get_friend_list,
        emoji="📇",
    )

    # qq_get_group_list
    async def _get_group_list(_: Dict[str, Any]) -> str:
        try:
            groups = await _client.call("get_group_list", {}) or []
            lines = [
                f"{g.get('group_id')} | {g.get('group_name', 'unknown')} "
                f"| {g.get('member_count', '?')} members"
                for g in groups
            ]
            return f"Groups ({len(groups)}):\n" + "\n".join(lines)
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_group_list",
        "Get the bot's full group list (group ID, name, member count).",
        {},
        [],
        _get_group_list,
        emoji="📋",
    )

    # qq_get_group_member_list
    async def _get_group_member_list(a: Dict[str, Any]) -> str:
        try:
            members = await _client.call("get_group_member_list",
                                         {"group_id": a["group_id"]}) or []
            lines = [
                f"{m.get('user_id')} | "
                f"{m.get('card') or m.get('nickname', 'unknown')} | "
                f"{m.get('role', 'member')}"
                for m in members
            ]
            return (f"Group {a['group_id']} members ({len(members)}):\n"
                    + "\n".join(lines))
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_group_member_list",
        "Get the full member list of a QQ group "
        "(useful for resolving @QQNumber mentions to real names).",
        {"group_id": _num("Group number")},
        ["group_id"],
        _get_group_member_list,
        emoji="👨‍👩‍👧‍👦",
    )

    # qq_get_group_honor_info
    async def _get_group_honor_info(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_group_honor_info",
                                   {"group_id": a["group_id"],
                                    "type": a.get("type", "all")})
            sections = [f"Group {a['group_id']} Honor Info:"]
            t = d.get("current_talkative") if isinstance(d, dict) else None
            if t:
                sections.append(
                    f"Dragon King: {t.get('nickname') or t.get('user_id')} "
                    f"({t.get('day_count')} days)"
                )
            for key in ("talkative_list", "performer_list", "legend_list",
                        "strong_newbie_list", "emotion_list"):
                lst = (d or {}).get(key) or []
                if lst:
                    label = key.replace("_list", "").replace("_", " ")
                    sections.append(f"\n{label}:")
                    for e in lst[:10]:
                        sections.append(
                            f"  {e.get('nickname') or e.get('user_id')} — "
                            f"{e.get('description', '')}"
                        )
            return "\n".join(sections)
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_group_honor_info",
        "Get group honor info (talkative, performer, legend, strong_newbie, "
        "emotion). Use type='all' for everything.",
        {
            "group_id": _num("Group number"),
            "type": _str("Honor type: talkative, performer, legend, "
                         "strong_newbie, emotion, or all"),
        },
        ["group_id"],
        _get_group_honor_info,
        emoji="🏆",
    )

    # qq_get_group_at_all_remain
    async def _get_at_all_remain(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_group_at_all_remain",
                                   {"group_id": a["group_id"]})
            return (
                f"Group {a['group_id']} @all remain: "
                f"can_at_all={d.get('can_at_all')}, "
                f"group_remain={d.get('remain_at_all_count_for_group')}, "
                f"my_remain={d.get('remain_at_all_count_for_uin')}"
            )
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_group_at_all_remain",
        "Get remaining @all uses today in a group.",
        {"group_id": _num("Group number")},
        ["group_id"],
        _get_at_all_remain,
        emoji="📢",
    )


# ---------------------------------------------------------------------------
# Batch 2: Interaction tools (like, poke, recall, react, OCR, translate, mark read)
# ---------------------------------------------------------------------------


def _register_interaction_tools() -> None:
    # qq_like_user
    async def _like(a: Dict[str, Any]) -> str:
        try:
            times = int(a.get("times", 10))
            await _client.call(
                "send_like", {"user_id": a["user_id"], "times": times}
            )
            return f"Successfully liked user {a['user_id']} {times} time(s)."
        except Exception as e:
            return _err(e)

    _register(
        "qq_like_user",
        "Give a QQ user a thumbs-up (like). Provide target QQ number and "
        "times (1-10). When a user @mentions someone, extract the QQ number "
        "from @QQNumber.",
        {
            "user_id": _num("Target QQ number"),
            "times": _num("Number of likes, 1-10, default 10",
                          minimum=1, maximum=10),
        },
        ["user_id"],
        _like,
        emoji="👍",
    )

    # qq_poke
    async def _poke(a: Dict[str, Any]) -> str:
        try:
            user_id = a["user_id"]
            group_id = a.get("group_id")
            if group_id:
                await _client.call("group_poke",
                                   {"group_id": group_id, "user_id": user_id})
                return f"Poked user {user_id} in group {group_id}."
            await _client.call("friend_poke", {"user_id": user_id})
            return f"Poked user {user_id}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_poke",
        "Send a poke (nudge) to a QQ user in a group or private chat.",
        {
            "user_id": _num("Target QQ number to poke"),
            "group_id": _num("Group number (omit for private poke)"),
        },
        ["user_id"],
        _poke,
        emoji="👉",
    )

    # qq_recall_message
    async def _recall(a: Dict[str, Any]) -> str:
        try:
            await _client.call("delete_msg", {"message_id": a["message_id"]})
            return f"Message {a['message_id']} recalled."
        except Exception as e:
            return _err(e)

    _register(
        "qq_recall_message",
        "Recall (unsend) a message by its message ID.",
        {"message_id": _num("Message ID to recall")},
        ["message_id"],
        _recall,
        emoji="↩️",
    )

    # qq_set_msg_emoji_like
    async def _emoji_react(a: Dict[str, Any]) -> str:
        try:
            await _client.call(
                "set_msg_emoji_like",
                {"message_id": a["message_id"], "emoji_id": a["emoji_id"]},
            )
            return f"Emoji reaction added to message {a['message_id']}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_set_msg_emoji_like",
        "Add an emoji reaction to a message. Common emoji_id values: "
        "128077(thumbs up), 128078(thumbs down), 128079(clap), "
        "128512(laugh), 128525(heart eyes), 128557(cry), 128293(fire).",
        {
            "message_id": _num("Message ID to react to"),
            "emoji_id": _num("Emoji ID (unicode code point as number)"),
        },
        ["message_id", "emoji_id"],
        _emoji_react,
        emoji="🎭",
    )

    # qq_ocr_image
    async def _ocr(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("ocr_image", {"image": a["image"]})
            texts = []
            if isinstance(d, dict):
                src = d.get("texts") or d.get("text_detections") or []
                texts = [t.get("text", "") for t in src]
            elif isinstance(d, list):
                texts = [t.get("text", "") for t in d if isinstance(t, dict)]
            joined = "\n".join(t for t in texts if t)
            return joined or "No text detected."
        except Exception as e:
            return _err(e)

    _register(
        "qq_ocr_image",
        "Perform OCR text recognition on an image. Provide image path/URL/file_id.",
        {"image": _str("Image file path, URL, or file ID")},
        ["image"],
        _ocr,
        emoji="🔍",
    )

    # qq_translate_en2zh
    async def _translate(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("translate_en2zh", {"text": a["text"]})
            if isinstance(d, dict):
                return str(d.get("result") or d.get("text") or json.dumps(d))
            return str(d) if d else "(no translation)"
        except Exception as e:
            return _err(e)

    _register(
        "qq_translate_en2zh",
        "Translate English text to Chinese using QQ's built-in translation.",
        {"text": _str("English text to translate")},
        ["text"],
        _translate,
        emoji="🌐",
    )

    # qq_mark_msg_as_read
    async def _mark_read(a: Dict[str, Any]) -> str:
        try:
            is_group, id_ = _parse_target(a["target"])
            if is_group:
                await _client.call("mark_group_msg_as_read",
                                   {"group_id": id_})
            else:
                await _client.call("mark_private_msg_as_read",
                                   {"user_id": id_})
            return f"Messages marked as read for {a['target']}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_mark_msg_as_read",
        "Mark messages as read. target format: 'group:<group_id>' or 'private:<user_id>'.",
        {"target": _str("Target: 'group:<group_id>' or 'private:<user_id>'")},
        ["target"],
        _mark_read,
        emoji="📖",
    )


# ---------------------------------------------------------------------------
# Batch 3: Messaging tools (send/upload/forward)
# ---------------------------------------------------------------------------


def _register_message_tools() -> None:
    # qq_send_message
    def _looks_like_url(s: str) -> bool:
        s = (s or "").strip().lower()
        return s.startswith(("http://", "https://", "file://", "base64://"))

    def _validate_media(url: str, kind: str) -> Optional[str]:
        """Return an error string if ``url`` is unusable, else None."""
        if not url or not url.strip():
            return None
        url = url.strip()
        if _looks_like_url(url):
            return None  # remote URL — let NapCat fetch it
        # Treat as local path: it MUST exist where NapCat can read it.
        import os.path
        if not os.path.isabs(url):
            return (
                f"qq_send_message rejected: {kind} path '{url}' is not absolute. "
                "Provide a public http(s) URL, or an absolute file path that "
                "exists on the NapCat host. DO NOT invent placeholder filenames."
            )
        if not os.path.exists(url):
            return (
                f"qq_send_message rejected: {kind} file '{url}' does not exist "
                "on the hermes host. If you don't actually have a media file, "
                "drop the {kind}_url parameter and send only text. NEVER make "
                "up filenames hoping NapCat will produce a video for you — it "
                "won't, and the user will see an empty [视频] / [图片] / [语音]."
            ).format(kind=kind)
        return None

    async def _send_message(a: Dict[str, Any]) -> str:
        try:
            is_group, id_ = _parse_target(a["target"])
            action = "send_group_msg" if is_group else "send_private_msg"
            key = "group_id" if is_group else "user_id"

            # Pre-validate any local file paths so the LLM gets an immediate,
            # honest failure instead of NapCat half-sending a broken bubble.
            for kind, url in (
                ("image", a.get("image_url")),
                ("voice", a.get("voice_url")),
                ("video", a.get("video_url")),
            ):
                err = _validate_media(url, kind)
                if err:
                    return err

            sent_parts: List[str] = []

            # text + image can be combined
            text_img_segs: List[Dict[str, Any]] = []
            if a.get("image_url"):
                text_img_segs.append({"type": "image",
                                      "data": {"file": a["image_url"]}})
            if a.get("text"):
                text_img_segs.append({"type": "text",
                                      "data": {"text": a["text"]}})
            if text_img_segs:
                await _client.call(action, {key: id_, "message": text_img_segs})
                sent_parts.append("text/image")

            # voice (must be sent alone)
            if a.get("voice_url"):
                await _client.call(action, {
                    key: id_,
                    "message": [{"type": "record",
                                 "data": {"file": a["voice_url"]}}],
                })
                sent_parts.append("voice")

            # video (must be sent alone)
            if a.get("video_url"):
                await _client.call(action, {
                    key: id_,
                    "message": [{"type": "video",
                                 "data": {"file": a["video_url"]}}],
                })
                sent_parts.append("video")

            if not sent_parts:
                return f"Nothing to send (no text/image/voice/video provided)."
            return f"Sent {', '.join(sent_parts)} to {a['target']}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_send_message",
        "Send a message to a specific QQ user or group. Supports text, image, "
        "voice, video. target format: 'group:<group_id>' or 'private:<user_id>'. "
        "Pass at least one of text / image_url / voice_url / video_url. "
        "IMPORTANT: media URLs/paths MUST point to a real, reachable resource "
        "— either a public http(s) URL, or a file path that NapCat's process "
        "can read on disk. DO NOT invent placeholder paths like "
        "'/tmp/random_video.mp4' — if you don't actually have a media file, "
        "leave that parameter empty and send only text. To use a file produced "
        "by another tool (e.g. download_file), pass the exact path that tool "
        "returned, not a guess.",
        {
            "target": _str("Target: 'group:<group_id>' or 'private:<user_id>'"),
            "text": _str("Text content (optional)"),
            "image_url": _str(
                "Public image URL (http/https) or an absolute path NapCat can "
                "read. Omit unless you have a real resource."
            ),
            "voice_url": _str(
                "Public voice URL (http/https) or absolute path NapCat can "
                "read — sent as QQ voice message. Omit unless real."
            ),
            "video_url": _str(
                "Public video URL (http/https) or absolute path NapCat can "
                "read. Omit unless real."
            ),
        },
        ["target"],
        _send_message,
        emoji="✉️",
    )

    # qq_upload_file
    async def _upload_file(a: Dict[str, Any]) -> str:
        try:
            is_group, id_ = _parse_target(a["target"])
            action = "upload_group_file" if is_group else "upload_private_file"
            key = "group_id" if is_group else "user_id"
            await _client.call(
                action,
                {key: id_, "file": a["file"], "name": a["name"]},
                timeout_s=UPLOAD_TIMEOUT_S,
            )
            return f"File '{a['name']}' uploaded to {a['target']}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_upload_file",
        "Upload a file (pdf, doc, zip, mp3, …) to a QQ group or private chat. "
        "target format: 'group:<group_id>' or 'private:<user_id>'.",
        {
            "target": _str("Target: 'group:<group_id>' or 'private:<user_id>'"),
            "file": _str("File URL or local file path"),
            "name": _str("Display file name, e.g. 'report.pdf'"),
        },
        ["target", "file", "name"],
        _upload_file,
        emoji="📎",
    )

    # qq_forward_message
    async def _forward(a: Dict[str, Any]) -> str:
        try:
            is_group, id_ = _parse_target(a["target"])
            action = "send_group_msg" if is_group else "send_private_msg"
            key = "group_id" if is_group else "user_id"
            await _client.call(action, {
                key: id_,
                "message": [{"type": "forward",
                             "data": {"id": str(a["message_id"])}}],
            })
            return f"Message {a['message_id']} forwarded to {a['target']}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_forward_message",
        "Forward a message by message_id to another QQ group or user. "
        "target format: 'group:<group_id>' or 'private:<user_id>'.",
        {
            "message_id": _num("Message ID to forward"),
            "target": _str("Target: 'group:<group_id>' or 'private:<user_id>'"),
        },
        ["message_id", "target"],
        _forward,
        emoji="➡️",
    )

    # qq_send_group_forward_msg
    async def _send_group_forward(a: Dict[str, Any]) -> str:
        try:
            await _client.call("send_group_forward_msg", {
                "group_id": a["group_id"],
                "messages": a["messages"],
            })
            return f"Merged forward sent to group {a['group_id']}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_send_group_forward_msg",
        "Send a merged forward message (multiple messages combined) to a group. "
        "Each node: {type:'node', data:{name:'sender', uin:'QQ', "
        "content:[{type:'text', data:{text:'…'}}]}}.",
        {
            "group_id": _num("Group number"),
            "messages": _arr("Array of forward message node objects"),
        },
        ["group_id", "messages"],
        _send_group_forward,
        emoji="📨",
    )

    # qq_send_private_forward_msg
    async def _send_private_forward(a: Dict[str, Any]) -> str:
        try:
            await _client.call("send_private_forward_msg", {
                "user_id": a["user_id"],
                "messages": a["messages"],
            })
            return f"Merged forward sent to user {a['user_id']}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_send_private_forward_msg",
        "Send a merged forward message (multiple messages combined) to a "
        "private chat. Same node format as send_group_forward_msg.",
        {
            "user_id": _num("Target QQ number"),
            "messages": _arr("Array of forward message node objects"),
        },
        ["user_id", "messages"],
        _send_private_forward,
        emoji="📨",
    )

    # qq_download_file
    async def _download_file(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("download_file", {
                "url": a["url"],
                "thread_count": int(a.get("thread_count", 1)),
                "headers": [],
            }, timeout_s=UPLOAD_TIMEOUT_S)
            return f"File downloaded to: {(d or {}).get('file', 'unknown')}"
        except Exception as e:
            return _err(e)

    _register(
        "qq_download_file",
        "Download a file from a URL to NapCat's local storage. Returns the local path.",
        {
            "url": _str("URL of the file to download"),
            "thread_count": _num("Number of download threads (default 1)"),
        },
        ["url"],
        _download_file,
        emoji="⬇️",
    )


# ---------------------------------------------------------------------------
# Batch 4: History + essence + friend tools
# ---------------------------------------------------------------------------


def _register_history_tools() -> None:
    # qq_get_group_msg_history
    async def _group_history(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_group_msg_history", {
                "group_id": a["group_id"],
                "count": int(a.get("count", 20)),
            }) or {}
            msgs = d.get("messages") or []
            lines = []
            for m in msgs:
                sender = m.get("sender") or {}
                name = (sender.get("card") or sender.get("nickname")
                        or sender.get("user_id") or "unknown")
                raw = str(m.get("raw_message", ""))[:200]
                lines.append(f"[{m.get('message_id')}] {name}: {raw}")
            return (f"Group {a['group_id']} history ({len(msgs)}):\n"
                    + "\n".join(lines))
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_group_msg_history",
        "Get recent message history from a QQ group (last N messages).",
        {
            "group_id": _num("Group number"),
            "count": _num("Messages to retrieve (default 20, max 100)"),
        },
        ["group_id"],
        _group_history,
        emoji="📜",
    )

    # qq_get_friend_msg_history
    async def _friend_history(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_friend_msg_history", {
                "user_id": a["user_id"],
                "count": int(a.get("count", 20)),
            }) or {}
            msgs = d.get("messages") or []
            lines = []
            for m in msgs:
                sender = m.get("sender") or {}
                name = (sender.get("nickname")
                        or sender.get("user_id") or "unknown")
                raw = str(m.get("raw_message", ""))[:200]
                lines.append(f"[{m.get('message_id')}] {name}: {raw}")
            return (f"Friend {a['user_id']} history ({len(msgs)}):\n"
                    + "\n".join(lines))
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_friend_msg_history",
        "Get recent message history from a private/friend chat.",
        {
            "user_id": _num("Friend QQ number"),
            "count": _num("Messages to retrieve (default 20, max 100)"),
        },
        ["user_id"],
        _friend_history,
        emoji="📜",
    )


def _register_essence_tools() -> None:
    # qq_get_essence_msg_list
    async def _get_essence(a: Dict[str, Any]) -> str:
        try:
            lst = await _client.call("get_essence_msg_list",
                                     {"group_id": a["group_id"]}) or []
            lines = []
            for e in lst:
                name = e.get("sender_nick") or e.get("sender_id") or "unknown"
                content = str(e.get("content") or e.get("message_seq") or "")[:150]
                lines.append(f"[{e.get('message_id')}] {name}: {content}")
            return (f"Group {a['group_id']} essence messages ({len(lst)}):\n"
                    + "\n".join(lines))
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_essence_msg_list",
        "Get the list of pinned/essence messages in a QQ group.",
        {"group_id": _num("Group number")},
        ["group_id"],
        _get_essence,
        emoji="📌",
    )

    # qq_set_essence_msg
    async def _set_essence(a: Dict[str, Any]) -> str:
        try:
            await _client.call("set_essence_msg",
                               {"message_id": a["message_id"]})
            return f"Message {a['message_id']} pinned as essence."
        except Exception as e:
            return _err(e)

    _register(
        "qq_set_essence_msg",
        "Pin a message as an essence/pinned message in a QQ group.",
        {"message_id": _num("Message ID to pin")},
        ["message_id"],
        _set_essence,
        emoji="📌",
    )

    # qq_delete_essence_msg
    async def _del_essence(a: Dict[str, Any]) -> str:
        try:
            await _client.call("delete_essence_msg",
                               {"message_id": a["message_id"]})
            return f"Message {a['message_id']} removed from essence."
        except Exception as e:
            return _err(e)

    _register(
        "qq_delete_essence_msg",
        "Remove a message from the essence/pinned list.",
        {"message_id": _num("Message ID to unpin")},
        ["message_id"],
        _del_essence,
        emoji="❌",
    )


def _register_friend_tools() -> None:
    # qq_set_friend_remark
    async def _set_friend_remark(a: Dict[str, Any]) -> str:
        try:
            await _client.call("set_friend_remark", {
                "user_id": a["user_id"],
                "remark": a["remark"],
            })
            return f"Friend {a['user_id']} remark set to '{a['remark']}'."
        except Exception as e:
            return _err(e)

    _register(
        "qq_set_friend_remark",
        "Set a remark/alias for a friend.",
        {
            "user_id": _num("Friend QQ number"),
            "remark": _str("New remark name"),
        },
        ["user_id", "remark"],
        _set_friend_remark,
        emoji="✏️",
    )

    # qq_delete_friend
    async def _del_friend(a: Dict[str, Any]) -> str:
        try:
            await _client.call("delete_friend", {"user_id": a["user_id"]})
            return f"Friend {a['user_id']} deleted."
        except Exception as e:
            return _err(e)

    _register(
        "qq_delete_friend",
        "Delete (remove) a friend from the bot's friend list. Use with caution.",
        {"user_id": _num("Friend QQ number to delete")},
        ["user_id"],
        _del_friend,
        emoji="🗑️",
    )


# ---------------------------------------------------------------------------
# Batch 5: Group admin tools
# ---------------------------------------------------------------------------


def _register_group_admin_tools() -> None:
    # qq_mute_group_member
    async def _mute(a: Dict[str, Any]) -> str:
        try:
            duration = int(a.get("duration", 600))
            await _client.call("set_group_ban", {
                "group_id": a["group_id"],
                "user_id": a["user_id"],
                "duration": duration,
            })
            action = "unmuted" if duration == 0 else f"muted for {duration}s"
            return f"User {a['user_id']} {action} in group {a['group_id']}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_mute_group_member",
        "Mute (ban) a member in a QQ group for a duration in seconds. "
        "Set duration=0 to unmute.",
        {
            "group_id": _num("Group number"),
            "user_id": _num("Target QQ number to mute"),
            "duration": _num("Mute duration in seconds (0 = unmute, default 600)",
                             minimum=0),
        },
        ["group_id", "user_id"],
        _mute,
        emoji="🔇",
    )

    # qq_kick_group_member
    async def _kick(a: Dict[str, Any]) -> str:
        try:
            await _client.call("set_group_kick", {
                "group_id": a["group_id"],
                "user_id": a["user_id"],
                "reject_add_request": bool(a.get("reject_add_request", False)),
            })
            return (f"User {a['user_id']} kicked from group "
                    f"{a['group_id']}.")
        except Exception as e:
            return _err(e)

    _register(
        "qq_kick_group_member",
        "Remove (kick) a member from a QQ group.",
        {
            "group_id": _num("Group number"),
            "user_id": _num("Target QQ number to kick"),
            "reject_add_request": _bool("Reject future join requests from this user"),
        },
        ["group_id", "user_id"],
        _kick,
        emoji="🦶",
    )

    # qq_set_group_card
    async def _set_card(a: Dict[str, Any]) -> str:
        try:
            await _client.call("set_group_card", {
                "group_id": a["group_id"],
                "user_id": a["user_id"],
                "card": a.get("card", ""),
            })
            return (f"Set user {a['user_id']}'s card to '{a.get('card', '')}' "
                    f"in group {a['group_id']}.")
        except Exception as e:
            return _err(e)

    _register(
        "qq_set_group_card",
        "Set a member's card (display name) in a QQ group. "
        "Pass card='' to clear.",
        {
            "group_id": _num("Group number"),
            "user_id": _num("Target QQ number"),
            "card": _str("New card name (empty string to clear)"),
        },
        ["group_id", "user_id", "card"],
        _set_card,
        emoji="🏷️",
    )

    # qq_set_group_admin
    async def _set_admin(a: Dict[str, Any]) -> str:
        try:
            await _client.call("set_group_admin", {
                "group_id": a["group_id"],
                "user_id": a["user_id"],
                "enable": bool(a["enable"]),
            })
            verb = "promoted to admin" if a["enable"] else "removed from admin"
            return f"User {a['user_id']} {verb} in group {a['group_id']}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_set_group_admin",
        "Set or unset a member as group admin in a QQ group.",
        {
            "group_id": _num("Group number"),
            "user_id": _num("Target QQ number"),
            "enable": _bool("true = set as admin, false = remove admin"),
        },
        ["group_id", "user_id", "enable"],
        _set_admin,
        emoji="🛡️",
    )

    # qq_set_group_name
    async def _set_group_name(a: Dict[str, Any]) -> str:
        try:
            await _client.call("set_group_name", {
                "group_id": a["group_id"],
                "group_name": a["group_name"],
            })
            return f"Group {a['group_id']} name changed to '{a['group_name']}'."
        except Exception as e:
            return _err(e)

    _register(
        "qq_set_group_name",
        "Change the name of a QQ group.",
        {
            "group_id": _num("Group number"),
            "group_name": _str("New group name"),
        },
        ["group_id", "group_name"],
        _set_group_name,
        emoji="📝",
    )

    # qq_set_group_whole_ban
    async def _whole_ban(a: Dict[str, Any]) -> str:
        try:
            await _client.call("set_group_whole_ban", {
                "group_id": a["group_id"],
                "enable": bool(a["enable"]),
            })
            verb = "enabled" if a["enable"] else "disabled"
            return f"Whole-group mute {verb} for group {a['group_id']}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_set_group_whole_ban",
        "Enable or disable whole-group mute (all members muted; only admins "
        "and owners can speak when enabled).",
        {
            "group_id": _num("Group number"),
            "enable": _bool("true = mute all, false = unmute all"),
        },
        ["group_id", "enable"],
        _whole_ban,
        emoji="🤐",
    )

    # qq_set_group_special_title
    async def _special_title(a: Dict[str, Any]) -> str:
        try:
            await _client.call("set_group_special_title", {
                "group_id": a["group_id"],
                "user_id": a["user_id"],
                "special_title": a.get("special_title", ""),
                "duration": -1,
            })
            t = a.get("special_title", "")
            if t:
                return (f"Set user {a['user_id']}'s special title to "
                        f"'{t}' in group {a['group_id']}.")
            return (f"Cleared user {a['user_id']}'s special title in "
                    f"group {a['group_id']}.")
        except Exception as e:
            return _err(e)

    _register(
        "qq_set_group_special_title",
        "Set a member's special title (exclusive tag) in a QQ group. "
        "Group owner only.",
        {
            "group_id": _num("Group number"),
            "user_id": _num("Target QQ number"),
            "special_title": _str("Special title text (empty to clear)"),
        },
        ["group_id", "user_id", "special_title"],
        _special_title,
        emoji="🎖️",
    )

    # qq_leave_group
    async def _leave_group(a: Dict[str, Any]) -> str:
        try:
            dismiss = bool(a.get("is_dismiss", False))
            await _client.call("set_group_leave", {
                "group_id": a["group_id"],
                "is_dismiss": dismiss,
            })
            suffix = " (dissolved)" if dismiss else ""
            return f"Left group {a['group_id']}{suffix}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_leave_group",
        "Make the bot leave (quit) a QQ group. "
        "If bot is owner, is_dismiss=true dissolves the group.",
        {
            "group_id": _num("Group number to leave"),
            "is_dismiss": _bool("true = dissolve (owner only), false = just leave"),
        },
        ["group_id"],
        _leave_group,
        emoji="🚪",
    )

    # qq_set_group_portrait
    async def _portrait(a: Dict[str, Any]) -> str:
        try:
            await _client.call("set_group_portrait", {
                "group_id": a["group_id"],
                "file": a["file"],
            })
            return f"Group {a['group_id']} portrait updated."
        except Exception as e:
            return _err(e)

    _register(
        "qq_set_group_portrait",
        "Set the group avatar/portrait. Provide image file path or URL.",
        {
            "group_id": _num("Group number"),
            "file": _str("Image file path or URL"),
        },
        ["group_id", "file"],
        _portrait,
        emoji="🖼️",
    )

    # qq_set_group_sign
    async def _group_sign(a: Dict[str, Any]) -> str:
        try:
            await _client.call("send_group_sign", {"group_id": a["group_id"]})
            return f"Signed in for group {a['group_id']}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_set_group_sign",
        "Perform a daily sign-in/check-in for the bot in a QQ group.",
        {"group_id": _num("Group number")},
        ["group_id"],
        _group_sign,
        emoji="✅",
    )

    # qq_set_group_remark
    async def _group_remark(a: Dict[str, Any]) -> str:
        try:
            await _client.call("set_group_remark", {
                "group_id": a["group_id"],
                "remark": a["remark"],
            })
            return f"Group {a['group_id']} remark set to '{a['remark']}'."
        except Exception as e:
            return _err(e)

    _register(
        "qq_set_group_remark",
        "Set a remark/note for a group (only visible to the bot).",
        {
            "group_id": _num("Group number"),
            "remark": _str("New remark text"),
        },
        ["group_id", "remark"],
        _group_remark,
        emoji="📌",
    )


# ---------------------------------------------------------------------------
# Batch 6: Group notice + files + requests + NapCat extensions
# ---------------------------------------------------------------------------


def _register_group_notice_tools() -> None:
    # qq_send_group_notice
    async def _send_notice(a: Dict[str, Any]) -> str:
        try:
            await _client.call("_send_group_notice", {
                "group_id": a["group_id"],
                "content": a["content"],
            })
            return f"Group notice sent to group {a['group_id']}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_send_group_notice",
        "Send a group announcement/notice in a QQ group. "
        "Requires admin or owner permission.",
        {
            "group_id": _num("Group number"),
            "content": _str("Announcement content text"),
        },
        ["group_id", "content"],
        _send_notice,
        emoji="📣",
    )

    # qq_get_group_notice
    async def _get_notices(a: Dict[str, Any]) -> str:
        try:
            notices = await _client.call("_get_group_notice",
                                         {"group_id": a["group_id"]}) or []
            lines = []
            for n in notices:
                msg = n.get("message") or {}
                text = msg.get("text") or n.get("content", "")
                lines.append(f"[{n.get('notice_id')}] {str(text)[:200]}")
            return (f"Group {a['group_id']} notices ({len(notices)}):\n"
                    + "\n".join(lines))
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_group_notice",
        "Get the list of announcements/notices in a QQ group.",
        {"group_id": _num("Group number")},
        ["group_id"],
        _get_notices,
        emoji="📋",
    )

    # qq_delete_group_notice
    async def _del_notice(a: Dict[str, Any]) -> str:
        try:
            await _client.call("_del_group_notice", {
                "group_id": a["group_id"],
                "notice_id": a["notice_id"],
            })
            return (f"Notice {a['notice_id']} deleted from group "
                    f"{a['group_id']}.")
        except Exception as e:
            return _err(e)

    _register(
        "qq_delete_group_notice",
        "Delete a specific announcement/notice from a QQ group.",
        {
            "group_id": _num("Group number"),
            "notice_id": _str("Notice ID to delete"),
        },
        ["group_id", "notice_id"],
        _del_notice,
        emoji="🗑️",
    )


def _register_group_file_tools() -> None:
    # qq_get_group_root_files
    async def _root_files(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_group_root_files",
                                   {"group_id": a["group_id"]}) or {}
            folders = d.get("folders") or []
            files = d.get("files") or []
            lines = [f"[Folder] {f.get('folder_name')} (id: {f.get('folder_id')})"
                     for f in folders]
            lines += [
                f"[File] {f.get('file_name')} "
                f"(id: {f.get('file_id')}, size: {f.get('file_size', '?')})"
                for f in files
            ]
            return (f"Group {a['group_id']} files:\n"
                    + ("\n".join(lines) if lines else "Empty"))
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_group_root_files",
        "Get the list of files and folders in a QQ group's root file directory.",
        {"group_id": _num("Group number")},
        ["group_id"],
        _root_files,
        emoji="📁",
    )

    # qq_get_group_file_url
    async def _file_url(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_group_file_url", {
                "group_id": a["group_id"],
                "file_id": a["file_id"],
                "busid": a["busid"],
            }) or {}
            return f"Download URL: {d.get('url', 'unknown')}"
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_group_file_url",
        "Get the download URL for a file in a QQ group. Requires file_id and "
        "busid from the file list.",
        {
            "group_id": _num("Group number"),
            "file_id": _str("File ID"),
            "busid": _num("Business ID of the file"),
        },
        ["group_id", "file_id", "busid"],
        _file_url,
        emoji="🔗",
    )

    # qq_create_group_file_folder
    async def _create_folder(a: Dict[str, Any]) -> str:
        try:
            await _client.call("create_group_file_folder", {
                "group_id": a["group_id"],
                "name": a["name"],
            })
            return f"Folder '{a['name']}' created in group {a['group_id']}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_create_group_file_folder",
        "Create a new folder in a QQ group's file system.",
        {
            "group_id": _num("Group number"),
            "name": _str("Folder name"),
        },
        ["group_id", "name"],
        _create_folder,
        emoji="📂",
    )

    # qq_delete_group_file
    async def _del_group_file(a: Dict[str, Any]) -> str:
        try:
            await _client.call("delete_group_file", {
                "group_id": a["group_id"],
                "file_id": a["file_id"],
                "busid": a["busid"],
            })
            return f"File deleted from group {a['group_id']}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_delete_group_file",
        "Delete a file from a QQ group's file system.",
        {
            "group_id": _num("Group number"),
            "file_id": _str("File ID to delete"),
            "busid": _num("Business ID of the file"),
        },
        ["group_id", "file_id", "busid"],
        _del_group_file,
        emoji="🗑️",
    )


def _register_request_tools() -> None:
    # qq_handle_friend_request
    async def _handle_friend_req(a: Dict[str, Any]) -> str:
        try:
            await _client.call("set_friend_add_request", {
                "flag": a["flag"],
                "approve": bool(a["approve"]),
                "remark": a.get("remark", ""),
            })
            return ("Friend request "
                    + ("approved." if a["approve"] else "rejected."))
        except Exception as e:
            return _err(e)

    _register(
        "qq_handle_friend_request",
        "Approve or reject a friend add request. Requires the request flag "
        "from the inbound event payload.",
        {
            "flag": _str("Request flag identifier"),
            "approve": _bool("true = approve, false = reject"),
            "remark": _str("Remark name for the friend (optional, when approving)"),
        },
        ["flag", "approve"],
        _handle_friend_req,
        emoji="🤝",
    )

    # qq_handle_group_request
    async def _handle_group_req(a: Dict[str, Any]) -> str:
        try:
            await _client.call("set_group_add_request", {
                "flag": a["flag"],
                "sub_type": a["sub_type"],
                "approve": bool(a["approve"]),
                "reason": a.get("reason", ""),
            })
            verb = "approved" if a["approve"] else "rejected"
            return f"Group {a['sub_type']} request {verb}."
        except Exception as e:
            return _err(e)

    _register(
        "qq_handle_group_request",
        "Approve or reject a group join/invite request. Requires the request "
        "flag and sub_type ('add' or 'invite') from the inbound event.",
        {
            "flag": _str("Request flag identifier"),
            "sub_type": _str("Request sub type: 'add' or 'invite'"),
            "approve": _bool("true = approve, false = reject"),
            "reason": _str("Rejection reason (optional, when rejecting)"),
        },
        ["flag", "sub_type", "approve"],
        _handle_group_req,
        emoji="🤝",
    )


def _register_misc_tools() -> None:
    # Reserved for future expansion; currently no extra tools here —
    # qq_get_group_at_all_remain is registered in the query batch.
    pass


def _register_napcat_extension_tools() -> None:
    """NapCat-only extension APIs not present in the OneBot 11 standard."""

    # qq_get_login_info
    async def _login_info(_: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_login_info", {}) or {}
            return (f"Bot QQ: {d.get('user_id')}\n"
                    f"Nickname: {d.get('nickname', 'unknown')}")
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_login_info",
        "Get the bot's own QQ number and nickname.",
        {},
        [],
        _login_info,
        emoji="🤖",
    )

    # qq_get_status
    async def _status(_: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_status", {}) or {}
            return json.dumps(d, ensure_ascii=False, indent=2)
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_status",
        "Get the bot's runtime status (online, good, app online status).",
        {},
        [],
        _status,
        emoji="💚",
    )

    # qq_get_version_info
    async def _version(_: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_version_info", {}) or {}
            return json.dumps(d, ensure_ascii=False, indent=2)
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_version_info",
        "Get NapCat / OneBot implementation version info.",
        {},
        [],
        _version,
        emoji="ℹ️",
    )

    # qq_can_send_image
    async def _can_send_image(_: Dict[str, Any]) -> str:
        try:
            d = await _client.call("can_send_image", {}) or {}
            return f"can_send_image: {d.get('yes', False)}"
        except Exception as e:
            return _err(e)

    _register(
        "qq_can_send_image",
        "Check whether the current account is allowed to send images.",
        {},
        [],
        _can_send_image,
        emoji="🖼️",
    )

    # qq_can_send_record
    async def _can_send_record(_: Dict[str, Any]) -> str:
        try:
            d = await _client.call("can_send_record", {}) or {}
            return f"can_send_record: {d.get('yes', False)}"
        except Exception as e:
            return _err(e)

    _register(
        "qq_can_send_record",
        "Check whether the current account is allowed to send voice records.",
        {},
        [],
        _can_send_record,
        emoji="🎙️",
    )

    # qq_clean_cache
    async def _clean_cache(_: Dict[str, Any]) -> str:
        try:
            await _client.call("clean_cache", {})
            return "NapCat cache cleaned."
        except Exception as e:
            return _err(e)

    _register(
        "qq_clean_cache",
        "Clean NapCat's media cache on the server (frees disk space).",
        {},
        [],
        _clean_cache,
        emoji="🧹",
    )

    # qq_get_msg
    async def _get_msg(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_msg",
                                   {"message_id": a["message_id"]}) or {}
            sender = d.get("sender") or {}
            name = (sender.get("card") or sender.get("nickname")
                    or sender.get("user_id") or "unknown")
            raw = d.get("raw_message") or ""
            return (f"[{d.get('message_id')}] {name}\n"
                    f"time: {d.get('time')}\n"
                    f"raw: {raw}")
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_msg",
        "Fetch a message by message_id (sender, time, content).",
        {"message_id": _num("Message ID to look up")},
        ["message_id"],
        _get_msg,
        emoji="🔎",
    )

    # qq_get_forward_msg
    async def _get_forward_msg(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_forward_msg",
                                   {"id": a["id"]}) or {}
            return json.dumps(d, ensure_ascii=False, indent=2)[:4000]
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_forward_msg",
        "Fetch the contents of a merged forward message by its id.",
        {"id": _str("Forward message id")},
        ["id"],
        _get_forward_msg,
        emoji="🔎",
    )

    # qq_get_record (NapCat: download voice in a specific format)
    async def _get_record(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_record", {
                "file": a["file"],
                "out_format": a.get("out_format", "mp3"),
            }) or {}
            return f"Voice converted: {d.get('file', '?')}"
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_record",
        "Convert a NapCat voice file to a given audio format and return the path.",
        {
            "file": _str("Voice file (file_id or path returned by an event)"),
            "out_format": _str("Output format: mp3 (default), amr, wma, m4a, …"),
        },
        ["file"],
        _get_record,
        emoji="🎵",
    )

    # qq_get_image (NapCat: resolve image file to local path)
    async def _get_image(a: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_image", {"file": a["file"]}) or {}
            return f"Image local file: {d.get('file', '?')}"
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_image",
        "Resolve an image file id/URL to NapCat's local file path.",
        {"file": _str("Image file id or URL from an event")},
        ["file"],
        _get_image,
        emoji="🖼️",
    )

    # qq_set_qq_profile (NapCat extension)
    async def _set_profile(a: Dict[str, Any]) -> str:
        try:
            await _client.call("set_qq_profile", {
                "nickname": a.get("nickname", ""),
                "personal_note": a.get("personal_note", ""),
                "sex": a.get("sex", ""),
            })
            return "Bot QQ profile updated."
        except Exception as e:
            return _err(e)

    _register(
        "qq_set_qq_profile",
        "Update the bot's own QQ profile (nickname, signature, sex).",
        {
            "nickname": _str("New nickname (optional)"),
            "personal_note": _str("New signature (optional)"),
            "sex": _str("Sex: '男'/'女'/'未知' (optional)"),
        },
        [],
        _set_profile,
        emoji="🪪",
    )

    # qq_get_group_system_msg (pending join/invite requests)
    async def _system_msg(_: Dict[str, Any]) -> str:
        try:
            d = await _client.call("get_group_system_msg", {}) or {}
            return json.dumps(d, ensure_ascii=False, indent=2)[:4000]
        except Exception as e:
            return _err(e)

    _register(
        "qq_get_group_system_msg",
        "Get pending group system messages (unhandled join/invite requests).",
        {},
        [],
        _system_msg,
        emoji="🗂️",
    )
