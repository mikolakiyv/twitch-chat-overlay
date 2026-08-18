# -*- coding: utf-8 -*-
"""Twitch Chat Overlay — чат Twitch поверх всех окон.

by aliveenjoyer (twitch.tv/aliveenjoyer)

Читает чат Twitch (можно анонимно, можно со своим аккаунтом) и показывает
сообщения в компактном полупрозрачном окне поверх других приложений.
Поддерживает несколько каналов сразу, отправку сообщений после входа
и подсветку упоминаний.

Управление:
  • Перетаскивание за верхнюю полосу — переместить окно
  • Уголок ◢ справа внизу — изменить размер
  • Правый клик (или ⚙) — меню настроек
  • F8 — вкл/выкл «сквозные клики» (мышь проходит сквозь окно)

Запуск: двойной клик по этому файлу (нужен Python) или TwitchChatOverlay.exe.
"""

import base64
import ctypes
import json
import os
import queue
import random
import re
import socket
import sys
import threading
import urllib.request
import webbrowser
import zlib
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
import tkinter as tk
from tkinter import font as tkfont

# Pillow нужен только для смайлов 7TV (они в формате webp); без него всё
# остальное работает как раньше
try:
    import io as _io
    from PIL import Image as _PILImage
    HAS_PIL = True
except Exception:
    HAS_PIL = False

if getattr(sys, "frozen", False):
    # собрано в .exe (PyInstaller): настройки и кэш держим рядом с exe
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "overlay_config.json")
CACHE_DIR = os.path.join(APP_DIR, ".emote_cache")
BADGE_CACHE_DIR = os.path.join(APP_DIR, ".badge_cache")

# Две темы: классическая твичевская и тёплая в стиле Claude
PALETTES = {
    "claude": dict(bg="#262624", bar="#1f1e1b", border="#3f3c36", fg="#f0eee6",
                   sys="#9c968b", chip="#a8a29a", accent="#d97757",
                   accent_hover="#e89b7f", accent_active="#b85c3e",
                   mention="#503527", entry="#30302b", chipbtn="#34332e",
                   select="#4a4740", grip="#6b665d", btnfg="#cac5bb",
                   slider_track="#cfc8ba", slider_knob="#f6f2e9"),
    "twitch": dict(bg="#17171a", bar="#1e1e22", border="#3a3a41", fg="#efeff1",
                   sys="#a3a3ab", chip="#a6a6ae", accent="#9147ff",
                   accent_hover="#c39cff", accent_active="#772ce8",
                   mention="#3d2a66", entry="#26262b", chipbtn="#2e2e35",
                   select="#404049", grip="#63636b", btnfg="#cfcfd6",
                   slider_track="#c9cbd6", slider_knob="#f4f4f8"),
}


def apply_palette(name):
    """Назначает глобальные цвета из выбранной темы (BG — ключ прозрачности)."""
    global BG, BAR_BG, BORDER, FG, SYS_FG, CHIP_FG, ACCENT, ACCENT_HOVER
    global ACCENT_ACTIVE, MENTION_BG, ENTRY_BG, CHIPBTN_BG, SELECT_BG, GRIP_FG, BTN_FG
    global SLIDER_TRACK, SLIDER_KNOB
    p = PALETTES.get(name) or PALETTES["claude"]
    BG = p["bg"]
    BAR_BG = p["bar"]
    BORDER = p["border"]
    FG = p["fg"]
    SYS_FG = p["sys"]
    CHIP_FG = p["chip"]
    ACCENT = p["accent"]
    ACCENT_HOVER = p["accent_hover"]
    ACCENT_ACTIVE = p["accent_active"]
    MENTION_BG = p["mention"]
    ENTRY_BG = p["entry"]
    CHIPBTN_BG = p["chipbtn"]
    SELECT_BG = p["select"]
    GRIP_FG = p["grip"]
    BTN_FG = p["btnfg"]
    SLIDER_TRACK = p["slider_track"]
    SLIDER_KNOB = p["slider_knob"]


def dwm_round(widget, small=False):
    """Скругляет углы окна средствами Windows 11 (на Windows 10 просто игнор)."""
    try:
        hwnd = ctypes.windll.user32.GetAncestor(widget.winfo_id(), 2)
        pref = ctypes.c_int(3 if small else 2)  # DWMWCP_ROUNDSMALL / DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
    except Exception:
        pass


apply_palette("twitch")

DEBUG = "--debug" in sys.argv

DEFAULTS = {
    "channels": [],
    "opacity": 0.88,
    "font_size": 11,
    "width": 380,
    "height": 480,
    "x": None,
    "y": None,
    "ghost": False,
    "frameless": False,
    "theme": "twitch",
    "lang": "ru",
    "active_tab": "*",
    "key_clickthrough": {"vk": 119, "name": "F8"},
    "key_frameless": {"vk": 120, "name": "F9"},
    "max_messages": 150,
    "token": "",
    "login": "",
    "highlight_name": "",
}

# ---------------------------------------------------------------- язык

LANG = "ru"

STRINGS = {
    "ru": {
        "hint_start": "Правый клик или ⚙ — настройки · %s — сквозные клики · %s — только текст",
        "s_channels": "Каналы",
        "s_apply": "OK",
        "s_account": "Аккаунт",
        "s_mention": "Упоминания (@ник)",
        "s_modes": "РЕЖИМЫ",
        "s_appear": "ВНЕШНИЙ ВИД",
        "s_textonly": "Только текст чата",
        "s_click": "Сквозные клики",
        "s_support": "💜 Поддержать",
        "s_about": "О программе",
        "mention_saved": "Упоминания: @%s",
        "tab_all": "Все",
        "pil_off": "Смайлы 7TV выключены: нет пакета Pillow. Запустите через батник — установит сам.",
        "connecting": "Подключение к Twitch…",
        "reconnecting": "Переподключение…",
        "conn_lost": "Соединение потеряно, повтор через 5 с…",
        "joined": "Подключено: #%s",
        "no_channels": "Каналы не заданы: откройте настройки (⚙ или правый клик)",
        "auth_stale": "Вход не удался: токен устарел. Меню → «Войти в Twitch…»",
        "logged_in": "Вы вошли как %s. Теперь можно писать в чат.",
        "no_chat_edit": "Внимание: у токена нет права chat:edit — отправка может не работать.",
        "logged_out": "Вы вышли из аккаунта.",
        "not_sent": "Не отправлено: нет соединения.",
        "clickthrough_on": "Сквозные клики ВКЛ: окно не ловит мышь. %s — выключить.",
        "clickthrough_hint": "  сквозные клики · %s",
        "textonly_on": "Только текст чата. %s — вернуть окно.",
        "bind_updated": "Бинд обновлён: %s → %s",
        "mentions_now": "Упоминания: @%s",
        "mentions_off": "Упоминания выключены",
        "lang_set": "Язык: русский",
        "newmsgs": "↓ новые сообщения (%d)",
        "placeholder": "Написать в чат…",
        "tt_donate": "Поддержать разработчика",
        "tt_menu": "Настройки",
        "tt_close": "Закрыть",
        "tt_chan": "Куда отправлять (клик — сменить)",
        "m_channels": "Каналы…",
        "m_login": "Войти в Twitch…",
        "m_account": "Аккаунт: %s",
        "m_logout": "Выйти",
        "m_clear": "Очистить чат",
        "m_textonly": "Только текст чата (%s)",
        "m_clickthrough": "Сквозные клики (%s)",
        "m_topmost": "Поверх всех окон",
        "m_view": "Внешний вид",
        "m_opacity": "Непрозрачность",
        "m_fontsize": "Размер шрифта",
        "m_theme": "Тема",
        "th_twitch": "Twitch (фиолетовая)",
        "th_claude": "Claude (тёплая)",
        "m_ghost": "Прозрачный фон",
        "m_settings": "Настройки",
        "m_mentions": "Упоминания…",
        "m_keys": "Горячие клавиши",
        "k_textonly": "Только текст… (%s)",
        "k_clickthrough": "Сквозные клики… (%s)",
        "m_lang": "Язык / Language",
        "m_resetpos": "Сбросить позицию окна",
        "m_about": "О программе · поддержать 💜",
        "m_quit": "Закрыть оверлей",
        "d_channels_title": "Каналы Twitch",
        "d_channels_label": "Каналы через запятую (имя или ссылка):",
        "d_first_label": "Каналы Twitch через запятую (имя или ссылка):",
        "d_ok": "Готово",
        "d_login_title": "Вход в Twitch",
        "d_login_steps": ("Чтобы писать в чат, нужен токен доступа:\n"
                          "1. Нажмите «Получить токен» — откроется сайт\n"
                          "    twitchtokengenerator.com\n"
                          "2. Выберите «Bot Chat Token» и войдите в Twitch\n"
                          "3. Скопируйте ACCESS TOKEN и вставьте сюда"),
        "d_login_get": "Получить токен (откроется браузер)",
        "d_login_note": "Токен хранится только на этом компьютере (зашифрован).",
        "d_login_btn": "Войти",
        "d_login_checking": "Проверяю токен…",
        "d_login_bad": "Токен не подошёл. Скопируйте ACCESS TOKEN целиком.",
        "d_mention_title": "Уведомления об упоминаниях",
        "d_mention_label": "Подсвечивать сообщения, где упомянут ник:",
        "d_key_textonly": "Только текст чата",
        "d_key_click": "Сквозные клики",
        "d_key_press": "Нажмите новую клавишу\n(сейчас: %s, Esc — отмена)",
        "d_key_mod": "Модификаторы нельзя.\nНажмите обычную клавишу (Esc — отмена)",
        "d_key_taken": "Клавиша занята другим биндом.\nНажмите другую (Esc — отмена)",
        "about_sub": "чат поверх всех окон · 7TV · упоминания",
        "about_dev": "Разработано: %s",
        "about_support": "Поддержать на DonationAlerts 💜",
    },
    "en": {
        "hint_start": "Right-click or ⚙ — settings · %s — click-through · %s — text only",
        "s_channels": "Channels",
        "s_apply": "OK",
        "s_account": "Account",
        "s_mention": "Mentions (@name)",
        "s_modes": "MODES",
        "s_appear": "APPEARANCE",
        "s_textonly": "Text-only chat",
        "s_click": "Click-through",
        "s_support": "💜 Support",
        "s_about": "About",
        "mention_saved": "Mentions: @%s",
        "tab_all": "All",
        "pil_off": "7TV emotes disabled: Pillow package missing. Run the .bat — it installs it.",
        "connecting": "Connecting to Twitch…",
        "reconnecting": "Reconnecting…",
        "conn_lost": "Connection lost, retrying in 5 s…",
        "joined": "Joined #%s",
        "no_channels": "No channels set: open settings (⚙ or right-click)",
        "auth_stale": "Login failed: token expired. Menu → “Log in to Twitch…”",
        "logged_in": "Logged in as %s. You can chat now.",
        "no_chat_edit": "Note: token lacks chat:edit scope — sending may not work.",
        "logged_out": "Logged out.",
        "not_sent": "Not sent: no connection.",
        "clickthrough_on": "Click-through ON: window ignores mouse. %s — turn off.",
        "clickthrough_hint": "  click-through · %s",
        "textonly_on": "Text-only mode. %s — bring the window back.",
        "bind_updated": "Hotkey updated: %s → %s",
        "mentions_now": "Mentions: @%s",
        "mentions_off": "Mentions disabled",
        "lang_set": "Language: English",
        "newmsgs": "↓ new messages (%d)",
        "placeholder": "Send a message…",
        "tt_donate": "Support the developer",
        "tt_menu": "Settings",
        "tt_close": "Close",
        "tt_chan": "Send target (click to switch)",
        "m_channels": "Channels…",
        "m_login": "Log in to Twitch…",
        "m_account": "Account: %s",
        "m_logout": "Log out",
        "m_clear": "Clear chat",
        "m_textonly": "Text-only chat (%s)",
        "m_clickthrough": "Click-through (%s)",
        "m_topmost": "Always on top",
        "m_view": "Appearance",
        "m_opacity": "Opacity",
        "m_fontsize": "Font size",
        "m_theme": "Theme",
        "th_twitch": "Twitch (purple)",
        "th_claude": "Claude (warm)",
        "m_ghost": "Transparent background",
        "m_settings": "Settings",
        "m_mentions": "Mentions…",
        "m_keys": "Hotkeys",
        "k_textonly": "Text-only… (%s)",
        "k_clickthrough": "Click-through… (%s)",
        "m_lang": "Язык / Language",
        "m_resetpos": "Reset window position",
        "m_about": "About · support 💜",
        "m_quit": "Close overlay",
        "d_channels_title": "Twitch channels",
        "d_channels_label": "Channels, comma-separated (name or link):",
        "d_first_label": "Twitch channels, comma-separated (name or link):",
        "d_ok": "Done",
        "d_login_title": "Log in to Twitch",
        "d_login_steps": ("To chat you need an access token:\n"
                          "1. Click “Get token” — opens\n"
                          "    twitchtokengenerator.com\n"
                          "2. Choose “Bot Chat Token” and log in to Twitch\n"
                          "3. Copy the ACCESS TOKEN and paste it here"),
        "d_login_get": "Get token (opens browser)",
        "d_login_note": "The token is stored only on this PC (encrypted).",
        "d_login_btn": "Log in",
        "d_login_checking": "Checking token…",
        "d_login_bad": "Token rejected. Copy the whole ACCESS TOKEN.",
        "d_mention_title": "Mention notifications",
        "d_mention_label": "Highlight messages mentioning this name:",
        "d_key_textonly": "Text-only chat",
        "d_key_click": "Click-through",
        "d_key_press": "Press a new key\n(current: %s, Esc — cancel)",
        "d_key_mod": "Modifier keys not allowed.\nPress a regular key (Esc — cancel)",
        "d_key_taken": "Key already used by another hotkey.\nPress another (Esc — cancel)",
        "about_sub": "chat on top of everything · 7TV · mentions",
        "about_dev": "Developed by %s",
        "about_support": "Support on DonationAlerts 💜",
    },
}


