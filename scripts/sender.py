#!/usr/bin/env python3
"""发送模块：按 config.yaml delivery.channel 推送（whatsapp/telegram/webhook/none）。

Hermes 用户：channel=whatsapp 时依赖 Hermes cron 的 deliver=whatsapp 投递，
脚本本身不直接发（agent 输出即投递）——本模块供 no_agent / 通用框架场景使用。
"""
import json
import os
import urllib.request

from appconfig import delivery_channel


def send_text(text, channel=None, cfg_delivery=None):
    channel, d = delivery_channel() if channel is None else (channel, cfg_delivery or {})
    if channel == "none" or not text.strip():
        return False
    try:
        if channel == "telegram":
            tg = d.get("telegram", {})
            token, chat = tg.get("bot_token", ""), tg.get("chat_id", "")
            if not (token and chat):
                print("[send] telegram 未配置 bot_token/chat_id")
                return False
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = json.dumps({"chat_id": chat, "text": text}).encode()
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 200
        if channel == "webhook":
            url = d.get("webhook", {}).get("url", "")
            if not url:
                print("[send] webhook 未配置 url")
                return False
            data = json.dumps({"text": text}).encode()
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return 200 <= resp.status < 300
        if channel == "whatsapp":
            # Hermes 场景：投递由 cron deliver=whatsapp 完成，脚本无需直发
            print("[send] whatsapp 通道由 Hermes cron 投递（agent 输出即消息）")
            return True
        print(f"[send] 未知通道: {channel}")
        return False
    except Exception as e:
        print(f"[send] 发送失败 ({channel}): {e}")
        return False
