#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sender.py —— 把消息送到你手上（通道可换，脚本不关心你选哪个）。

通道一览（config.yaml → delivery.channel）：
  hermes    交给 Hermes 定时任务投递（agent 的回复就是消息，最省事）
  whatsapp  同上，但走 Hermes 的 whatsapp deliver
  telegram  Bot API 直发（要 bot_token + chat_id）
  ntfy      手机装 ntfy App，订阅一个 topic 即可（最轻，不用注册）
  webhook   往任意 URL POST {"text": ...}（接飞书/钉钉/Slack/自建服务）
  email     SMTP 发邮件（要 smtp 服务器 + 授权码）
  local     只存到本机文件 + 弹 macOS 通知（不联网）
  none      不发，只写日志（调试用）

Agent 调用：`python3 sender.py "要发的文字"`，或 `from sender import send_text`。
"""
from __future__ import annotations

import json
import os
import smtplib
import subprocess
import sys
import urllib.request
from email.mime.text import MIMEText

try:
    import config_store as cs
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config_store as cs

import platform_support as ps


CHANNELS = {
    "auto":     {"label": "自动（跟着你平台上已有的推送方式走）", "needs": "无"},
    "hermes":   {"label": "Hermes 定时任务投递（推荐 Hermes 用户）", "needs": "无"},
    "whatsapp": {"label": "Hermes → WhatsApp", "needs": "无（靠 cron deliver=whatsapp）"},
    "telegram": {"label": "Telegram Bot", "needs": "bot_token、chat_id"},
    "ntfy":     {"label": "ntfy 推送（手机 App 订阅 topic）", "needs": "topic（可自建 server）"},
    "webhook":  {"label": "任意 Webhook（飞书/钉钉/Slack/自建）", "needs": "url"},
    "email":    {"label": "邮件 SMTP", "needs": "smtp_host、port、user、password、to"},
    "local":    {"label": "只存本机 + 系统通知", "needs": "无"},
    "none":     {"label": "不发（只写日志）", "needs": "无"},
}


def detect_channel(cfg=None):
    """自动选通道：不麻烦用户想「推到哪个 App」，跟着他平台上已有的方式走。

    返回 (通道, 为什么)。优先顺序：
      1. 环境变量 MOODLE_KILLER_CHANNEL（定时任务里可以钉死）
      2. 配置里明确写了通道 → 听配置的
      3. 检测到 Agent 平台的定时投递（Hermes）→ 交给它，脚本不重复发
      4. 都没有 → 本机通知（Mac/Win/Linux 各自弹各自的通知）
    """
    forced = (os.environ.get("MOODLE_KILLER_CHANNEL") or "").strip().lower()
    if forced:
        return forced, "环境变量指定"
    cfg = cfg or cs.load_config()
    ch = str(cs.get_path(cfg, "delivery.channel") or "auto").strip().lower()
    if ch and ch != "auto":
        return ch, "你在配置里指定的"
    if os.environ.get("MOODLE_KILLER_DELIVERED_BY_AGENT"):
        return "hermes", "由当前 Agent 平台投递"
    if os.path.exists(os.path.expanduser("~/.hermes/cron/jobs.json")):
        return "hermes", "检测到 Hermes 定时任务，交给它投递"
    return "local", "没检测到能投递的 Agent 平台，先用本机通知"


def _post_json(url, payload, timeout=15):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return 200 <= resp.status < 300


def _local(text, title):
    out = cs.out_dir() / "sent.log"
    with open(str(out), "a", encoding="utf-8") as f:
        f.write("%s\t%s\n" % (title or "Moodle", text.replace("\n", " | ")))
    ps.notify(title or "Moodle-killer", text)
    return True


def send_text(text, title=None, channel=None, cfg=None, dry_run=False):
    """发一条消息。返回 (是否成功, 说明文字)。"""
    cfg = cfg or cs.load_config()
    d = cs.get_path(cfg, "delivery") or {}
    channel = channel or d.get("channel") or "auto"
    if channel == "auto":
        channel, why = detect_channel(cfg)
    else:
        why = ""
    text = (text or "").strip()
    if channel == "none" or not text:
        return False, "通道 none 或内容为空，未发送"
    if dry_run:
        return True, "试跑：本应通过 %s 发送 %d 字%s" % (
            channel, len(text), ("（%s）" % why if why else ""))

    try:
        if channel in ("hermes", "whatsapp"):
            # 由 Agent / cron 的 deliver 参数投递，脚本侧不重复发
            return True, "%s：由 Agent 投递（脚本不直发）" % channel

        if channel == "telegram":
            tg = d.get("telegram") or {}
            token = tg.get("bot_token") or ""
            chat = tg.get("chat_id") or ""
            if not (token and chat):
                return False, "telegram 缺 bot_token / chat_id"
            ok = _post_json("https://api.telegram.org/bot%s/sendMessage" % token,
                            {"chat_id": chat, "text": text})
            return ok, "telegram %s" % ("已发送" if ok else "返回异常")

        if channel == "ntfy":
            n = d.get("ntfy") or {}
            topic = n.get("topic") or ""
            server = (n.get("server") or "https://ntfy.sh").rstrip("/")
            if not topic:
                return False, "ntfy 缺 topic"
            req = urllib.request.Request(
                "%s/%s" % (server, topic), data=text.encode("utf-8"),
                headers={"Title": title or "Moodle-killer", "Tags": "books"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return 200 <= resp.status < 300, "ntfy %s" % resp.status

        if channel == "webhook":
            url = (d.get("webhook") or {}).get("url") or ""
            if not url:
                return False, "webhook 缺 url"
            ok = _post_json(url, {"text": text, "title": title or "Moodle-killer"})
            return ok, "webhook %s" % ("已发送" if ok else "返回异常")

        if channel == "email":
            e = d.get("email") or {}
            need = ["smtp_host", "smtp_user", "smtp_password", "to"]
            miss = [k for k in need if not e.get(k)]
            if miss:
                return False, "email 缺 %s" % "、".join(miss)
            msg = MIMEText(text, "plain", "utf-8")
            msg["Subject"] = title or "Moodle-killer"
            msg["From"] = e.get("smtp_user")
            msg["To"] = e.get("to")
            port = int(e.get("smtp_port") or 465)
            if port == 587:
                srv = smtplib.SMTP(e["smtp_host"], port, timeout=20)
                srv.starttls()
            else:
                srv = smtplib.SMTP_SSL(e["smtp_host"], port, timeout=20)
            with srv:
                srv.login(e["smtp_user"], e["smtp_password"])
                srv.sendmail(e["smtp_user"], [x.strip() for x in e["to"].split(",")], msg.as_string())
            return True, "email 已发送"

        if channel == "local":
            return _local(text, title), "local 已记录"

        return False, "未知通道：%s" % channel
    except Exception as e:
        return False, "发送失败(%s)：%s" % (channel, e)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        print("通道：")
        for k, v in CHANNELS.items():
            print("  %-9s %s（需要：%s）" % (k, v["label"], v["needs"]))
        return 0
    dry = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    text = argv[0] if argv else sys.stdin.read()
    ok, msg = send_text(text, dry_run=dry)
    print(("✅ " if ok else "❌ ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