def T(key, *args):
    s = STRINGS.get(LANG, STRINGS["ru"]).get(key) or STRINGS["ru"].get(key, key)
    return (s % args) if args else s


def set_language(lang):
    global LANG
    LANG = lang if lang in STRINGS else "ru"


# палитра для ников без своего цвета (как у Twitch)
NICK_COLORS = [
    "#FF4A4A", "#5C9DFF", "#57D964", "#FF7F50", "#9ACD32", "#FF69B4",
    "#5F9EA0", "#1E90FF", "#8A2BE2", "#00FF7F", "#DAA520", "#FF4500",
]

# Запасные значки (UUID картинок на CDN), если GQL-запрос не сработает
FALLBACK_BADGES = {
    "broadcaster/1": "5527c58c-fb7d-422d-b71b-f309dcb85cc1",
    "moderator/1": "3267646d-33f0-4b17-b3df-f923a41db1d0",
    "vip/1": "b817aba4-fad8-49e2-b88a-7cc744dfa6ec",
    "partner/1": "d12a2e27-16f6-41d0-ab77-b780518f00a3",
    "staff/1": "d97c37bd-a6f5-4c38-8f57-4e4bef88af34",
    "premium/1": "bbbe0db0-a598-423e-86d0-f9fb98ca1933",
    "turbo/1": "bd444ec6-8f34-4bf9-91f4-af1e3428d80f",
    "subscriber/0": "5d9f2208-5dd8-11e7-8513-2ff4adfae661",
    "founder/0": "511b78a9-ab37-472f-9569-457753bbe7d3",
}
GQL_URL = "https://gql.twitch.tv/gql"
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # публичный client-id сайта twitch.tv
TOKEN_SITE = "https://twitchtokengenerator.com/"
AUTHOR = "aliveenjoyer"
AUTHOR_URL = "https://twitch.tv/aliveenjoyer"
DONATE_URL = "https://www.donationalerts.com/r/nihaoenjoyer"

SEVENTV_CACHE_DIR = os.path.join(APP_DIR, ".7tv_cache")

_MISS = object()


class LruDict:
    """Потокобезопасный LRU-кэш: память не растёт бесконечно, старое вытесняется."""

    def __init__(self, cap):
        self.cap = cap
        self._d = {}
        self._lock = threading.Lock()

    def __contains__(self, k):
        with self._lock:
            return k in self._d

    def get(self, k, default=None):
        with self._lock:
            if k not in self._d:
                return default
            v = self._d.pop(k)
            self._d[k] = v  # освежаем позицию
            return v

    def put(self, k, v):
        with self._lock:
            self._d.pop(k, None)
            self._d[k] = v
            while len(self._d) > self.cap:
                del self._d[next(iter(self._d))]


EMOTE_CACHE = LruDict(300)        # id -> base64 PNG или None (не удалось скачать)
BADGE_IMG_CACHE = LruDict(300)    # url -> base64 PNG или None
SEVENTV_IMG_CACHE = LruDict(300)  # 7tv id -> base64 PNG или None
DOWNLOAD_POOL = ThreadPoolExecutor(max_workers=4)  # параллельная докачка картинок


def write_cache_file(path, raw):
    """Атомарная запись кэша: параллельные загрузки не портят файл друг другу."""
    tmp = "%s.%06d.tmp" % (path, random.randint(0, 999999))
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def dbg(*a):
    if DEBUG:
        try:
            print(*a, flush=True)
        except Exception:
            pass


class _CryptBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]


def _dpapi(data, encrypt):
    """Шифрование Windows DPAPI: расшифровать может только этот пользователь Windows."""
    try:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _CryptBlob(len(data), ctypes.cast(buf, ctypes.c_void_p))
        blob_out = _CryptBlob()
        fn = (ctypes.windll.crypt32.CryptProtectData if encrypt
              else ctypes.windll.crypt32.CryptUnprotectData)
        if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
            return None
        out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        try:
            # без явного c_void_p 64-битный указатель не пролезает в LocalFree
            ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(blob_out.pbData))
        except Exception:
            pass
        return out
    except Exception:
        return None


def encrypt_token(tok):
    if not tok:
        return ""
    enc = _dpapi(tok.encode("utf-8"), True)
    return base64.b64encode(enc).decode("ascii") if enc else ""


def decrypt_token(s):
    if not s:
        return ""
    try:
        dec = _dpapi(base64.b64decode(s), False)
        return dec.decode("utf-8") if dec else ""
    except Exception:
        return ""


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
        # миграция со старого формата с одним каналом
        if not cfg.get("channels") and data.get("channel"):
            cfg["channels"] = [data["channel"]]
        # токен: зашифрованный token_enc в приоритете, голый token — наследие
        enc = cfg.pop("token_enc", "")
        if enc:
            cfg["token"] = decrypt_token(enc) or ""
    except Exception:
        pass
    cfg.pop("channel", None)
    chans = []
    for c in cfg.get("channels") or []:
        c = extract_channel(str(c))
        if c and c not in chans:
            chans.append(c)
    cfg["channels"] = chans
    for key, default in (("key_clickthrough", DEFAULTS["key_clickthrough"]),
                         ("key_frameless", DEFAULTS["key_frameless"])):
        v = cfg.get(key)
        if not (isinstance(v, dict) and isinstance(v.get("vk"), int) and v.get("name")):
            cfg[key] = dict(default)
    return cfg


def save_config(cfg):
    try:
        data = dict(cfg)
        tok = data.pop("token", "")
        if tok:
            enc = encrypt_token(tok)
            if enc:
                data["token_enc"] = enc
            else:
                data["token"] = tok  # DPAPI недоступен — храним как есть
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def extract_channel(raw):
    """Достаёт имя канала из строки: имя, @имя или ссылка twitch.tv/имя."""
    raw = (raw or "").strip()
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]{1,25})", raw)
    if m:
        return m.group(1).lower()
    raw = raw.lstrip("#@ ").strip()
    if re.fullmatch(r"[A-Za-z0-9_]{1,25}", raw):
        return raw.lower()
    return None


def parse_channels(raw):
    """'канал1, канал2 канал3' -> список каналов без повторов."""
    out = []
    for part in re.split(r"[,;\s]+", raw or ""):
        c = extract_channel(part)
        if c and c not in out:
            out.append(c)
    return out


