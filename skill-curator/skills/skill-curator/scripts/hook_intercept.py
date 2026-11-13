#!/usr/bin/env python3
"""Pre-invocation hook for Antigravity (PreInvocation) and Claude Code (UserPromptSubmit)."""

from __future__ import annotations

import json
import re
import sys
from urllib.parse import urlparse

URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", re.IGNORECASE)
NON_SKILL_GITHUB_SEGMENT_RE = re.compile(
    r"^/(?:[^/]+/[^/]+)/(?:issues|pull|pulls|discussions|commit|commits|"
    r"releases|actions|wiki|compare|settings|security|projects|milestones|labels)(?:/|$)",
    re.IGNORECASE,
)
GITHUB_BLOB_RE = re.compile(r"^/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$", re.IGNORECASE)
GITHUB_TREE_RE = re.compile(r"^/([^/]+)/([^/]+)/tree/([^/]+)(?:/.*)?$", re.IGNORECASE)
GITHUB_ROOT_RE = re.compile(r"^/([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)

CLI_INSTALL_RE = re.compile(
    r"\b(?:npx\s+skills\s+(?:add|install)|skills\s+add|agy\s+skills|claude\s+plugin\s+install)\b",
    re.IGNORECASE,
)
CURATOR_TERM_RE = re.compile(
    r"(?:skill[\s_-]?vault|skill[\s_-]?curator|scene[\s_-]?tags?|global\s+core|"
    r"场景标签|技能库|技能仓库|全局技能|冷库)",
    re.IGNORECASE,
)
EN_SKILL_INTENT_RE = re.compile(
    r"\b(?:install|add|curate|mount|unmount|demote|search|find|lookup|audit|check|inspect|health)\b"
    r".{0,48}\bskills?\b"
    r"|\bskills?\b.{0,32}\b(?:install|mount|demote|audit|health|search|vault)\b",
    re.IGNORECASE,
)
CN_SKILL_INTENT_RE = re.compile(
    r"(?:帮我装|装一下|装一?个|安装|挂载|收集|整理|导入|入库|归档|搜索|查找|审计|检查|健康检查|体检|降级|卸载).{0,24}(?:skills?|技能)"
    r"|(?:skills?|技能).{0,24}(?:入库|归档|安装|挂载|健康检查|体检|审计|降级|卸载|搜索|查找)",
    re.IGNORECASE,
)


def detect_skill_urls(text: str) -> list[str]:
    """Return URLs in text that plausibly point to a skill or skill repository."""
    found: list[str] = []
    for raw in URL_RE.findall(text):
        url = raw.rstrip(".,;:!?)'\"]")
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path or "/"

        if host in ("skills.sh", "www.skills.sh", "clawhub.ai", "www.clawhub.ai"):
            found.append(url)
            continue

        if host == "raw.githubusercontent.com" and path.lower().endswith("/skill.md"):
            found.append(url)
            continue

        if host in ("github.com", "www.github.com"):
            if NON_SKILL_GITHUB_SEGMENT_RE.match(path):
                continue
            blob_m = GITHUB_BLOB_RE.match(path)
            if blob_m:
                if blob_m.group(4).lower().endswith("skill.md"):
                    found.append(url)
                continue
            if GITHUB_TREE_RE.match(path) or GITHUB_ROOT_RE.match(path):
                found.append(url)
                continue
    return list(dict.fromkeys(found))


def build_reminder(urls: list[str]) -> str:
    """Build a concise (< 120 words) curation reminder for the agent."""
    url_note = f" Detected URL(s): {', '.join(urls)}." if urls else ""
    return (
        "[skill-curator] Skill curation intent detected."
        f"{url_note} Load the `skill-curator` skill (`SKILL.md`) before taking action. "
        "NEVER dump skills directly into `~/.agents/skills` or `~/.claude/skills`. "
        "Follow `fetch` -> `inspect` -> present one concise audit/tier recommendation for user confirmation "
        "-> `store` into Skill Vault (plus copy-based `mount` or `mount-tag` if requested). "
        "For Vault lookup or cleanup, use `search` or `health` + user-confirmed `demote`."
    )


def process_payload(payload: object) -> dict[str, object]:
    """Evaluate stdin JSON payload and return Antigravity or Claude Code hook response."""
    if not isinstance(payload, dict):
        return {}

    if "lastUserInput" in payload or payload.get("hook_event_name") == "PreInvocation":
        inv_num = payload.get("invocationNum")
        try:
            if inv_num is not None and float(inv_num) >= 1:
                return {}
        except (TypeError, ValueError):
            pass
        text = str(payload.get("lastUserInput") or "")
        harness = "antigravity"
    elif "prompt" in payload or payload.get("hook_event_name") == "UserPromptSubmit":
        text = str(payload.get("prompt") or "")
        harness = "claude"
    else:
        return {}

    if not text.strip():
        return {}

    urls = detect_skill_urls(text)
    triggered = bool(
        urls
        or CLI_INSTALL_RE.search(text)
        or CURATOR_TERM_RE.search(text)
        or EN_SKILL_INTENT_RE.search(text)
        or CN_SKILL_INTENT_RE.search(text)
    )
    if not triggered:
        return {}

    reminder = build_reminder(urls)
    if harness == "claude":
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": reminder,
            }
        }
    return {"injectSteps": [{"systemMessage": {"systemMessage": reminder}}]}


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.stdout.write("{}\n")
            return 0
        payload = json.loads(raw)
        response = process_payload(payload)
        sys.stdout.write(json.dumps(response) + "\n")
    except Exception:
        sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
