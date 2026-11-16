#!/usr/bin/env python3
"""Телеграм-бот: слідкує за графіком відключень і пише, коли він змінився
та за годину до відключення. Лише стандартна бібліотека Python 3.9+.

Налаштування через змінні середовища:
  TG_TOKEN, TG_CHAT_ID  обов'язково для відправки (без них бот просто друкує повідомлення)
  QUEUE                 черга, за замовчуванням 3.2
  DATA_URL              адреса JSON з графіком
  GROUP_KEY             ключ групи в JSON, якщо автоматичний (GPV6 для 3.2) не підходить
  REMIND_MIN            за скільки хвилин нагадувати, за замовчуванням 60
"""
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Kyiv")
QUEUE = os.environ.get("QUEUE") or "3.2"
DATA_URL = os.environ.get("DATA_URL") or (
    "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Chernihivoblenergo.json"
)
DATA_FILE = os.environ.get("DATA_FILE")  # для локальної перевірки
GROUP_KEY = os.environ.get("GROUP_KEY")
REMIND_MIN = int(os.environ.get("REMIND_MIN", "60"))
STATE_PATH = os.environ.get("STATE_PATH", "state.json")

# 0 світло є, 1 немає, 2 можливе відключення
STATUS = {
    "yes": (0, 0), "no": (1, 1), "first": (1, 0), "second": (0, 1),
    "mfirst": (2, 0), "msecond": (0, 2), "maybe": (2, 2), "on": (0, 0), "off": (1, 1),
}
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня",
          "серпня", "вересня", "жовтня", "листопада", "грудня"]


def now_kyiv():
    override = os.environ.get("NOW")  # лише для перевірки, формат 2026-11-16T22:00
    if override:
        return datetime.fromisoformat(override).replace(tzinfo=TZ)
    return datetime.now(TZ)


def fetch_json():
    if DATA_FILE:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(DATA_URL, headers={"User-Agent": "svitlo-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def group_key(queue):
    g, s = queue.split(".")
    return "GPV%d" % ((int(g) - 1) * 2 + int(s))


def days_data(raw):
    for path in (("data", "fact", "data"), ("fact", "data"), ("data",), ()):
        node = raw
        try:
            for p in path:
                node = node[p]
        except (KeyError, TypeError):
            continue
        if isinstance(node, dict) and node:
            return node
    return {}


def parse_days(raw, gkey):
    """{ 'YYYY-MM-DD': [48 станів] } для однієї групи."""
    out = {}
    seen_groups = set()
    for ts, groups in days_data(raw).items():
        if not isinstance(groups, dict):
            continue
        seen_groups.update(groups.keys())
        if gkey not in groups:
            continue
        try:
            date = datetime.fromtimestamp(int(ts), TZ).date().isoformat()
        except (ValueError, OverflowError, OSError):
            continue
        slots = [0] * 48
        for h, st in groups[gkey].items():
            try:
                hi = int(h) - 1
            except ValueError:
                continue
            pair = STATUS.get(str(st).lower())
            if pair and 0 <= hi < 24:
                slots[hi * 2], slots[hi * 2 + 1] = pair
        out[date] = slots
    return out, sorted(seen_groups)


def runs_in_day(slots):
    """Інтервали [(start_min, end_min, state)] для станів 1 і 2."""
    runs, i = [], 0
    while i < 48:
        if slots[i] in (1, 2):
            j = i
            while j < 48 and slots[j] == slots[i]:
                j += 1
            runs.append((i * 30, j * 30, slots[i]))
            i = j
        else:
            i += 1
    return runs


def hhmm(m):
    m %= 1440
    return "%02d:%02d" % (m // 60, m % 60)


def dur(m):
    h, mm = divmod(m, 60)
    if h and mm:
        return "%d год %d хв" % (h, mm)
    return "%d год" % h if h else "%d хв" % mm


def day_title(iso, today):
    d = datetime.fromisoformat(iso).date()
    name = "%d %s" % (d.day, MONTHS[d.month - 1])
    if d == today:
        return "Сьогодні, " + name
    if (d - today).days == 1:
        return "Завтра, " + name
    return name


def format_day(iso, slots, today):
    runs = runs_in_day(slots)
    head = day_title(iso, today)
    if not runs:
        return head + ": відключень немає"
    lines = [head + ":"]
    for a, b, st in runs:
        mark = "" if st == 1 else " (можливо)"
        lines.append("• %s–%s, %s%s" % (hhmm(a), hhmm(b) if b < 1440 else "24:00", dur(b - a), mark))
    return "\n".join(lines)


def absolute_outages(days):
    """Суцільні відключення з датами, склеєні через північ. Лише стан 1."""
    items = []
    for iso in sorted(days):
        base = datetime.combine(datetime.fromisoformat(iso).date(), time(), TZ)
        for a, b, st in runs_in_day(days[iso]):
            if st != 1:
                continue