def unescape_tag(v):
    out = []
    i = 0
    repl = {"s": " ", ":": ";", "\\": "\\", "r": "\r", "n": "\n"}
    while i < len(v):
        c = v[i]
        if c == "\\" and i + 1 < len(v):
            out.append(repl.get(v[i + 1], v[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse_irc(line):
    """@tags :prefix COMMAND args :trailing"""
    tags = {}
    if line.startswith("@"):
        head, _, line = line.partition(" ")
        for kv in head[1:].split(";"):
            k, _, v = kv.partition("=")
            tags[k] = unescape_tag(v)
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    rest, _, trailing = line.partition(" :")
    parts = rest.split()
    cmd = parts[0] if parts else ""
    args = parts[1:]
    return tags, prefix, cmd, args, trailing


def readable_color(hex_color, login=""):
    """Цвет ника; слишком тёмные подсветляются, чтобы читались на тёмном фоне."""
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", hex_color or ""):
        hex_color = NICK_COLORS[zlib.crc32(login.lower().encode("utf-8")) % len(NICK_COLORS)]
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    for _ in range(4):
        if 0.2126 * r + 0.7152 * g + 0.0722 * b >= 80:
            break
        r = int(r + (255 - r) * 0.35)
        g = int(g + (255 - g) * 0.35)
        b = int(b + (255 - b) * 0.35)
    return "#%02x%02x%02x" % (r, g, b)


def fetch_emote(eid):
    """Скачивает смайлик (PNG, 28px) с кэшем на диске. Возвращает base64 или None."""
    eid = re.sub(r"[^A-Za-z0-9_-]", "", eid)
    if not eid:
        return None
    cached = EMOTE_CACHE.get(eid, _MISS)
    if cached is not _MISS:
        return cached
    data = None
    path = os.path.join(CACHE_DIR, eid + ".png")
    try:
        if os.path.isfile(path):
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
        else:
            url = "https://static-cdn.jtvnw.net/emoticons/v2/%s/static/dark/1.0" % eid
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                raw = r.read()
            os.makedirs(CACHE_DIR, exist_ok=True)
            write_cache_file(path, raw)
            data = base64.b64encode(raw).decode("ascii")
    except Exception:
        data = None
    EMOTE_CACHE.put(eid, data)
    return data


def fetch_badge_maps(channels):
    """Значки как в настоящем чате: ({'global': {set/version: url}, канал: {...}}, {канал: twitch_id}).

    Глобальные значки + значки каждого канала (сабы, битсы) берутся тем же
    GQL-запросом, что использует сайт twitch.tv, без логина. Вторым значением
    возвращает числовые id каналов (нужны для 7TV).
    """
    maps = {"global": {}}
    ids = {}
    for key, uid in FALLBACK_BADGES.items():
        maps["global"][key] = "https://static-cdn.jtvnw.net/badges/v1/%s/1" % uid
    try:
        q = ("query($logins: [String!]!){ badges { setID version imageURL(size: NORMAL) } "
             "users(logins: $logins) { id login broadcastBadges { setID version imageURL(size: NORMAL) } } }")
        body = json.dumps({"query": q, "variables": {"logins": list(channels)}}).encode("utf-8")
        req = urllib.request.Request(GQL_URL, data=body, headers={
            "Client-ID": GQL_CLIENT_ID,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        d = data.get("data") or {}
        for b in (d.get("badges") or []):
            if b and b.get("setID") and b.get("imageURL"):
                maps["global"]["%s/%s" % (b["setID"], b.get("version"))] = b["imageURL"]
        for u in (d.get("users") or []):
            if not u or not u.get("login"):
                continue
            if u.get("id"):
                ids[u["login"].lower()] = str(u["id"])
            cm = {}
            for b in (u.get("broadcastBadges") or []):
                if b and b.get("setID") and b.get("imageURL"):
                    cm["%s/%s" % (b["setID"], b.get("version"))] = b["imageURL"]
            maps[u["login"].lower()] = cm
    except Exception as e:
        dbg("! badge maps:", e)
    return maps, ids


def fetch_7tv_maps(channels, twitch_ids):
    """Карты смайлов 7TV: {'global': {имя: id}, канал: {имя: id}}."""
    maps = {}

    def load(url):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        g = load("https://7tv.io/v3/emote-sets/global")
        maps["global"] = {e["name"]: e["id"] for e in (g.get("emotes") or [])
                          if e.get("name") and e.get("id")}
    except Exception as e:
        dbg("! 7tv global:", e)
        maps["global"] = {}
    for ch in channels:
        tid = twitch_ids.get(ch)
        if not tid:
            continue
        try:
            u = load("https://7tv.io/v3/users/twitch/%s" % tid)
            es = (u.get("emote_set") or {}).get("emotes") or []
            maps[ch] = {e["name"]: e["id"] for e in es if e.get("name") and e.get("id")}
            dbg("7tv %s: %d emotes" % (ch, len(maps[ch])))
        except Exception as e:
            dbg("! 7tv %s:" % ch, e)  # канал не зарегистрирован в 7TV — это нормально
    return maps


def fetch_7tv_image(eid):
    """Скачивает смайл 7TV (webp), конвертирует в PNG высотой 28px. base64 или None."""
    if not HAS_PIL:
        return None
    eid = re.sub(r"[^A-Za-z0-9]", "", eid)
    if not eid:
        return None
    cached = SEVENTV_IMG_CACHE.get(eid, _MISS)
    if cached is not _MISS:
        return cached
    data = None
    path = os.path.join(SEVENTV_CACHE_DIR, eid + ".png")
    try:
        if os.path.isfile(path):
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
        else:
            url = "https://cdn.7tv.app/emote/%s/1x.webp" % eid
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                raw = r.read()
            img = _PILImage.open(_io.BytesIO(raw))
            img.seek(0)  # у анимированных берём первый кадр
            img = img.convert("RGBA")
            if img.height > 28:
                img = img.resize((max(1, round(img.width * 28 / img.height)), 28),
                                 _PILImage.LANCZOS)
            buf = _io.BytesIO()
            img.save(buf, format="PNG")
            png = buf.getvalue()
            os.makedirs(SEVENTV_CACHE_DIR, exist_ok=True)
            write_cache_file(path, png)
            data = base64.b64encode(png).decode("ascii")
    except Exception as e:
        dbg("! 7tv img:", e)
        data = None
    SEVENTV_IMG_CACHE.put(eid, data)
    return data


def fetch_badge_image(url):
    """Скачивает картинку значка (18px) с кэшем на диске. Возвращает base64 или None."""
    cached = BADGE_IMG_CACHE.get(url, _MISS)
    if cached is not _MISS:
        return cached
    data = None
    path = os.path.join(BADGE_CACHE_DIR, "%08x.png" % (zlib.crc32(url.encode("utf-8")) & 0xFFFFFFFF))
    try:
        if os.path.isfile(path):
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                raw = r.read()
            os.makedirs(BADGE_CACHE_DIR, exist_ok=True)
            write_cache_file(path, raw)
            data = base64.b64encode(raw).decode("ascii")
    except Exception:
        data = None
    BADGE_IMG_CACHE.put(url, data)
    return data


def badge_urls_for(badge_maps, channel, badges_tag):
    """Тег badges -> [(ключ, url картинки), ...] с мемоизацией резолва."""
    memo = badge_maps.setdefault("_memo", {})
    ready = badge_maps.get("_ready")
    out = []
    for b in (badges_tag or "").split(","):
        if "/" not in b:
            continue
        key = (channel, b)
        if ready and key in memo:
            url = memo[key]
        else:
            setname = b.split("/", 1)[0]
            url = None
            for m in (badge_maps.get(channel) or {}, badge_maps.get("global") or {}):
                url = m.get(b) or m.get(setname + "/1") or m.get(setname + "/0")
                if not url:
                    url = next((v for k, v in m.items() if k.startswith(setname + "/")), None)
                if url:
                    break
            if ready:  # до загрузки карт не запоминаем: резолв ещё неполный
                memo[key] = url
        if url:
            out.append((b, url))
    return out


def resolve_badges(badge_maps, channel, badges_tag):
    """Тег badges='moderator/1,subscriber/9' -> [(ключ, base64-картинка), ...]."""
    out = []
    for b, url in badge_urls_for(badge_maps, channel, badges_tag):
        b64 = fetch_badge_image(url)
        if b64:
            out.append((b, b64))
    return out


def validate_token(token):
    """Проверяет OAuth-токен через id.twitch.tv. Возвращает {'token','login','scopes'} или None."""
    token = (token or "").strip().strip('"').strip()
    if token.lower().startswith("oauth:"):
        token = token[6:]
    if not re.fullmatch(r"[A-Za-z0-9]{20,64}", token):
        return None
    try:
        req = urllib.request.Request("https://id.twitch.tv/oauth2/validate",
                                     headers={"Authorization": "OAuth " + token})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        login = (data.get("login") or "").lower()
        if login:
            return {"token": token, "login": login, "scopes": data.get("scopes") or []}
    except Exception as e:
        dbg("! validate:", e)
    return None


# ---------------------------------------------------------------- IRC поток

class IrcThread(threading.Thread):
    """Читает чат каналов (анонимно или с токеном) и кладёт события в очередь."""

    def __init__(self, channels, out_q, badge_maps, seventv_maps, token="", login=""):
        super().__init__(daemon=True)
        self.channels = [c.lower().lstrip("#") for c in channels]
        self.q = out_q
        self.badge_maps = badge_maps      # общие словари, заполняются отдельным потоком
        self.seventv_maps = seventv_maps
        self.token = token
        self.login = (login or "").lower()
        self.stop_event = threading.Event()
        self.sock = None
        self.send_lock = threading.Lock()
        self.userstate = {}  # канал -> (display-name, color) для эха своих сообщений
        self.nick = self.login if token else "justinfan%d" % random.randint(10000, 99999)

    def stop(self):
        self.stop_event.set()
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    def put(self, item):
        if self.stop_event.is_set():
            return
        while self.q.qsize() >= 800:  # при переполнении выбрасываем старое, не новое
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.q.put(item)

    def send_message(self, channel, text):
        """Отправка сообщения из UI-потока. True, если ушло в сокет."""
        if not self.token or not text:
            return False
        text = text[:500]  # лимит Twitch на длину сообщения
        try:
            with self.send_lock:
                if not self.sock:
                    return False
                self.sock.sendall(("PRIVMSG #%s :%s\r\n" % (channel, text)).encode("utf-8"))
            return True
        except OSError:
            return False

    def run(self):
        first = True
        while not self.stop_event.is_set():
            try:
                self.put(("sys", T("connecting") if first else T("reconnecting")))
                self._session()
            except Exception as e:
                if self.stop_event.is_set():
                    break
                dbg("! connection error:", e)
                self.put(("sys", T("conn_lost")))
                self.stop_event.wait(5)
            first = False

    def _send(self, line):
        with self.send_lock:
            self.sock.sendall((line + "\r\n").encode("utf-8"))

    def _session(self):
        self.sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=15)
        # Twitch пингует примерно раз в 5 минут; больший таймаут = обрыв
        self.sock.settimeout(420)
        self._send("CAP REQ :twitch.tv/tags twitch.tv/commands")
        if self.token:
            self._send("PASS oauth:" + self.token)
        self._send("NICK " + self.nick)
        # JOIN пачками по 10, чтобы не упереться в лимит Twitch на много каналов
        chans = ["#" + c for c in self.channels]
        for i in range(0, len(chans), 10):
            self._send("JOIN " + ",".join(chans[i:i + 10]))
            if i + 10 < len(chans):
                self.stop_event.wait(2)
        buf = b""
        while not self.stop_event.is_set():
            data = self.sock.recv(4096)
            if not data:
                raise ConnectionError("соединение закрыто")
            buf += data
            while b"\r\n" in buf:
                raw, buf = buf.split(b"\r\n", 1)
                self._handle(raw.decode("utf-8", "replace"))

    def _handle(self, line):
        dbg("<", line)
        if line.startswith("PING"):
            self._send("PONG :tmi.twitch.tv")
            return
        tags, prefix, cmd, args, trailing = parse_irc(line)
        channel = ""
        for a in args:
            if a.startswith("#"):
                channel = a.lstrip("#").lower()
                break
        if cmd == "PRIVMSG":
            login = prefix.split("!", 1)[0]
            name = tags.get("display-name") or login or "???"
            text = trailing or ""
            action = False
            if text.startswith("\x01ACTION ") and text.endswith("\x01"):
                action = True
                text = text[8:-1]
            self._prefetch_message(channel, text, tags.get("emotes", ""), tags.get("badges", ""))
            segs = self._apply_7tv(self._segments(text, tags.get("emotes", "")), channel)
            self.put(("msg", channel, name, tags.get("color", ""), segs, action,
                      resolve_badges(self.badge_maps, channel, tags.get("badges", "")),
                      login.lower()))
        elif cmd == "USERSTATE":
            self.userstate[channel] = (tags.get("display-name") or self.nick,
                                       tags.get("color", ""), tags.get("badges", ""))
        elif cmd == "GLOBALUSERSTATE":
            self.userstate["*"] = (tags.get("display-name") or self.nick,
                                   tags.get("color", ""), tags.get("badges", ""))
        elif cmd == "USERNOTICE":
            sysmsg = tags.get("system-msg", "")
            if sysmsg:
                self.put(("sys", sysmsg))
            if trailing:
                name = tags.get("display-name") or tags.get("login") or "?"
                self._prefetch_message(channel, trailing, tags.get("emotes", ""),
                                       tags.get("badges", ""))
                segs = self._apply_7tv(self._segments(trailing, tags.get("emotes", "")), channel)
                self.put(("msg", channel, name, tags.get("color", ""), segs, False,
                          resolve_badges(self.badge_maps, channel, tags.get("badges", "")),
                          (tags.get("login") or "").lower()))
        elif cmd == "NOTICE":
            if trailing:
                low = trailing.lower()
                if "authentication failed" in low or "improperly formatted auth" in low:
                    self.put(("auth_failed", trailing))
                    self.stop_event.set()
                    return
                self.put(("sys", ("#%s: " % channel if channel else "") + trailing))
        elif cmd == "JOIN" and prefix.startswith(self.nick + "!"):
            self.put(("sys", T("joined", channel)))
        elif cmd == "RECONNECT":
            raise ConnectionError("сервер запросил переподключение")

    def _prefetch_message(self, channel, text, emotes_tag, badges_tag):
        """Качает все незнакомые картинки сообщения параллельно (пул из 4 потоков).

        Дальше сборка сообщения идёт по тёплому кэшу. Ждём не дольше 2.5 с:
        что не успело — покажется текстом, а в кэш всё равно доедет.
        """
        jobs = []
        for part in (emotes_tag or "").split("/"):
            eid = re.sub(r"[^A-Za-z0-9_-]", "", part.partition(":")[0])
            if eid and eid not in EMOTE_CACHE:
                jobs.append(lambda e=eid: fetch_emote(e))
        if HAS_PIL:
            cm = self.seventv_maps.get(channel) or {}
            gm = self.seventv_maps.get("global") or {}
            if cm or gm:
                for w in set(text.split()):
                    sid = cm.get(w) or gm.get(w)
                    if sid and re.sub(r"[^A-Za-z0-9]", "", sid) not in SEVENTV_IMG_CACHE:
                        jobs.append(lambda s=sid: fetch_7tv_image(s))
        for _b, url in badge_urls_for(self.badge_maps, channel, badges_tag):
            if url not in BADGE_IMG_CACHE:
                jobs.append(lambda u=url: fetch_badge_image(u))
        if jobs:
            futures_wait([DOWNLOAD_POOL.submit(j) for j in jobs], timeout=2.5)

    def _apply_7tv(self, segs, channel):
        """Заменяет слова-смайлы 7TV в текстовых кусках на картинки."""
        if not HAS_PIL:
            return segs
        cm = self.seventv_maps.get(channel) or {}
        gm = self.seventv_maps.get("global") or {}
        if not cm and not gm:
            return segs
        out = []
        for seg in segs:
            if seg[0] != "t":
                out.append(seg)
                continue
            buf = []
            for w in re.split(r"(\s+)", seg[1]):  # пробелы сохраняем как есть
                eid = cm.get(w) or gm.get(w)
                b64 = fetch_7tv_image(eid) if eid else None
                if b64:
                    if buf:
                        out.append(("t", "".join(buf)))
                        buf = []
                    out.append(("e", "7tv" + eid, b64, w))
                else:
                    buf.append(w)
            if buf:
                out.append(("t", "".join(buf)))
        return out

    def _segments(self, text, emotes_tag):
        """Режет сообщение на куски: ('t', текст) и ('e', id, base64, альт-текст)."""
        if not emotes_tag:
            return [("t", text)]
        try:
            ranges = []
            for part in emotes_tag.split("/"):
                eid, _, spans = part.partition(":")
                for span in spans.split(","):
                    a, _, b = span.partition("-")
                    ranges.append((int(a), int(b), eid))
            ranges.sort()
            segs = []
            pos = 0
            for a, b, eid in ranges:
                if a < pos or b >= len(text):
                    raise ValueError("диапазон вне сообщения")
                if a > pos:
                    segs.append(("t", text[pos:a]))
                alt = text[a:b + 1]
                data = fetch_emote(eid)
                if data:
                    segs.append(("e", eid, data, alt))
                else:
                    segs.append(("t", alt))
                pos = b + 1
            if pos < len(text):
                segs.append(("t", text[pos:]))
            return segs
        except Exception:
            return [("t", text)]


# ---------------------------------------------------------------- окно

def _pill(canvas, x0, y0, x1, y1, fill, tag="pill"):
    """Рисует пилюлю (полностью скруглённый прямоугольник) на канвасе."""
    h = y1 - y0
    canvas.create_oval(x0, y0, x0 + h, y1, fill=fill, outline=fill, tags=tag)
    canvas.create_oval(x1 - h, y0, x1, y1, fill=fill, outline=fill, tags=tag)
    canvas.create_rectangle(x0 + h / 2, y0, x1 - h / 2, y1, fill=fill,
                            outline=fill, tags=tag)


def _lighten(color, k=0.12):
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    return "#%02x%02x%02x" % (int(r + (255 - r) * k), int(g + (255 - g) * k),
                              int(b + (255 - b) * k))


class RoundButton(tk.Canvas):
    """Кнопка-«пилюля» со скруглёнными краями."""

    def __init__(self, parent, text, command=None, fill=None, fg=None,
                 font=("Segoe UI", 9), padx=12, pady=4, parent_bg=None, bold=False):
        self._font = tkfont.Font(font=(font[0], font[1], "bold") if bold else font)
        self._padx, self._pady = padx, pady
        self._fill = fill or CHIPBTN_BG
        self._fg = fg or FG
        self._command = command
        w, h = self._size(text)
        super().__init__(parent, width=w, height=h, bg=parent_bg or BG,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._text = text
        self._draw()
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda e: self._paint(_lighten(self._fill)))
        self.bind("<Leave>", lambda e: self._paint(self._fill))

    def _size(self, text):
        return (self._font.measure(text) + self._padx * 2,
                self._font.metrics("linespace") + self._pady * 2)

    def _draw(self):
        self.delete("all")
        w, h = self._size(self._text)
        self.configure(width=w, height=h)
        _pill(self, 1, 1, w - 1, h - 1, self._fill)
        self.create_text(w / 2, h / 2, text=self._text, fill=self._fg,
                         font=self._font, tags="txt")

    def _paint(self, color):
        for item in self.find_withtag("pill"):
            self.itemconfigure(item, fill=color, outline=color)

    def _on_click(self, event=None):
        if self._command:
            self._command()

    def set_text(self, text):
        if text != self._text:
            self._text = text
            self._draw()

    def restyle(self, fill=None, fg=None, parent_bg=None):
        if fill:
            self._fill = fill
        if fg:
            self._fg = fg
        if parent_bg:
            self.configure(bg=parent_bg)
        self._draw()


class RoundEntry(tk.Canvas):
    """Поле ввода-«пилюля»: скруглённый фон, внутри обычный Entry."""

    def __init__(self, parent, font=("Segoe UI", 10), height=30,
                 fill=None, fg=None, parent_bg=None, show=None):
        super().__init__(parent, width=180, height=height, bg=parent_bg or BG,
                         highlightthickness=0, bd=0)
        self._fill = fill or ENTRY_BG
        self._h = height
        self.entry = tk.Entry(self, relief="flat", bd=0, bg=self._fill,
                              fg=fg or FG, insertbackground=fg or FG, font=font)
        if show:
            self.entry.configure(show=show)
        self._win = None
        self.bind("<Configure>", self._redraw)
        self.bind("<Button-1>", lambda e: self.entry.focus_set())

    def _redraw(self, event=None):
        self.delete("pill")
        w = self.winfo_width()
        h = self._h
        _pill(self, 1, 2, w - 1, h - 2, self._fill)
        self.tag_lower("pill")
        r = h // 2
        if self._win is None:
            self._win = self.create_window(r, h // 2, window=self.entry,
                                           anchor="w", width=max(10, w - 2 * r))
        else:
            self.coords(self._win, r, h // 2)
            self.itemconfigure(self._win, width=max(10, w - 2 * r))

    def restyle(self, fill=None, fg=None, parent_bg=None):
        if fill:
            self._fill = fill
            self.entry.configure(bg=fill)
        if fg:
            self.entry.configure(fg=fg, insertbackground=fg)
        if parent_bg:
            self.configure(bg=parent_bg)
        self._redraw()


class RoundSlider(tk.Canvas):
    """Светлый слайдер-«пилюля»: трек со скруглениями и круглая ручка."""

    def __init__(self, parent, from_, to, value, command=None, on_release=None,
                 length=170, height=24, parent_bg=None):
        super().__init__(parent, width=length, height=height,
                         bg=parent_bg or BG, highlightthickness=0, bd=0,
                         cursor="hand2")
        self.lo, self.hi = from_, to
        self.value = max(from_, min(to, value))
        self.command = command
        self.on_release = on_release
        self.length, self.h = length, height
        self.r = 8  # радиус ручки
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    def _x_for(self, value):
        pad = self.r + 2
        frac = (value - self.lo) / float(self.hi - self.lo)
        return pad + frac * (self.length - 2 * pad)

    def _draw(self):
        self.delete("all")
        cy = self.h / 2
        pad = self.r + 2
        x = self._x_for(self.value)
        self.create_line(pad, cy, self.length - pad, cy, width=6,
                         capstyle="round", fill=SLIDER_TRACK)
        self.create_line(pad, cy, max(pad + 1, x), cy, width=6,
                         capstyle="round", fill=ACCENT)
        self.create_oval(x - self.r, cy - self.r, x + self.r, cy + self.r,
                         fill=SLIDER_KNOB, outline=SLIDER_KNOB)

    def _drag(self, event):
        pad = self.r + 2
        frac = (event.x - pad) / float(self.length - 2 * pad)
        val = round(self.lo + max(0.0, min(1.0, frac)) * (self.hi - self.lo))
        if val != self.value:
            self.value = val
            self._draw()
            if self.command:
                self.command(val)

    def _release(self, event=None):
        if self.on_release:
            self.on_release()

    def set(self, value):
        self.value = max(self.lo, min(self.hi, int(value)))
        self._draw()


class Tooltip:
    """Маленькая всплывающая подсказка для кнопок (в tkinter её нет из коробки)."""

    def __init__(self, widget, text_key):
        self.widget = widget
        self.key = text_key
        self.tip = None
        self._after = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<Button-1>", self._hide, add="+")

    def _schedule(self, event=None):
        self._cancel()
        self._after = self.widget.after(550, self._show)

    def _show(self):
        if self.tip is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 6
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            self.tip = tk.Toplevel(self.widget)
            self.tip.overrideredirect(True)
            self.tip.attributes("-topmost", True)
            tk.Label(self.tip, text=T(self.key), bg=ENTRY_BG, fg=FG,
                     font=("Segoe UI", 9), padx=7, pady=3).pack()
            self.tip.geometry("+%d+%d" % (x, y))
            self.tip.update_idletasks()
            dwm_round(self.tip, small=True)
        except tk.TclError:
            self.tip = None

    def _cancel(self):
        if self._after:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _hide(self, event=None):
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


class OverlayApp:
    def __init__(self, root, cfg):
        self.root = root
        self.cfg = cfg
        self.q = queue.Queue()
        self.irc = None
        self.images = LruDict(600)  # ключ -> PhotoImage; LRU, чтобы память не росла
        self.known_tags = set()
        self._mention_re = None
        self._mention_name = ""
        self._key_state = {}
        self._drag = None
        self._resize = None
        self._flash_count = 0
        self.send_index = 0

        try:
            self.user32 = ctypes.windll.user32
        except Exception:
            self.user32 = None

        self.clickthrough = tk.BooleanVar(value=False)
        self.ghost = tk.BooleanVar(value=bool(cfg.get("ghost")))
        self.frameless = tk.BooleanVar(value=bool(cfg.get("frameless")))
        self.topmost = tk.BooleanVar(value=True)
        self.theme_var = tk.StringVar(value=cfg.get("theme", "twitch"))
        self.lang_var = tk.StringVar(value=cfg.get("lang", "ru"))
        self.settings_win = None

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=BORDER)

        frame = tk.Frame(root, bg=BG)
        frame.pack(fill="both", expand=True, padx=1, pady=1)
        self.frame = frame

        # --- шрифты в духе Twitch: полужирные сообщения, жирные ники ---
        fams = set(tkfont.families())
        base_family = next((f for f in ("Roobert", "Inter", "Segoe UI") if f in fams), "Segoe UI")
        semi_family = next((f for f in ("Roobert SemiBold", "Inter SemiBold", "Inter Medium",
                                        "Segoe UI Semibold") if f in fams), None)
        size = int(cfg.get("font_size", 11))
        self.font_msg = tkfont.Font(family=semi_family or base_family, size=size)
        self.font_nick = tkfont.Font(family=base_family, size=size, weight="bold")
        self.font_sys = tkfont.Font(family=base_family, size=max(8, size - 2), slant="italic")
        self.font_chip = tkfont.Font(family=base_family, size=max(7, size - 3))

        # --- верхняя полоса ---
        self.bar = tk.Frame(frame, bg=BAR_BG)
        self.bar.pack(fill="x")
        self.title_lbl = tk.Label(self.bar, text="Twitch", bg=BAR_BG, fg=FG,
                                  font=(base_family, 10, "bold"), padx=8, pady=4, cursor="fleur")
        self.title_lbl.pack(side="left")
        self.hint_lbl = tk.Label(self.bar, text="", bg=BAR_BG, fg=SYS_FG, font=(base_family, 8))
        self.hint_lbl.pack(side="left")
        self.close_btn = tk.Label(self.bar, text=" ✕ ", bg=BAR_BG, fg=BTN_FG,
                                  font=("Segoe UI", 10, "bold"), cursor="hand2")
        self.close_btn.pack(side="right", padx=(0, 4))
        self.gear_btn = tk.Label(self.bar, text=" ⚙ ", bg=BAR_BG, fg=BTN_FG,
                                 font=("Segoe UI", 10), cursor="hand2")
        self.gear_btn.pack(side="right")
        self.heart_btn = tk.Label(self.bar, text=" ♥ ", bg=BAR_BG, fg=ACCENT,
                                  font=("Segoe UI", 10, "bold"), cursor="hand2")
        self.heart_btn.pack(side="right")
        self.close_btn.bind("<Button-1>", lambda e: self.quit())
        self.gear_btn.bind("<Button-1>", self.open_settings)
        self.heart_btn.bind("<Button-1>", lambda e: webbrowser.open(DONATE_URL))
        self.heart_btn.bind("<Enter>", lambda e: self.heart_btn.configure(fg=ACCENT_HOVER))
        self.heart_btn.bind("<Leave>", lambda e: self.heart_btn.configure(fg=ACCENT))
        for b in (self.close_btn, self.gear_btn):
            b.bind("<Enter>", lambda e, w=b: w.configure(fg=FG))
            b.bind("<Leave>", lambda e, w=b: w.configure(fg=BTN_FG))

        # --- поле отправки (видно после входа в Twitch) ---
        self.input_bar = tk.Frame(frame, bg=BAR_BG)
        self.chan_btn = RoundButton(self.input_bar, "#", command=self.cycle_send_channel,
                                    fill=CHIPBTN_BG, fg=CHIP_FG, parent_bg=BAR_BG,
                                    font=("Segoe UI", 9))
        self.entry_pill = RoundEntry(self.input_bar, font=self.font_msg,
                                     fill=ENTRY_BG, fg=FG, parent_bg=BAR_BG)
        self.entry = self.entry_pill.entry
        self.entry.bind("<Return>", self.send_current)
        self.chan_btn.pack(side="left", padx=(6, 0), pady=5)
        self.entry_pill.pack(side="left", fill="x", expand=True, padx=(6, 16), pady=5)
        # плейсхолдер: подсказывает, что тут пишут в чат
        self._ph_on = False
        self.entry.bind("<FocusIn>", self._ph_clear)
        self.entry.bind("<FocusOut>", lambda e: self._ph_set())
        self._ph_set()

        # --- ленты чата: "*" — общий поток, плюс по одной на канал (вкладки) ---
        self.tab_bar = tk.Frame(frame, bg=BAR_BG)
        self._tab_btns = {}
        self.active_tab = "*"
        self.texts = {"*": self._make_text()}
        self.text = self.texts["*"]
        self.text.pack(fill="both", expand=True)

        self.grip = tk.Label(frame, text="◢", bg=BG, fg=GRIP_FG,
                             cursor="size_nw_se", font=("Segoe UI", 9))
        self.grip.place(relx=1.0, rely=1.0, anchor="se")

        # кнопка «вниз к новым сообщениям», появляется при прокрутке вверх
        self.newmsg_count = 0
        self.newmsg_btn = RoundButton(frame, "↓", command=self.jump_to_bottom,
                                      fill=CHIPBTN_BG, fg=FG, parent_bg=BG,
                                      font=("Segoe UI", 9), padx=12)

        for w, key in ((self.heart_btn, "tt_donate"), (self.gear_btn, "tt_menu"),
                       (self.close_btn, "tt_close"), (self.chan_btn, "tt_chan")):
            Tooltip(w, key)

        # --- события ---
        for w in (self.bar, self.title_lbl, self.hint_lbl):
            w.bind("<ButtonPress-1>", self.drag_start)
            w.bind("<B1-Motion>", self.drag_move)
            w.bind("<ButtonRelease-1>", lambda e: self.save_geometry())
        self.grip.bind("<ButtonPress-1>", self.resize_start)
        self.grip.bind("<B1-Motion>", self.resize_move)
        self.grip.bind("<ButtonRelease-1>", lambda e: self.save_geometry())
        for w in (root, self.bar, self.title_lbl):
            w.bind("<Button-3>", self.open_settings)

        self.place_window()
        root.deiconify()
        root.update_idletasks()
        dwm_round(root)  # скруглённые углы окна (Windows 11)
        self.apply_look()
        self.update_input_bar()
        self.update_mention_re()
        if self.frameless.get():
            self.apply_frameless(startup=True)

        self.connect(cfg["channels"])
        self.sys_message(T("hint_start", cfg["key_clickthrough"]["name"],
                           cfg["key_frameless"]["name"]))
        if not HAS_PIL:
            self.sys_message(T("pil_off"))
        self.poll_queue()
        self.poll_keys()
        self.keep_topmost()

    # ---------- меню ----------

    def open_settings(self, event=None):
        """Панель настроек: всё на одном экране, без вложенных меню."""
        if getattr(self, "settings_win", None) is not None and self.settings_win.winfo_exists():
            self.settings_win.deiconify()
            self.settings_win.lift()
            self.settings_win.focus_force()
            return
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.resizable(False, False)
        win.attributes("-topmost", True)
        self._fill_settings()
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.update_idletasks()
        w = win.winfo_width()
        ox, oy = self.root.winfo_x(), self.root.winfo_y()
        x = ox + self.root.winfo_width() + 10
        if x + w > self.root.winfo_screenwidth():
            x = ox - w - 10
        win.geometry("+%d+%d" % (x, max(0, oy)))

    def refresh_settings(self):
        if getattr(self, "settings_win", None) is not None and self.settings_win.winfo_exists():
            self._fill_settings()

    def _fill_settings(self):
        win = self.settings_win
        win.title(T("m_settings"))
        win.configure(bg=BG)
        for c in win.winfo_children():
            c.destroy()
        body = tk.Frame(win, bg=BG, padx=18, pady=14)
        body.pack(fill="both", expand=True)
        lbl_font = ("Segoe UI", 10)

        def row():
            f = tk.Frame(body, bg=BG)
            f.pack(fill="x", pady=3)
            return f

        def label(parent, key):
            tk.Label(parent, text=T(key), bg=BG, fg=FG, font=lbl_font, width=17,
                     anchor="w").pack(side="left")

        def header(key):
            tk.Label(body, text=T(key), bg=BG, fg=SYS_FG, font=("Segoe UI", 8, "bold"),
                     anchor="w").pack(fill="x", pady=(10, 2))

        def chip_btn(parent, text, cmd):
            return RoundButton(parent, text, command=cmd, fill=CHIPBTN_BG, fg=FG,
                               parent_bg=BG, font=("Segoe UI", 9))

        # --- каналы / аккаунт / упоминания ---
        r = row()
        label(r, "s_channels")
        pill = RoundEntry(r, font=("Segoe UI", 10), fill=ENTRY_BG, fg=FG, parent_bg=BG)
        self.set_chan_entry = pill.entry
        self.set_chan_entry.insert(0, ", ".join(self.cfg.get("channels") or []))
        pill.pack(side="left", fill="x", expand=True)
        self.set_chan_entry.bind("<Return>", lambda e: self._apply_channels_from_settings())
        chip_btn(r, T("s_apply"), self._apply_channels_from_settings).pack(side="left", padx=(6, 0))

        r = row()
        label(r, "s_account")
        if self.cfg.get("login"):
            tk.Label(r, text="@" + self.cfg["login"], bg=BG, fg=ACCENT,
                     font=lbl_font).pack(side="left")
            chip_btn(r, T("m_logout"), self._logout_and_refresh).pack(side="left", padx=(8, 0))
        else:
            chip_btn(r, T("m_login"), self.auth_dialog).pack(side="left")

        r = row()
        label(r, "s_mention")
        mpill = RoundEntry(r, font=("Segoe UI", 10), fill=ENTRY_BG, fg=FG, parent_bg=BG)
        self.set_mention_entry = mpill.entry
        self.set_mention_entry.insert(0, self.cfg.get("highlight_name")
                                      or self.cfg.get("login") or "")
        mpill.pack(side="left", fill="x", expand=True)
        self.set_mention_entry.bind("<Return>", self._save_mention_entry)
        self.set_mention_entry.bind("<FocusOut>", self._save_mention_entry)

        # --- режимы: переключатели ---
        header("s_modes")
        chk = dict(bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                   selectcolor=ENTRY_BG, font=lbl_font, anchor="w",
                   highlightthickness=0, bd=0, cursor="hand2")
        r = row()
        tk.Checkbutton(r, text=T("s_textonly"), variable=self.frameless,
                       command=self.apply_frameless, **chk).pack(side="left")
        chip_btn(r, self.cfg["key_frameless"]["name"],
                 lambda: self.rebind_key("key_frameless", T("d_key_textonly"),
                                         self.refresh_settings)).pack(side="right")
        r = row()
        tk.Checkbutton(r, text=T("s_click"), variable=self.clickthrough,
                       command=self.apply_clickthrough, **chk).pack(side="left")
        chip_btn(r, self.cfg["key_clickthrough"]["name"],
                 lambda: self.rebind_key("key_clickthrough", T("d_key_click"),
                                         self.refresh_settings)).pack(side="right")
        tk.Checkbutton(row(), text=T("m_topmost"), variable=self.topmost,
                       command=self.apply_topmost, **chk).pack(side="left")
        tk.Checkbutton(row(), text=T("m_ghost"), variable=self.ghost,
                       command=self.apply_look, **chk).pack(side="left")

        # --- вид: светлые слайдеры-пилюли ---
        header("s_appear")
        r = row()
        label(r, "m_opacity")
        op = RoundSlider(r, 30, 100, round(float(self.cfg.get("opacity", 0.88)) * 100),
                         command=self._on_opacity_slide,
                         on_release=lambda: save_config(self.cfg),
                         length=160, parent_bg=BG)
        op.pack(side="left")
        self._op_lbl = tk.Label(r, text="%d%%" % round(float(self.cfg.get("opacity", 0.88)) * 100),
                                bg=BG, fg=SYS_FG, font=lbl_font, width=5, anchor="e")
        self._op_lbl.pack(side="left")

        r = row()
        label(r, "m_fontsize")
        fsc = RoundSlider(r, 9, 18, int(self.cfg.get("font_size", 11)),
                          command=self._on_font_slide,
                          on_release=lambda: save_config(self.cfg),
                          length=160, parent_bg=BG)
        fsc.pack(side="left")
        self._fs_lbl = tk.Label(r, text=str(int(self.cfg.get("font_size", 11))),
                                bg=BG, fg=SYS_FG, font=lbl_font, width=5, anchor="e")
        self._fs_lbl.pack(side="left")

        rb = dict(bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                  selectcolor=ENTRY_BG, font=lbl_font, highlightthickness=0,
                  bd=0, cursor="hand2")
        r = row()
        label(r, "m_theme")
        tk.Radiobutton(r, text="Twitch", variable=self.theme_var, value="twitch",
                       command=self.set_theme, **rb).pack(side="left")
        tk.Radiobutton(r, text="Claude", variable=self.theme_var, value="claude",
                       command=self.set_theme, **rb).pack(side="left", padx=(10, 0))
        r = row()
        label(r, "m_lang")
        tk.Radiobutton(r, text="Русский", variable=self.lang_var, value="ru",
                       command=self.set_lang, **rb).pack(side="left")
        tk.Radiobutton(r, text="English", variable=self.lang_var, value="en",
                       command=self.set_lang, **rb).pack(side="left", padx=(10, 0))

        # --- низ: действия и ссылки ---
        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=(12, 8))
        r = row()
        chip_btn(r, T("m_clear"), self.clear_chat).pack(side="left")
        chip_btn(r, T("m_resetpos"), self.reset_position).pack(side="left", padx=(6, 0))
        r = row()
        sup = tk.Label(r, text=T("s_support"), bg=BG, fg=ACCENT,
                       font=("Segoe UI", 10, "bold"), cursor="hand2")
        sup.pack(side="left")
        sup.bind("<Button-1>", lambda e: webbrowser.open(DONATE_URL))
        ab = tk.Label(r, text=T("s_about"), bg=BG, fg=SYS_FG, font=lbl_font, cursor="hand2")
        ab.pack(side="left", padx=(14, 0))
        ab.bind("<Button-1>", lambda e: self.about_dialog())
        q = tk.Label(r, text=T("m_quit"), bg=BG, fg="#e06c6c", font=lbl_font, cursor="hand2")
        q.pack(side="right")
        q.bind("<Button-1>", lambda e: self.quit())

    def _apply_channels_from_settings(self):
        chans = parse_channels(self.set_chan_entry.get())
        if chans and chans != (self.cfg.get("channels") or []):
            self.send_index = 0
            self.connect(chans)
        self.refresh_settings()

    def _save_mention_entry(self, event=None):
        try:
            val = self.set_mention_entry.get().strip().lstrip("@").lower()
        except tk.TclError:
            return
        if val != (self.cfg.get("highlight_name") or ""):
            self.cfg["highlight_name"] = val
            save_config(self.cfg)
            self.update_mention_re()
            self.sys_message(T("mentions_now", val) if val else T("mentions_off"))

    def _logout_and_refresh(self):
        self.logout()
        self.refresh_settings()

    def _on_opacity_slide(self, val):
        v = int(float(val))
        self.cfg["opacity"] = v / 100.0
        if not self.ghost.get():
            try:
                self.root.attributes("-alpha", v / 100.0)
            except tk.TclError:
                pass
        try:
            self._op_lbl.configure(text="%d%%" % v)
        except tk.TclError:
            pass

    def _on_font_slide(self, val):
        size = int(float(val))
        if size == int(self.cfg.get("font_size", 11)):
            return
        self.cfg["font_size"] = size
        self.font_msg.configure(size=size)
        self.font_nick.configure(size=size)
        self.font_sys.configure(size=max(8, size - 2))
        self.font_chip.configure(size=max(7, size - 3))
        try:
            self._fs_lbl.configure(text=str(size))
        except tk.TclError:
            pass

    def set_lang(self):
        self.cfg["lang"] = self.lang_var.get()
        save_config(self.cfg)
        set_language(self.cfg["lang"])
        if getattr(self, "_ph_on", False):
            self._ph_on = False
            self.entry.delete(0, "end")
            self._ph_set()
        self.sys_message(T("lang_set"))
        self.refresh_settings()

    def about_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("О программе")
        dlg.configure(bg=BG, padx=28, pady=18)
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        tk.Label(dlg, text="Twitch Chat Overlay", bg=BG, fg=FG,
                 font=("Segoe UI", 13, "bold")).pack()
        tk.Label(dlg, text=T("about_sub"),
                 bg=BG, fg=SYS_FG, font=("Segoe UI", 9)).pack(pady=(2, 12))
        tk.Label(dlg, text=T("about_dev", AUTHOR), bg=BG, fg=FG,
                 font=("Segoe UI", 10)).pack()
        link = tk.Label(dlg, text=AUTHOR_URL.replace("https://", ""), bg=BG, fg=ACCENT,
                        font=("Segoe UI", 10, "underline"), cursor="hand2")
        link.pack()
        link.bind("<Button-1>", lambda e: webbrowser.open(AUTHOR_URL))
        tk.Button(dlg, text=T("about_support"),
                  command=lambda: webbrowser.open(DONATE_URL),
                  bg=ACCENT, fg="white", relief="flat", activebackground=ACCENT_ACTIVE,
                  activeforeground="white", font=("Segoe UI", 10, "bold"),
                  padx=14, pady=6, cursor="hand2").pack(pady=(14, 0))
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % ((dlg.winfo_screenwidth() - dlg.winfo_width()) // 2,
                                 (dlg.winfo_screenheight() - dlg.winfo_height()) // 2))

    # панель настроек открывается по ⚙ и правому клику (см. open_settings)

    def set_theme(self):
        self.cfg["theme"] = self.theme_var.get()
        save_config(self.cfg)
        self.apply_theme_live()
        self.refresh_settings()

    def apply_theme_live(self):
        """Перекрашивает все виджеты под выбранную тему без перезапуска."""
        apply_palette(self.cfg.get("theme", "twitch"))
        self.root.configure(bg=BORDER)
        self.frame.configure(bg=BG)
        self.bar.configure(bg=BAR_BG)
        self.title_lbl.configure(bg=BAR_BG, fg=FG)
        self.hint_lbl.configure(bg=BAR_BG, fg=SYS_FG)
        self.close_btn.configure(bg=BAR_BG, fg=BTN_FG)
        self.gear_btn.configure(bg=BAR_BG, fg=BTN_FG)
        self.heart_btn.configure(bg=BAR_BG, fg=ACCENT)
        self.input_bar.configure(bg=BAR_BG)
        self.chan_btn.restyle(fill=CHIPBTN_BG, fg=CHIP_FG, parent_bg=BAR_BG)
        self.entry_pill.restyle(fill=ENTRY_BG, fg=FG, parent_bg=BAR_BG)
        self.entry.configure(selectbackground=SELECT_BG,
                             fg=SYS_FG if getattr(self, "_ph_on", False) else FG)
        self.newmsg_btn.restyle(fill=CHIPBTN_BG, fg=FG, parent_bg=BG)
        for w in self.texts.values():
            w.configure(bg=BG, fg=FG, selectbackground=SELECT_BG)
            w.tag_configure("sys", foreground=SYS_FG)
            w.tag_configure("msg", foreground=FG)
            w.tag_configure("chip", foreground=CHIP_FG)
            w.tag_configure("mention", background=MENTION_BG)
        self.tab_bar.configure(bg=BAR_BG)
        for b in self.tab_bar.winfo_children():
            b.configure(bg=BAR_BG)
        self._style_tabs()
        self.grip.configure(bg=BG, fg=GRIP_FG)
        self.apply_look()

    # ---------- вход в Twitch ----------

    def mention_name(self):
        return (self.cfg.get("highlight_name") or self.cfg.get("login") or "").lower()

    def auth_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title(T("d_login_title"))
        dlg.configure(bg=BG, padx=18, pady=14)
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        tk.Label(dlg, bg=BG, fg=FG, justify="left", font=("Segoe UI", 10),
                 text=T("d_login_steps")).pack(anchor="w")
        btn_row = tk.Frame(dlg, bg=BG)
        btn_row.pack(fill="x", pady=(10, 4))
        tk.Button(btn_row, text=T("d_login_get"),
                  command=lambda: webbrowser.open(TOKEN_SITE),
                  bg=CHIPBTN_BG, fg=FG, relief="flat", activebackground=SELECT_BG,
                  activeforeground=FG, font=("Segoe UI", 9), padx=10, pady=3,
                  cursor="hand2").pack(side="left")
        entry = tk.Entry(dlg, font=("Consolas", 10), bg=ENTRY_BG, fg=FG,
                         insertbackground=FG, relief="flat", width=42, show="•")
        entry.pack(fill="x", pady=(8, 2), ipady=4)
        status = tk.Label(dlg, text=T("d_login_note"),
                          bg=BG, fg=SYS_FG, font=("Segoe UI", 9))
        status.pack(anchor="w")

        def ok(event=None):
            status.configure(text=T("d_login_checking"), fg=SYS_FG)
            dlg.update_idletasks()
            info = validate_token(entry.get())
            if not info:
                status.configure(text=T("d_login_bad"), fg="#ff6b6b")
                return
            self.cfg["token"] = info["token"]
            self.cfg["login"] = info["login"]
            if not self.cfg.get("highlight_name"):
                self.cfg["highlight_name"] = info["login"]
            save_config(self.cfg)
            self.update_mention_re()
            dlg.destroy()
            self.sys_message(T("logged_in", info["login"]))
            if "chat:edit" not in info.get("scopes", []):
                self.sys_message(T("no_chat_edit"))
            self.update_input_bar()
            self.connect(self.cfg["channels"])
            self.refresh_settings()

        row = tk.Frame(dlg, bg=BG)
        row.pack(fill="x", pady=(8, 0))
        tk.Button(row, text=T("d_login_btn"), command=ok, bg=ACCENT, fg="white", relief="flat",
                  activebackground=ACCENT_ACTIVE, activeforeground="white",
                  font=("Segoe UI", 10, "bold"), padx=16, pady=4, cursor="hand2").pack(side="right")
        entry.bind("<Return>", ok)
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % ((dlg.winfo_screenwidth() - dlg.winfo_width()) // 2,
                                 (dlg.winfo_screenheight() - dlg.winfo_height()) // 2))
        entry.focus_force()
        dlg.grab_set()

    def logout(self):
        self.cfg["token"] = ""
        self.cfg["login"] = ""
        save_config(self.cfg)
        self.update_input_bar()
        self.update_mention_re()
        self.sys_message(T("logged_out"))
        self.connect(self.cfg["channels"])

    # ---------- отправка сообщений ----------

    def update_input_bar(self):
        if self.cfg.get("token") and self.cfg.get("login"):
            self.input_bar.pack(side="bottom", fill="x", before=self.text)
            self.update_chan_btn()
        else:
            self.input_bar.pack_forget()
        self.grip.lift()

    def update_chan_btn(self):
        chans = self.cfg.get("channels") or []
        if self.send_index >= len(chans):
            self.send_index = 0
        if len(chans) > 1:
            self.chan_btn.set_text("#%s ▾" % chans[self.send_index])
            self.chan_btn.pack(side="left", padx=(6, 0), pady=5)
        elif chans:
            self.chan_btn.set_text("#%s" % chans[0])
            self.chan_btn.pack(side="left", padx=(6, 0), pady=5)
        else:
            self.chan_btn.pack_forget()

    def _ph_set(self):
        if not self._ph_on and not self.entry.get():
            self._ph_on = True
            self.entry.configure(fg=SYS_FG)
            self.entry.insert(0, T("placeholder"))

    def _ph_clear(self, event=None):
        if self._ph_on:
            self._ph_on = False
            self.entry.delete(0, "end")
            self.entry.configure(fg=FG)

    def jump_to_bottom(self, event=None):
        self.newmsg_count = 0
        self.newmsg_btn.place_forget()
        self.text.see("end")

    def _show_newmsg_btn(self):
        self.newmsg_btn.set_text(T("newmsgs", self.newmsg_count))
        self.newmsg_btn.place(in_=self.text, relx=0.5, rely=1.0, anchor="s", y=-8)

    def cycle_send_channel(self, event=None):
        chans = self.cfg.get("channels") or []
        if len(chans) > 1:
            self.send_index = (self.send_index + 1) % len(chans)
            self.update_chan_btn()

    def send_current(self, event=None):
        if getattr(self, "_ph_on", False):
            return
        text = self.entry.get().strip()[:500]
        chans = self.cfg.get("channels") or []
        if not text or not chans or not self.irc:
            return
        channel = chans[min(self.send_index, len(chans) - 1)]
        irc = self.irc
        if irc.send_message(channel, text):
            self.entry.delete(0, "end")

            def build_echo():
                # своё сообщение собираем как чужие: смайлы 7TV + значки из USERSTATE
                us = (irc.userstate.get(channel) or irc.userstate.get("*")
                      or (self.cfg.get("login") or "я", "", ""))
                name, color, btag = us[0], us[1], us[2] if len(us) > 2 else ""
                segs = irc._apply_7tv([("t", text)], channel)
                badges = resolve_badges(irc.badge_maps, channel, btag)
                irc.put(("msg", channel, name, color, segs, False, badges,
                         (self.cfg.get("login") or "").lower()))

            DOWNLOAD_POOL.submit(build_echo)
        else:
            self.sys_message(T("not_sent"))

    # ---------- внешний вид ----------

    def apply_look(self):
        self.cfg["ghost"] = bool(self.ghost.get())
        try:
            if self.ghost.get():
                self.root.attributes("-alpha", 1.0)
                self.root.attributes("-transparentcolor", BG)
            else:
                self.root.attributes("-transparentcolor", "")
                self.root.attributes("-alpha", float(self.cfg.get("opacity", 0.88)))
        except tk.TclError:
            pass
        save_config(self.cfg)

    def _force_topmost(self, enable):
        """Прямой SetWindowPos: надёжнее, чем tk-атрибут, который Tk кэширует."""
        if not self.user32:
            return
        try:
            hwnd = self.user32.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT
            # HWND_TOPMOST=-1 / HWND_NOTOPMOST=-2; NOSIZE|NOMOVE|NOACTIVATE
            self.user32.SetWindowPos(hwnd, -1 if enable else -2, 0, 0, 0, 0, 0x0013)
        except Exception:
            pass

    def apply_topmost(self):
        on = bool(self.topmost.get())
        try:
            self.root.attributes("-topmost", on)
        except tk.TclError:
            pass
        self._force_topmost(on)

    def keep_topmost(self):
        # каждые 2 секунды заново поднимаем окно на самый верх: помогает,
        # когда игра в безрамочном/полноэкранном режиме перебивает z-порядок
        if self.topmost.get():
            self._force_topmost(True)
        self.root.after(2000, self.keep_topmost)

    def apply_clickthrough(self):
        if not self.user32:
            return
        try:
            hwnd = self.user32.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x80000
            WS_EX_TRANSPARENT = 0x20
            style = self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if self.clickthrough.get():
                style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
            else:
                style &= ~WS_EX_TRANSPARENT
            self.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            return
        key = self.cfg["key_clickthrough"]["name"]
        if self.clickthrough.get():
            self.hint_lbl.configure(text=T("clickthrough_hint", key))
            self.sys_message(T("clickthrough_on", key))
        else:
            self.hint_lbl.configure(text="")

    def apply_frameless(self, startup=False):
        """Безрамочный режим: убирает рамку, полосу И фон — остаётся только текст чата."""
        on = bool(self.frameless.get())
        self.cfg["frameless"] = on
        save_config(self.cfg)
        if on:
            if not startup:
                self._ghost_before = bool(self.ghost.get())
            self.bar.pack_forget()
            self.tab_bar.pack_forget()
            self.input_bar.pack_forget()
            self.grip.place_forget()
            self.jump_to_bottom()
            self.frame.pack_configure(padx=0, pady=0)
            if not self.ghost.get():
                self.ghost.set(True)
            self.apply_look()
            if not startup:
                self.sys_message(T("textonly_on", self.cfg["key_frameless"]["name"]))
        else:
            self.frame.pack_configure(padx=1, pady=1)
            self.bar.pack(fill="x", before=self.text)
            if len(self.cfg.get("channels") or []) > 1:
                self.tab_bar.pack(fill="x", after=self.bar)
            self.update_input_bar()
            self.grip.place(relx=1.0, rely=1.0, anchor="se")
            self.ghost.set(bool(getattr(self, "_ghost_before", False)))
            self.apply_look()

    def _key_pressed(self, vk):
        """Глобальное нажатие клавиши (по фронту), даже когда окно без фокуса."""
        try:
            pressed = bool(self.user32.GetAsyncKeyState(int(vk)) & 0x8000)
        except Exception:
            pressed = False
        was = self._key_state.get(vk, False)
        self._key_state[vk] = pressed
        return pressed and not was

    def poll_keys(self):
        if self.user32:
            if self._key_pressed(self.cfg["key_clickthrough"]["vk"]):
                self.clickthrough.set(not self.clickthrough.get())
                self.apply_clickthrough()
            if self._key_pressed(self.cfg["key_frameless"]["vk"]):
                self.frameless.set(not self.frameless.get())
                self.apply_frameless()
        self.root.after(120, self.poll_keys)

    def rebind_key(self, which, title, on_done=None):
        """Диалог «нажмите клавишу» для переназначения бинда."""
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.configure(bg=BG, padx=24, pady=18)
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        cur = self.cfg[which]["name"]
        lbl = tk.Label(dlg, text=T("d_key_press", cur),
                       bg=BG, fg=FG, font=("Segoe UI", 11), justify="center")
        lbl.pack()

        def on_key(e):
            if e.keysym == "Escape":
                dlg.destroy()
                return
            if e.keycode in (16, 17, 18):  # Shift/Ctrl/Alt сами по себе не подходят
                lbl.configure(text=T("d_key_mod"))
                return
            other = "key_frameless" if which == "key_clickthrough" else "key_clickthrough"
            if e.keycode == self.cfg[other]["vk"]:
                lbl.configure(text=T("d_key_taken"))
                return
            self.cfg[which] = {"vk": int(e.keycode), "name": e.keysym.upper()
                               if len(e.keysym) == 1 else e.keysym}
            save_config(self.cfg)
            self.sys_message(T("bind_updated", title, self.cfg[which]["name"]))
            dlg.destroy()
            if on_done:
                on_done()

        dlg.bind("<KeyPress>", on_key)
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % ((dlg.winfo_screenwidth() - dlg.winfo_width()) // 2,
                                 (dlg.winfo_screenheight() - dlg.winfo_height()) // 2))
        dlg.focus_force()
        dlg.grab_set()

    def flash_bar(self):
        if self._flash_count > 0:
            return
        self._flash_count = 6
        self._flash_step()

    def _flash_step(self):
        color = ACCENT if self._flash_count % 2 == 1 else BAR_BG
        try:
            self.bar.configure(bg=color)
            for w in self.bar.winfo_children():
                w.configure(bg=color)
        except tk.TclError:
            return
        self._flash_count -= 1
        if self._flash_count > 0:
            self.root.after(320, self._flash_step)

    # ---------- геометрия ----------

    def place_window(self):
        w = int(self.cfg.get("width") or 380)
        h = int(self.cfg.get("height") or 480)
        x, y = self.cfg.get("x"), self.cfg.get("y")
        if x is None or y is None:
            x = self.root.winfo_screenwidth() - w - 24
            y = self.root.winfo_screenheight() - h - 90
        self.root.geometry("%dx%d+%d+%d" % (w, h, int(x), int(y)))

    def reset_position(self):
        self.cfg["x"] = self.cfg["y"] = None
        self.cfg["width"], self.cfg["height"] = 380, 480
        self.place_window()
        self.save_geometry()

    def save_geometry(self):
        try:
            self.cfg["x"] = self.root.winfo_x()
            self.cfg["y"] = self.root.winfo_y()
            self.cfg["width"] = self.root.winfo_width()
            self.cfg["height"] = self.root.winfo_height()
            save_config(self.cfg)
        except tk.TclError:
            pass

    def drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        if self._drag:
            self.root.geometry("+%d+%d" % (e.x_root - self._drag[0], e.y_root - self._drag[1]))

    def resize_start(self, e):
        self._resize = (e.x_root, e.y_root, self.root.winfo_width(), self.root.winfo_height())

    def resize_move(self, e):
        if self._resize:
            x0, y0, w0, h0 = self._resize
            w = max(240, w0 + (e.x_root - x0))
            h = max(160, h0 + (e.y_root - y0))
            self.root.geometry("%dx%d" % (w, h))

    # ---------- чат ----------

    def connect(self, channels):
        if self.irc:
            self.irc.stop()
        channels = [c for c in channels if c]
        self.cfg["channels"] = channels
        save_config(self.cfg)
        if len(channels) > 1:
            self.title_lbl.configure(text="#%s +%d" % (channels[0], len(channels) - 1))
        elif channels:
            self.title_lbl.configure(text="#" + channels[0])
        else:
            self.title_lbl.configure(text="Twitch")
        # ленты вкладок: по одной на канал + общая "*"
        for ch in list(self.texts.keys()):
            if ch != "*" and ch not in channels:
                self.texts[ch].destroy()
                del self.texts[ch]
        for ch in channels:
            if ch not in self.texts:
                self.texts[ch] = self._make_text()
        if self.cfg.get("active_tab") not in self.texts:
            self.cfg["active_tab"] = "*"
        self.switch_tab(self.cfg["active_tab"], force=True)
        self._rebuild_tabs()
        self.clear_chat()
        self.update_chan_btn()
        if not channels:
            self.sys_message(T("no_channels"))
            return
        # значки и смайлы 7TV подтягиваются в фоне и обновляются каждые 15 минут
        # (новые смайлы стримера появятся без переподключения)
        badge_maps = {}
        seventv_maps = {}
        self.irc = IrcThread(channels, self.q, badge_maps, seventv_maps,
                             token=self.cfg.get("token", ""),
                             login=self.cfg.get("login", ""))
        stop_event = self.irc.stop_event

        def load_assets():
            while True:
                try:
                    bm, ids = fetch_badge_maps(channels)
                    badge_maps.pop("_memo", None)
                    badge_maps.update(bm)
                    badge_maps["_ready"] = True
                    seventv_maps.update(fetch_7tv_maps(channels, ids))
                except Exception as e:
                    dbg("! assets:", e)
                if stop_event.wait(900):
                    break

        threading.Thread(target=load_assets, daemon=True).start()
        self.irc.start()


    def clear_chat(self):
        for w in self.texts.values():
            w.configure(state="normal")
            w.delete("1.0", "end")
            w.configure(state="disabled")

    def sys_message(self, msg):
        self.render(("sys", msg))

    def update_mention_re(self):
        name = self.mention_name()
        self._mention_name = name
        self._mention_re = (re.compile(r"(?<![a-z0-9_])@?%s(?![a-z0-9_])" % re.escape(name),
                                       re.IGNORECASE) if name else None)

    def poll_queue(self):
        items = []
        auth_failed = False
        try:
            for _ in range(80):
                item = self.q.get_nowait()
                if item[0] == "auth_failed":
                    auth_failed = True
                else:
                    items.append(item)
        except queue.Empty:
            pass
        if items:
            self.render_batch(items)
        if auth_failed:
            self.cfg["token"] = ""
            save_config(self.cfg)
            self.update_input_bar()
            self.render(("sys", T("auth_stale")))
            self.connect(self.cfg["channels"])
        # если сами доскроллили вниз — прячем кнопку «новые сообщения»
        if self.newmsg_count:
            try:
                if self.text.yview()[1] > 0.97:
                    self.jump_to_bottom()
            except tk.TclError:
                pass
        self.root.after(60, self.poll_queue)

    def color_tag(self, w, color, login, body=False):
        c = readable_color(color, login)
        tag = ("a" if body else "n") + c
        if tag not in w._ctags:
            w.tag_configure(tag, foreground=c,
                            font=self.font_msg if body else self.font_nick)
            w._ctags.add(tag)
        return tag

    def cached_image(self, key, b64):
        img = self.images.get(key, _MISS)
        if img is not _MISS:
            return img
        img = None
        if b64:
            try:
                img = tk.PhotoImage(data=b64)
            except tk.TclError:
                img = None
        self.images.put(key, img)
        return img

    def render(self, item):
        self.render_batch([item])

    def _msg_mention(self, item):
        if item[0] != "msg":
            return False
        name, segs = item[2], item[4]
        me = self._mention_name
        if not (self._mention_re is not None and me and name.lower() != me):
            return False
        full = " ".join(s[1] if s[0] == "t" else s[3] for s in segs)
        return bool(self._mention_re.search(full))

    def render_batch(self, items):
        """Раскладывает пачку по лентам: общий поток «*» + вкладки каналов."""
        if not items:
            return
        flagged = [(it, self._msg_mention(it)) for it in items]
        mention_any = False
        for key, w in list(self.texts.items()):
            if key == "*":
                sel = flagged
            else:
                sel = [(it, hit) for it, hit in flagged
                       if it[0] == "sys" or (it[0] == "msg" and it[1] == key)]
            if sel and self._render_into(w, sel):
                mention_any = True
        if mention_any:
            self.flash_bar()

    def _render_into(self, w, flagged):
        """Пачка сообщений в одну ленту за одно переключение state."""
        try:
            at_bottom = w.yview()[1] > 0.97
        except tk.TclError:
            return False
        w.configure(state="normal")
        hit_any = False
        for item, hit in flagged:
            try:
                self._render_item(w, item, hit)
                hit_any = hit_any or hit
            except tk.TclError:
                break
        last = int(w.index("end-1c").split(".")[0])
        maxm = int(self.cfg.get("max_messages", 150))
        if last > maxm:
            w.delete("1.0", "%d.0" % (last - maxm + 1))
        w.configure(state="disabled")
        if w is self.text:
            if at_bottom:
                w.see("end")
            else:
                # пользователь листает историю — считаем новые сообщения внизу
                fresh = sum(1 for it, _ in flagged if it[0] == "msg")
                if fresh and not self.frameless.get():
                    self.newmsg_count += fresh
                    self._show_newmsg_btn()
        return hit_any

    def _make_text(self):
        """Создаёт ленту чата со всеми тегами и биндами (одна на вкладку)."""
        w = tk.Text(self.frame, bg=BG, fg=FG, bd=0, highlightthickness=0,
                    wrap="word", state="disabled", padx=8, pady=6,
                    cursor="arrow", font=self.font_msg, spacing1=3, spacing3=1,
                    selectbackground=SELECT_BG)
        w.tag_configure("sys", foreground=SYS_FG, font=self.font_sys)
        w.tag_configure("msg", foreground=FG, font=self.font_msg)
        w.tag_configure("chip", foreground=CHIP_FG, font=self.font_chip)
        w.tag_configure("mention", background=MENTION_BG)
        w.tag_configure("atbold", font=self.font_nick)
        w.tag_bind("nicklink", "<Enter>", lambda e, t=w: t.configure(cursor="hand2"))
        w.tag_bind("nicklink", "<Leave>", lambda e, t=w: t.configure(cursor="arrow"))
        w.bind("<Button-3>", self.open_settings)
        w._utags = set()   # кликабельные ники этой ленты
        w._ctags = set()   # цветовые теги этой ленты
        return w

    def _rebuild_tabs(self):
        for c in self.tab_bar.winfo_children():
            c.destroy()
        self._tab_btns = {}
        chans = self.cfg.get("channels") or []
        if len(chans) < 2:
            self.tab_bar.pack_forget()
            return
        for key, text in [("*", T("tab_all"))] + [(c, "#" + c) for c in chans]:
            b = tk.Label(self.tab_bar, text=text, bg=BAR_BG, fg=SYS_FG,
                         font=("Segoe UI", 9), padx=8, pady=3, cursor="hand2")
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, k=key: self.switch_tab(k))
            self._tab_btns[key] = b
        self._style_tabs()
        if not self.frameless.get():
            self.tab_bar.pack(fill="x", after=self.bar)

    def _style_tabs(self):
        for key, b in self._tab_btns.items():
            active = key == self.active_tab
            b.configure(fg=ACCENT if active else SYS_FG,
                        font=("Segoe UI", 9, "bold" if active else "normal"))

    def switch_tab(self, key, force=False):
        if key not in self.texts:
            key = "*"
        if key == self.active_tab and not force:
            return
        self.newmsg_count = 0
        self.newmsg_btn.place_forget()
        try:
            if self.text.winfo_exists():
                self.text.pack_forget()
        except tk.TclError:
            pass
        self.active_tab = key
        self.cfg["active_tab"] = key
        save_config(self.cfg)
        self.text = self.texts[key]
        self.text.pack(fill="both", expand=True)
        self.grip.lift()
        self.text.see("end")
        self._style_tabs()
        # на вкладке канала сообщения отправляются в него
        chans = self.cfg.get("channels") or []
        if key in chans:
            self.send_index = chans.index(key)
            self.update_chan_btn()

    def user_tag(self, w, login):
        """Тег «клик по нику -> профиль twitch.tv/login» (создаётся один раз)."""
        login = re.sub(r"[^a-z0-9_]", "", (login or "").lower())
        if not login:
            return None
        tag = "u:" + login
        if tag not in w._utags:
            w.tag_bind(tag, "<Button-1>",
                       lambda e, l=login: webbrowser.open("https://twitch.tv/" + l))
            w._utags.add(tag)
        return tag

    _AT_RE = re.compile(r"(@[A-Za-z0-9_]{3,25})")

    def _insert_body_text(self, w, part, body_tag):
        """Текст сообщения; @упоминания внутри — жирные и кликабельные."""
        for i, chunk in enumerate(self._AT_RE.split(part)):
            if not chunk:
                continue
            if i % 2:  # нечётные куски — сами @упоминания
                utag = self.user_tag(w, chunk[1:])
                tags = (body_tag, "atbold", "nicklink") + ((utag,) if utag else ())
                w.insert("end", chunk, tags)
            else:
                w.insert("end", chunk, body_tag)

    def _render_item(self, w, item, mention_hit=False):
        if item[0] == "sys":
            w.insert("end", item[1] + "\n", "sys")
            return
        _, channel, name, color, segs, action, badges = item[:7]
        login = item[7] if len(item) > 7 else ""
        line_no = int(w.index("end-1c").split(".")[0])
        multi = len(self.cfg.get("channels") or []) > 1
        if multi and channel and w is self.texts.get("*"):
            w.insert("end", "#%s " % channel, "chip")
        for bkey, b64 in badges:
            bimg = self.cached_image("b:" + bkey, b64)
            if bimg is not None:
                w.image_create("end", image=bimg, padx=2)
        utag = self.user_tag(w, login or name)
        nick_tags = (self.color_tag(w, color, name), "nicklink") + ((utag,) if utag else ())
        w.insert("end", name, nick_tags)
        body_tag = self.color_tag(w, color, name, body=True) if action else "msg"
        w.insert("end", " " if action else ": ", "msg")
        for seg in segs:
            if seg[0] == "t":
                self._insert_body_text(w, seg[1], body_tag)
            else:
                _, eid, b64, alt = seg
                img = self.cached_image("e:" + eid, b64)
                if img is not None:
                    w.image_create("end", image=img, padx=2)
                else:
                    w.insert("end", alt, body_tag)
        w.insert("end", "\n")
        if mention_hit:
            w.tag_add("mention", "%d.0" % line_no, "%d.end" % line_no)

    def quit(self):
        self.save_geometry()
        if self.irc:
            self.irc.stop()
        self.root.destroy()


# ---------------------------------------------------------------- диалоги

def ask_text(root, title, label, current=""):
    """Тёмный диалог с одним полем ввода. Возвращает строку или None."""
    result = {"value": None}
    dlg = tk.Toplevel(root)
    dlg.title(title)
    dlg.configure(bg=BG, padx=18, pady=14)
    dlg.resizable(False, False)
    dlg.attributes("-topmost", True)
    tk.Label(dlg, text=label, bg=BG, fg=FG, font=("Segoe UI", 11)).pack(anchor="w")
    entry = tk.Entry(dlg, font=("Segoe UI", 12), bg=ENTRY_BG, fg=FG,
                     insertbackground=FG, relief="flat", width=32)
    entry.pack(fill="x", pady=(8, 2), ipady=4)
    entry.insert(0, current)

    def ok(event=None):
        result["value"] = entry.get()
        dlg.destroy()

    row = tk.Frame(dlg, bg=BG)
    row.pack(fill="x", pady=(8, 0))
    tk.Button(row, text=T("d_ok"), command=ok, bg=ACCENT, fg="white", relief="flat",
              activebackground=ACCENT_ACTIVE, activeforeground="white",
              font=("Segoe UI", 10, "bold"), padx=16, pady=4, cursor="hand2").pack(side="right")
    entry.bind("<Return>", ok)
    dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
    dlg.update_idletasks()
    dlg.geometry("+%d+%d" % ((dlg.winfo_screenwidth() - dlg.winfo_width()) // 2,
                             (dlg.winfo_screenheight() - dlg.winfo_height()) // 2))
    entry.focus_force()
    dlg.grab_set()
    root.wait_window(dlg)
    return result["value"]


def ask_channels_first(root):
    while True:
        val = ask_text(root, T("d_channels_title"), T("d_first_label"), "")
        if val is None:
            return None
        chans = parse_channels(val)
        if chans:
            return chans


def cleanup_disk_caches():
    """Дисковые кэши не растут бесконечно: свыше 3000 файлов — трём самое старое."""
    for d in (CACHE_DIR, BADGE_CACHE_DIR, SEVENTV_CACHE_DIR):
        try:
            files = [e for e in os.scandir(d) if e.is_file()]
        except OSError:
            continue
        if len(files) <= 3000:
            continue
        try:
            files.sort(key=lambda e: e.stat().st_mtime)
            for e in files[:len(files) - 2000]:
                try:
                    os.remove(e.path)
                except OSError:
                    pass
        except OSError:
            pass


def already_running():
    """Один экземпляр: повторный запуск молча выходит (оверлей уже на экране)."""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW(None, False, "Local\\TwitchChatOverlayMutex")
        return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return False


def main():
    if DEBUG:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if already_running():
        return
    # чёткий текст при масштабировании Windows
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    cfg = load_config()
    apply_palette(cfg.get("theme", "twitch"))
    set_language(cfg.get("lang", "ru"))
    if "--channel" in sys.argv:
        i = sys.argv.index("--channel")
        if i + 1 < len(sys.argv):
            chans = parse_channels(sys.argv[i + 1])
            if chans:
                cfg["channels"] = chans

    threading.Thread(target=cleanup_disk_caches, daemon=True).start()

    root = tk.Tk()
    root.withdraw()
    try:
        root.iconbitmap(default=os.path.join(APP_DIR, "overlay.ico"))
    except Exception:
        pass

    if not cfg["channels"]:
        chans = ask_channels_first(root)
        if not chans:
            root.destroy()
            return
        cfg["channels"] = chans

    OverlayApp(root, cfg)
    root.mainloop()


if __name__ == "__main__":
    main()
