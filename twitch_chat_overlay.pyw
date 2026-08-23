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
import ssl
import sys
import threading
import urllib.error
import urllib.parse
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
    from PIL import ImageDraw as _PILDraw
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


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def draw_mod_icon(kind, size, hex_color):
    """Рисует иконку модерации (ban/timeout/warn/delete) в едином стиле.

    Возвращает base64 PNG с прозрачным фоном или None (без Pillow). Рисуется
    в 4x с последующим уменьшением — получается гладкая антиалиасная линия.
    """
    if not HAS_PIL:
        return None
    ss = 4
    S = size * ss
    col = _hex_to_rgb(hex_color) + (255,)
    img = _PILImage.new("RGBA", (S, S), (0, 0, 0, 0))
    d = _PILDraw.Draw(img)
    lw = max(2, round(S / 11))
    m = round(S * 0.14)
    c = S / 2
    if kind == "ban":
        d.ellipse([m, m, S - m, S - m], outline=col, width=lw)
        r = (S - 2 * m) / 2
        off = r * 0.707
        d.line([c - off, c - off, c + off, c + off], fill=col, width=lw)
    elif kind == "timeout":
        d.ellipse([m, m, S - m, S - m], outline=col, width=lw)
        rr = (S - 2 * m) / 2
        d.line([c, c, c, c - rr * 0.58], fill=col, width=lw)   # часовая на 12
        d.line([c, c, c + rr * 0.5, c], fill=col, width=lw)    # минутная на 3
    elif kind == "warn":
        top, bot = m * 0.7, S - m
        d.line([c, top, S - m, bot], fill=col, width=lw, joint="curve")
        d.line([S - m, bot, m, bot], fill=col, width=lw, joint="curve")
        d.line([m, bot, c, top], fill=col, width=lw, joint="curve")
        d.line([c, S * 0.42, c, S * 0.66], fill=col, width=lw)      # палочка «!»
        d.ellipse([c - lw * 0.6, S * 0.74, c + lw * 0.6, S * 0.74 + lw * 1.2], fill=col)
    elif kind == "delete":
        top = S * 0.30
        d.line([m * 0.9, top, S - m * 0.9, top], fill=col, width=lw)        # крышка
        d.line([c - S * 0.11, top - S * 0.10, c + S * 0.11, top - S * 0.10],
               fill=col, width=lw)                                          # ручка
        body = [S * 0.26, top, S * 0.74, top, S * 0.68, S - m, S * 0.32, S - m]
        d.line(body[0:4] + [S * 0.32, S - m], fill=col, width=lw, joint="curve")
        d.line([S * 0.30, top, S * 0.34, S - m], fill=col, width=lw)        # левая стенка
        d.line([S * 0.70, top, S * 0.66, S - m], fill=col, width=lw)        # правая стенка
        d.line([S * 0.34, S - m, S * 0.66, S - m], fill=col, width=lw)      # дно
        for fx in (0.43, 0.5, 0.57):                                         # рёбра
            d.line([S * fx, top + S * 0.10, S * fx, S - m - S * 0.06],
                   fill=col, width=max(2, lw - 1))
    elif kind == "emote":
        d.ellipse([m, m, S - m, S - m], outline=col, width=lw)
        r_eye = lw * 0.7
        for ex in (c - S * 0.13, c + S * 0.13):
            d.ellipse([ex - r_eye, S * 0.40 - r_eye, ex + r_eye, S * 0.40 + r_eye],
                      fill=col)
        d.arc([c - S * 0.18, S * 0.34, c + S * 0.18, S * 0.66], 25, 155,
              fill=col, width=lw)
    elif kind == "announce":
        d.polygon([(S * 0.20, S * 0.40), (S * 0.20, S * 0.60),
                   (S * 0.42, S * 0.60), (S * 0.62, S * 0.76),
                   (S * 0.62, S * 0.24), (S * 0.42, S * 0.40)],
                  outline=col, width=lw)
        d.arc([S * 0.66, S * 0.36, S * 0.86, S * 0.64], -55, 55, fill=col, width=lw)
    img = img.resize((size, size), _PILImage.LANCZOS)
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def dwm_round(widget, small=False):
    """Скругляет углы окна средствами Windows 11 (на Windows 10 просто игнор)."""
    try:
        hwnd = ctypes.windll.user32.GetAncestor(widget.winfo_id(), 2)
        pref = ctypes.c_int(3 if small else 2)  # DWMWCP_ROUNDSMALL / DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
    except Exception:
        pass


apply_palette("twitch")

CHROMA_KEY = "#ff00ff"  # пурпурный фон для захвата окна в OBS (фильтр «Цветовой ключ»)

# единый TLS-контекст с проверкой сертификатов на все сетевые запросы —
# делаем верификацию явной и одинаковой везде (а не полагаемся на умолчание)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = True
SSL_CTX.verify_mode = ssl.CERT_REQUIRED

DEBUG = "--debug" in sys.argv

DEFAULTS = {
    "channels": [],
    "favorites": [],
    "sets": {},
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
    "layout": "tabs",
    "animations": True,
    "obs_chroma": False,
    "mod_icons": True,
    "key_clickthrough": {"vk": 119, "name": "F8"},
    "key_frameless": {"vk": 120, "name": "F9"},
    "key_expand": {"vk": 121, "name": "F10"},
    "key_fullscreen": {"vk": 122, "name": "F11"},
    "key_ghostinput": {"vk": 45, "name": "Insert"},
    "ghost_input": False,
    "exp_geometry": None,
    "recent_emotes": [],
    "max_messages": 150,
    "token": "",
    "login": "",
    "highlight_name": "",
}

# ---------------------------------------------------------------- язык

LANG = "ru"

STRINGS = {
    "ru": {
        "hint_start": ("Правый клик или ⚙ — настройки · %s — сквозные клики · "
                       "%s — только текст · %s — развернуть"),
        "s_expand": "Развёрнутый мультичат",
        "d_key_expand": "Развернуть мультичат",
        "s_fullscreen": "Во весь экран",
        "d_key_fullscreen": "Во весь экран",
        "tt_min": "Свернуть",
        "s_ghostinput": "Текст + поле ввода",
        "d_key_ghostinput": "Текст + поле ввода",
        "ghostinput_on": "Прозрачный чат с полем ввода. %s — вернуть окно.",
        "tt_emotes": "Смайлы (Twitch и 7TV)",
        "ep_search": "Поиск смайла…",
        "s_channels": "Каналы",
        "s_apply": "OK",
        "s_fav": "Избранное",
        "s_sets": "Наборы",
        "s_addfav": "★ в избранное",
        "s_saveset": "＋ набор",
        "set_name_title": "Сохранить набор",
        "set_name_label": "Название набора (напр. «вечер»):",
        "fav_hint": "Клик — добавить канал в список выше",
        "set_hint": "Клик — загрузить набор, ✕ — удалить",
        "fav_empty": "Пусто. Наберите каналы выше и нажмите «★ в избранное».",
        "s_layout": "Раскладка",
        "lay_tabs": "Вкладки",
        "lay_unified": "Общая лента",
        "lay_columns": "Колонки",
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
        "s_anim": "Анимация смайлов",
        "s_chroma": "Хромакей для OBS",
        "act_profile": "Открыть профиль",
        "act_delete": "Удалить сообщение",
        "act_timeout": "Таймаут 10 мин",
        "act_warn": "Предупредить",
        "act_ban": "Забанить",
        "act_ban_confirm": "Точно забанить?",
        "s_modicons": "Кнопки модерации в чате",
        "ban_arm": "Ещё раз по значку бана в течение 3 сек — бан %s",
        "ann_sent": "📢 Анонс отправлен",
        "tt_announce": "Отправить как анонс",
        "mod_warned": "⚠ %s получил предупреждение",
        "warn_reason": "Предупреждение от модератора",
        "mod_deleted": "✂ Сообщение %s удалено",
        "mod_timeout": "⏱ Таймаут %s на 10 минут",
        "mod_banned": "🔨 %s забанен",
        "mod_err_scope": ("Нет прав в токене: перелогиньтесь, отметив moderator:manage:"
                          "banned_users, chat_messages и warnings."),
        "mod_err_notmod": "Не получилось: похоже, у вас нет модерки на #%s.",
        "mod_err": "Модерация: ошибка %s",
        "chroma_on": ("Фон для захвата окна стал пурпурным. В OBS: правый клик по источнику → "
                      "«Фильтры» → «Цветовой ключ», цвет — пурпурный. На вашем экране всё как раньше."),
        "chroma_off": "Хромакей для OBS выключен.",
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
                          "3. Скопируйте ACCESS TOKEN и вставьте сюда\n\n"
                          "Модераторам: чтобы банить, варнить и удалять\n"
                          "из оверлея, отметьте на сайте ещё три права:\n"
                          "moderator:manage:banned_users,\n"
                          "moderator:manage:chat_messages,\n"
                          "moderator:manage:warnings"),
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
        "hint_start": ("Right-click or ⚙ — settings · %s — click-through · "
                       "%s — text only · %s — expand"),
        "s_expand": "Expanded multichat",
        "d_key_expand": "Expand multichat",
        "s_fullscreen": "Fullscreen",
        "d_key_fullscreen": "Fullscreen",
        "tt_min": "Minimize",
        "s_ghostinput": "Text + input box",
        "d_key_ghostinput": "Text + input box",
        "ghostinput_on": "Transparent chat with the input box. %s — bring the window back.",
        "tt_emotes": "Emotes (Twitch & 7TV)",
        "ep_search": "Search emotes…",
        "s_channels": "Channels",
        "s_apply": "OK",
        "s_fav": "Favorites",
        "s_sets": "Sets",
        "s_addfav": "★ favorite",
        "s_saveset": "＋ set",
        "set_name_title": "Save set",
        "set_name_label": "Set name (e.g. “evening”):",
        "fav_hint": "Click to add a channel to the list above",
        "set_hint": "Click to load a set, ✕ to delete",
        "fav_empty": "Empty. Type channels above and click “★ favorite”.",
        "s_layout": "Layout",
        "lay_tabs": "Tabs",
        "lay_unified": "Unified",
        "lay_columns": "Columns",
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
        "s_anim": "Animated emotes",
        "s_chroma": "OBS chroma key",
        "act_profile": "Open profile",
        "act_delete": "Delete message",
        "act_timeout": "Timeout 10 min",
        "act_warn": "Warn",
        "act_ban": "Ban",
        "act_ban_confirm": "Really ban?",
        "s_modicons": "Mod buttons in chat",
        "ban_arm": "Click the ban icon again within 3 s to ban %s",
        "ann_sent": "📢 Announcement sent",
        "tt_announce": "Send as announcement",
        "mod_warned": "⚠ %s was warned",
        "warn_reason": "Moderator warning",
        "mod_deleted": "✂ Message by %s deleted",
        "mod_timeout": "⏱ %s timed out for 10 minutes",
        "mod_banned": "🔨 %s banned",
        "mod_err_scope": ("Token lacks moderator scopes: re-login with moderator:manage:"
                          "banned_users, chat_messages and warnings."),
        "mod_err_notmod": "Failed: you don't seem to be a moderator on #%s.",
        "mod_err": "Moderation: error %s",
        "chroma_on": ("Window-capture background is now magenta. In OBS: right-click the source → "
                      "Filters → Color Key, key color magenta. Your own screen is unchanged."),
        "chroma_off": "OBS chroma key is off.",
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
                          "3. Copy the ACCESS TOKEN and paste it here\n\n"
                          "Moderators: to ban/warn/delete from the overlay,\n"
                          "also tick three scopes on that site:\n"
                          "moderator:manage:banned_users,\n"
                          "moderator:manage:chat_messages,\n"
                          "moderator:manage:warnings"),
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

    def pop(self, k):
        with self._lock:
            self._d.pop(k, None)


EMOTE_CACHE = LruDict(500)        # id -> base64 PNG или None (не удалось скачать)
BADGE_IMG_CACHE = LruDict(300)    # url -> base64 PNG или None
SEVENTV_IMG_CACHE = LruDict(600)  # 7tv id -> base64 PNG или None
DOWNLOAD_POOL = ThreadPoolExecutor(max_workers=8)  # параллельная докачка картинок


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
    favs = []
    for c in cfg.get("favorites") or []:
        c = extract_channel(str(c))
        if c and c not in favs:
            favs.append(c)
    cfg["favorites"] = favs
    sets = {}
    if isinstance(cfg.get("sets"), dict):
        for name, chlist in cfg["sets"].items():
            clean = []
            for c in (chlist or []):
                c = extract_channel(str(c))
                if c and c not in clean:
                    clean.append(c)
            if str(name).strip() and clean:
                sets[str(name).strip()[:24]] = clean
    cfg["sets"] = sets
    rec = []
    for t in cfg.get("recent_emotes") or []:
        if isinstance(t, (list, tuple)) and len(t) == 3 and all(isinstance(x, str) for x in t):
            rec.append(list(t))
    cfg["recent_emotes"] = rec[:24]
    for key, default in (("key_clickthrough", DEFAULTS["key_clickthrough"]),
                         ("key_frameless", DEFAULTS["key_frameless"]),
                         ("key_expand", DEFAULTS["key_expand"]),
                         ("key_fullscreen", DEFAULTS["key_fullscreen"]),
                         ("key_ghostinput", DEFAULTS["key_ghostinput"])):
        v = cfg.get(key)
        if not (isinstance(v, dict) and isinstance(v.get("vk"), int) and v.get("name")):
            cfg[key] = dict(default)
    # F7 конфликтовала с браузерами (caret browsing) — старый дефолт мигрируем
    if cfg.get("key_ghostinput") == {"vk": 118, "name": "F7"}:
        cfg["key_ghostinput"] = dict(DEFAULTS["key_ghostinput"])
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


def _decode_frames(raw, target_h=None):
    """Pillow: анимация (gif/webp) -> (кадры PNG base64, задержки мс)."""
    frames, delays = [], []
    img = _PILImage.open(_io.BytesIO(raw))
    n = getattr(img, "n_frames", 1)
    step = max(1, (n + 31) // 32)  # не больше ~32 кадров (CPU и память)
    for i in range(0, n, step):
        img.seek(i)
        fr = img.convert("RGBA")
        if target_h and fr.height > target_h:
            fr = fr.resize((max(1, round(fr.width * target_h / fr.height)), target_h),
                           _PILImage.LANCZOS)
        buf = _io.BytesIO()
        fr.save(buf, format="PNG")
        frames.append(base64.b64encode(buf.getvalue()).decode("ascii"))
        d = img.info.get("duration", 80)
        delays.append(max(40, min(500, int(d) * step if d else 80)))
    return frames, delays


def _payload_animated(raw, is_gif, target_h=None):
    """Оригинал анимации -> payload {'master': b64, 'anim': ...} для отрисовки."""
    b64 = base64.b64encode(raw).decode("ascii")
    if HAS_PIL:
        try:
            frames, delays = _decode_frames(raw, target_h)
            if len(frames) > 1:
                return {"master": frames[0], "anim": ("frames", frames, delays)}
            if frames:
                return {"master": frames[0], "anim": None}
        except Exception as e:
            dbg("! anim decode:", e)
    if is_gif:
        # tkinter умеет GIF сам: мастер — кадр 0, кадры достанем по -index
        return {"master": b64, "anim": ("gif", b64)}
    return None


def fetch_emote(eid):
    """Смайл Twitch (28px, анимированные — с кадрами). Кэш на диске. payload или None."""
    eid = re.sub(r"[^A-Za-z0-9_-]", "", eid)
    if not eid:
        return None
    cached = EMOTE_CACHE.get(eid, _MISS)
    if cached is not _MISS:
        return cached
    data = None
    png_path = os.path.join(CACHE_DIR, eid + ".png")
    gif_path = os.path.join(CACHE_DIR, eid + ".gif")
    try:
        if os.path.isfile(gif_path):
            with open(gif_path, "rb") as f:
                data = _payload_animated(f.read(), True, 28)
        elif os.path.isfile(png_path):
            with open(png_path, "rb") as f:
                data = {"master": base64.b64encode(f.read()).decode("ascii"), "anim": None}
        else:
            # format=default: статичные приходят PNG, анимированные — GIF
            url = "https://static-cdn.jtvnw.net/emoticons/v2/%s/default/dark/1.0" % eid
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6, context=SSL_CTX) as r:
                raw = r.read()
            os.makedirs(CACHE_DIR, exist_ok=True)
            if raw[:6] in (b"GIF87a", b"GIF89a"):
                write_cache_file(gif_path, raw)
                data = _payload_animated(raw, True, 28)
            else:
                write_cache_file(png_path, raw)
                data = {"master": base64.b64encode(raw).decode("ascii"), "anim": None}
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
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
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


# Липкие индексы рабочих маршрутов 7TV: если прямой домен заблокирован
# провайдером (актуально для РФ), запоминаем зеркало и ходим через него
SEVENTV_ROUTE = {"api": 0, "cdn": 0}
SEVENTV_MAPS_CACHE = os.path.join(SEVENTV_CACHE_DIR, "_maps.json")
SEVENTV_ANIMATED = set()  # id анимированных смайлов (из API, живёт и в кэше карт)


def _rotated(n, start):
    return list(range(start, n)) + list(range(0, start))


def fetch_7tv_json(path):
    """JSON из API 7TV: прямой домен → альтернативный → публичные прокси."""
    direct = "https://7tv.io/v3/" + path
    urls = [
        direct,
        "https://api.7tv.app/v3/" + path,
        "https://api.allorigins.win/raw?url=" + urllib.parse.quote(direct, safe=""),
        "https://api.codetabs.com/v1/proxy?quest=" + urllib.parse.quote(direct, safe=""),
    ]
    last = None
    for i in _rotated(len(urls), SEVENTV_ROUTE["api"]):
        try:
            req = urllib.request.Request(urls[i], headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6 if i < 2 else 9, context=SSL_CTX) as r:
                data = json.loads(r.read().decode("utf-8"))
            SEVENTV_ROUTE["api"] = i
            return data
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None  # канала нет в 7TV — это не сетевая проблема
            last = e
        except Exception as e:
            last = e
    dbg("! 7tv api, все маршруты:", last)
    return "unreachable"


def _load_7tv_maps_cache():
    try:
        with open(SEVENTV_MAPS_CACHE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_7tv_maps_cache(maps):
    try:
        os.makedirs(SEVENTV_CACHE_DIR, exist_ok=True)
        with open(SEVENTV_MAPS_CACHE, "w", encoding="utf-8") as f:
            json.dump(maps, f, ensure_ascii=False)
    except Exception:
        pass


def fetch_7tv_maps(channels, twitch_ids):
    """Карты смайлов 7TV: {'global': {имя: id}, канал: {имя: id}}.

    Если API недоступен даже через зеркала, берём последние удачные карты
    с диска — смайлы продолжают работать при блокировке 7TV у провайдера.
    """
    cached = _load_7tv_maps_cache()

    def mark_animated(sid):
        # смайл мог уже закэшироваться статичным до загрузки карт —
        # выбрасываем из памяти, чтобы перекачался с кадрами
        if sid not in SEVENTV_ANIMATED:
            SEVENTV_ANIMATED.add(sid)
            SEVENTV_IMG_CACHE.pop(sid)

    for sid in cached.get("_animated") or []:
        mark_animated(sid)

    def collect(emotes):
        out = {}
        for e in emotes or []:
            if not (e.get("name") and e.get("id")):
                continue
            out[e["name"]] = e["id"]
            if (e.get("data") or {}).get("animated"):
                mark_animated(e["id"])
        return out

    maps = {"global": {}}
    g = fetch_7tv_json("emote-sets/global")
    if isinstance(g, dict):
        maps["global"] = collect(g.get("emotes"))
    elif g == "unreachable" and isinstance(cached.get("global"), dict):
        maps["global"] = cached["global"]
        dbg("7tv global: из дискового кэша")
    for ch in channels:
        tid = twitch_ids.get(ch)
        if not tid:
            continue
        u = fetch_7tv_json("users/twitch/%s" % tid)
        if isinstance(u, dict):
            maps[ch] = collect((u.get("emote_set") or {}).get("emotes"))
            dbg("7tv %s: %d emotes" % (ch, len(maps[ch])))
        elif u == "unreachable" and isinstance(cached.get(ch), dict):
            maps[ch] = cached[ch]
            dbg("7tv %s: из дискового кэша" % ch)
    merged = dict(cached)
    merged.update({k: v for k, v in maps.items() if v})
    merged["_animated"] = sorted(SEVENTV_ANIMATED)
    _save_7tv_maps_cache(merged)
    return maps


def fetch_7tv_image(eid):
    """Смайл 7TV (28px, анимированные — с кадрами): прямой CDN → зеркала.

    Зеркала wsrv.nl / images.weserv.nl сами конвертируют webp (в PNG или
    анимированный GIF), поэтому работают даже без Pillow и при блокировке
    cdn.7tv.app у провайдера. Возвращает payload или None.
    """
    eid = re.sub(r"[^A-Za-z0-9]", "", eid)
    if not eid:
        return None
    cached = SEVENTV_IMG_CACHE.get(eid, _MISS)
    if cached is not _MISS:
        return cached
    animated = eid in SEVENTV_ANIMATED
    data = None
    png_path = os.path.join(SEVENTV_CACHE_DIR, eid + ".png")
    webp_path = os.path.join(SEVENTV_CACHE_DIR, eid + ".webp")
    gif_path = os.path.join(SEVENTV_CACHE_DIR, eid + ".gif")
    try:
        if HAS_PIL and os.path.isfile(webp_path):
            with open(webp_path, "rb") as f:
                data = _payload_animated(f.read(), False, 28)
        elif os.path.isfile(gif_path):
            with open(gif_path, "rb") as f:
                data = _payload_animated(f.read(), True, 28)
        elif os.path.isfile(png_path) and not animated:
            with open(png_path, "rb") as f:
                data = {"master": base64.b64encode(f.read()).decode("ascii"), "anim": None}
    except OSError:
        data = None
    if data is not None:
        SEVENTV_IMG_CACHE.put(eid, data)
        return data

    cdn = "cdn.7tv.app/emote/%s/1x.webp" % eid
    q = urllib.parse.quote(cdn, safe="")
    mirror_fmt = "gif&n=-1" if animated else "png"
    routes = [
        ("https://" + cdn, "webp"),  # оригинал (нужен Pillow)
        ("https://wsrv.nl/?url=%s&output=%s&h=28" % (q, mirror_fmt), "ready"),
        ("https://images.weserv.nl/?url=%s&output=%s&h=28" % (q, mirror_fmt), "ready"),
        ("https://api.allorigins.win/raw?url=" +
         urllib.parse.quote("https://" + cdn, safe=""), "webp"),
    ]
    for i in _rotated(len(routes), SEVENTV_ROUTE["cdn"]):
        url, kind = routes[i]
        if kind == "webp" and not HAS_PIL:
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6 if i == 0 else 9, context=SSL_CTX) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if i == 0 and e.code == 404:
                break  # такого смайла нет — зеркала не помогут
            continue
        except Exception:
            continue
        payload, save_path = None, None
        if raw.startswith(b"\x89PNG"):
            payload = {"master": base64.b64encode(raw).decode("ascii"), "anim": None}
            save_path = png_path
        elif raw[:6] in (b"GIF87a", b"GIF89a"):
            payload = _payload_animated(raw, True, 28)
            save_path = gif_path
        elif raw[:4] == b"RIFF":  # webp
            payload = _payload_animated(raw, False, 28)
            save_path = webp_path
        if payload:
            data = payload
            SEVENTV_ROUTE["cdn"] = i
            try:
                os.makedirs(SEVENTV_CACHE_DIR, exist_ok=True)
                write_cache_file(save_path, raw)
            except OSError:
                pass
            break
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
            with urllib.request.urlopen(req, timeout=6, context=SSL_CTX) as r:
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
        url = "https://id.twitch.tv/oauth2/validate"
        if not url.startswith("https://"):  # токен уходит только по TLS
            return None
        req = urllib.request.Request(url, headers={"Authorization": "OAuth " + token})
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
            data = json.loads(r.read().decode("utf-8"))
        login = (data.get("login") or "").lower()
        if login:
            return {"token": token, "login": login, "scopes": data.get("scopes") or [],
                    "client_id": data.get("client_id") or "",
                    "user_id": str(data.get("user_id") or "")}
    except Exception as e:
        dbg("! validate:", e)
    return None


MOD_SCOPES = ("moderator:manage:banned_users", "moderator:manage:chat_messages")


def helix(method, path, token, client_id, params=None, body=None):
    """Запрос к Helix API. Возвращает (код, dict|None); (0, None) — сеть недоступна."""
    url = "https://api.twitch.tv/helix/" + path
    if not url.startswith("https://"):  # токен уходит только по TLS
        return 0, None
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Client-Id": client_id,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
            raw = r.read()
            return r.status, (json.loads(raw.decode("utf-8")) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        dbg("! helix:", e)
        return 0, None


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
        self.userstate = {}    # канал -> (имя, цвет, значки, есть_модерка)
        self.channel_ids = {}  # канал -> twitch id (заполняет загрузчик карт)
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
                      login.lower(), tags.get("user-id", ""), tags.get("id", ""),
                      tags.get("reply-parent-user-login", "").lower()))
        elif cmd == "USERSTATE":
            badges = tags.get("badges", "")
            is_mod = tags.get("mod") == "1" or "broadcaster/" in badges
            self.userstate[channel] = (tags.get("display-name") or self.nick,
                                       tags.get("color", ""), badges, is_mod)
        elif cmd == "GLOBALUSERSTATE":
            self.userstate["*"] = (tags.get("display-name") or self.nick,
                                   tags.get("color", ""), tags.get("badges", ""), False)
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
                          (tags.get("login") or "").lower(),
                          tags.get("user-id", ""), tags.get("id", ""),
                          tags.get("reply-parent-user-login", "").lower()))
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
        self.images = LruDict(900)  # ключ -> PhotoImage; LRU, чтобы память не росла
        self._anim = {}             # ключ -> состояние анимации смайла
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
        self.ghost_input = tk.BooleanVar(value=bool(cfg.get("ghost_input")))
        self.topmost = tk.BooleanVar(value=True)
        self.theme_var = tk.StringVar(value=cfg.get("theme", "twitch"))
        self.lang_var = tk.StringVar(value=cfg.get("lang", "ru"))
        self.layout_var = tk.StringVar(value=cfg.get("layout", "tabs"))
        self.anim_enabled = tk.BooleanVar(value=bool(cfg.get("animations", True)))
        self.obs_chroma = tk.BooleanVar(value=bool(cfg.get("obs_chroma")))
        self.expanded = False
        self.expand_var = tk.BooleanVar(value=False)
        self.fullscreen = False
        self.fs_var = tk.BooleanVar(value=False)
        self._minimized = False
        self._tw_emotes = None
        self.emote_win = None
        self.settings_win = None
        self._msg_meta = {}   # тег -> (канал, логин, имя, user_id, message_id)
        self._msg_seq = 0
        self._action_win = None
        self._armed_bans = set()  # «первый клик по ⊘ сделан» (канал/логин)
        self.mod_icons = tk.BooleanVar(value=bool(cfg.get("mod_icons", True)))

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
        self.min_btn = tk.Label(self.bar, text=" — ", bg=BAR_BG, fg=BTN_FG,
                                font=("Segoe UI", 10, "bold"), cursor="hand2")
        self.min_btn.pack(side="right")
        self.min_btn.bind("<Button-1>", lambda e: self.minimize_window())
        self.close_btn.bind("<Button-1>", lambda e: self.quit())
        self.gear_btn.bind("<Button-1>", self.open_settings)
        self.heart_btn.bind("<Button-1>", lambda e: webbrowser.open(DONATE_URL))
        self.heart_btn.bind("<Enter>", lambda e: self.heart_btn.configure(fg=ACCENT_HOVER))
        self.heart_btn.bind("<Leave>", lambda e: self.heart_btn.configure(fg=ACCENT))
        for b in (self.close_btn, self.gear_btn, self.min_btn):
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
        # кнопка-мегафон: отправить текст поля цветным анонсом (для модеров)
        self.announce_btn = tk.Label(self.input_bar, bg=BAR_BG, cursor="hand2")
        self.announce_btn.bind("<Button-1>", lambda e: self.send_announce())
        self._announce_shown = False
        # кнопка-смайлик: открывает пикер смайлов Twitch + 7TV
        self.emote_btn = tk.Label(self.input_bar, bg=BAR_BG, cursor="hand2")
        self.emote_btn.bind("<Button-1>", lambda e: self.open_emote_picker())
        self.emote_btn.bind("<Enter>", lambda e: self._btn_hot(self.emote_btn, "emote", True))
        self.emote_btn.bind("<Leave>", lambda e: self._btn_hot(self.emote_btn, "emote", False))
        self.chan_btn.pack(side="left", padx=(6, 0), pady=5)
        self.entry_pill.pack(side="left", fill="x", expand=True, padx=(6, 4), pady=5)
        self.emote_btn.pack(side="left", padx=(0, 14), pady=5)
        # плейсхолдер: подсказывает, что тут пишут в чат
        self._ph_on = False
        self.entry.bind("<FocusIn>", self._ph_clear)
        self.entry.bind("<FocusOut>", lambda e: self._ph_set())
        self._ph_set()

        # --- ленты чата: "*" — общий поток, плюс по одной на канал ---
        self.tab_bar = tk.Frame(frame, bg=BAR_BG)
        self._tab_btns = {}
        self.active_tab = "*"
        self.unread = {}  # канал -> {"n": непрочитанные, "hl": упоминания/ответы}
        self.layout = self.cfg.get("layout", "tabs")  # tabs | unified | columns
        # feed_area — контейнер лент; в нём grid: строка 0 — заголовки колонок,
        # строка 1 — сами ленты (для режима «колонки»); в остальных режимах
        # одна лента растягивается на всю ширину
        self.feed_area = tk.Frame(frame, bg=BG)
        self._col_headers = {}  # канал -> Label заголовка колонки
        self.texts = {"*": self._make_text()}
        self.text = self.texts["*"]
        self.feed_area.pack(fill="both", expand=True)

        self.grip = tk.Label(frame, text="◢", bg=BG, fg=GRIP_FG,
                             cursor="size_nw_se", font=("Segoe UI", 9))
        self.grip.place(relx=1.0, rely=1.0, anchor="se")

        # кнопка «вниз к новым сообщениям», появляется при прокрутке вверх
        self.newmsg_count = 0
        self.newmsg_btn = RoundButton(frame, "↓", command=self.jump_to_bottom,
                                      fill=CHIPBTN_BG, fg=FG, parent_bg=BG,
                                      font=("Segoe UI", 9), padx=12)

        for w, key in ((self.heart_btn, "tt_donate"), (self.gear_btn, "tt_menu"),
                       (self.close_btn, "tt_close"), (self.chan_btn, "tt_chan"),
                       (self.announce_btn, "tt_announce"), (self.min_btn, "tt_min"),
                       (self.emote_btn, "tt_emotes")):
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

        self._build_icon_photos()
        self.place_window()
        root.deiconify()
        root.update_idletasks()
        dwm_round(root)  # скруглённые углы окна (Windows 11)
        self._make_taskbar_proxy()
        self.apply_look()
        self.update_input_bar()
        self.update_mention_re()
        if self.frameless.get():
            self.apply_frameless(startup=True)
        elif self.ghost_input.get():
            self.apply_ghost_input(startup=True)

        self.connect(cfg["channels"])
        self.sys_message(T("hint_start", cfg["key_clickthrough"]["name"],
                           cfg["key_frameless"]["name"], cfg["key_expand"]["name"]))
        self.poll_queue()
        self.poll_keys()
        self.keep_topmost()
        self._animate()

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

        # --- избранные каналы ---
        r = row()
        label(r, "s_fav")
        favwrap = tk.Frame(r, bg=BG)
        favwrap.pack(side="left", fill="x", expand=True)
        favs = self.cfg.get("favorites") or []
        if favs:
            for ch in favs:
                self._fav_chip(favwrap, ch)
        else:
            tk.Label(favwrap, text=T("fav_empty"), bg=BG, fg=SYS_FG,
                     font=("Segoe UI", 8)).pack(side="left")
        chip_btn(r, T("s_addfav"), self._add_favorite).pack(side="left", padx=(6, 0))

        # --- сохранённые наборы ---
        sets = self.cfg.get("sets") or {}
        r = row()
        label(r, "s_sets")
        setwrap = tk.Frame(r, bg=BG)
        setwrap.pack(side="left", fill="x", expand=True)
        for nm in sorted(sets):
            self._set_chip(setwrap, nm)
        chip_btn(r, T("s_saveset"), self._save_set).pack(side="left", padx=(6, 0))

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
        tk.Checkbutton(r, text=T("s_ghostinput"), variable=self.ghost_input,
                       command=self.apply_ghost_input, **chk).pack(side="left")
        chip_btn(r, self.cfg["key_ghostinput"]["name"],
                 lambda: self.rebind_key("key_ghostinput", T("d_key_ghostinput"),
                                         self.refresh_settings)).pack(side="right")
        r = row()
        tk.Checkbutton(r, text=T("s_click"), variable=self.clickthrough,
                       command=self.apply_clickthrough, **chk).pack(side="left")
        chip_btn(r, self.cfg["key_clickthrough"]["name"],
                 lambda: self.rebind_key("key_clickthrough", T("d_key_click"),
                                         self.refresh_settings)).pack(side="right")
        r = row()
        tk.Checkbutton(r, text=T("s_expand"), variable=self.expand_var,
                       command=lambda: self.set_expanded(self.expand_var.get()),
                       **chk).pack(side="left")
        chip_btn(r, self.cfg["key_expand"]["name"],
                 lambda: self.rebind_key("key_expand", T("d_key_expand"),
                                         self.refresh_settings)).pack(side="right")
        r = row()
        tk.Checkbutton(r, text=T("s_fullscreen"), variable=self.fs_var,
                       command=lambda: self.set_fullscreen(self.fs_var.get()),
                       **chk).pack(side="left")
        chip_btn(r, self.cfg["key_fullscreen"]["name"],
                 lambda: self.rebind_key("key_fullscreen", T("d_key_fullscreen"),
                                         self.refresh_settings)).pack(side="right")
        tk.Checkbutton(row(), text=T("m_topmost"), variable=self.topmost,
                       command=self.apply_topmost, **chk).pack(side="left")
        tk.Checkbutton(row(), text=T("m_ghost"), variable=self.ghost,
                       command=self.apply_look, **chk).pack(side="left")
        tk.Checkbutton(row(), text=T("s_chroma"), variable=self.obs_chroma,
                       command=self.toggle_chroma, **chk).pack(side="left")
        tk.Checkbutton(row(), text=T("s_modicons"), variable=self.mod_icons,
                       command=self._save_mod_icons, **chk).pack(side="left")

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

        tk.Checkbutton(row(), text=T("s_anim"), variable=self.anim_enabled,
                       command=self.toggle_animations, **chk).pack(side="left")

        rb = dict(bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                  selectcolor=ENTRY_BG, font=lbl_font, highlightthickness=0,
                  bd=0, cursor="hand2")
        r = row()
        label(r, "s_layout")
        for val, key in (("tabs", "lay_tabs"), ("unified", "lay_unified"),
                         ("columns", "lay_columns")):
            tk.Radiobutton(r, text=T(key), variable=self.layout_var, value=val,
                           command=lambda: self.set_layout(self.layout_var.get()),
                           **rb).pack(side="left", padx=(0, 8))
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

    def _fav_chip(self, parent, ch):
        f = tk.Frame(parent, bg=CHIPBTN_BG)
        f.pack(side="left", padx=(0, 4), pady=1)
        lbl = tk.Label(f, text="#" + ch, bg=CHIPBTN_BG, fg=FG,
                       font=("Segoe UI", 9), padx=6, pady=2, cursor="hand2")
        lbl.pack(side="left")
        Tooltip(lbl, "fav_hint")
        lbl.bind("<Button-1>", lambda e, c=ch: self._fav_click(c))
        x = tk.Label(f, text="✕", bg=CHIPBTN_BG, fg=SYS_FG,
                     font=("Segoe UI", 8), padx=4, pady=2, cursor="hand2")
        x.pack(side="left")
        x.bind("<Button-1>", lambda e, c=ch: self._unfavorite(c))

    def _set_chip(self, parent, name):
        f = tk.Frame(parent, bg=CHIPBTN_BG)
        f.pack(side="left", padx=(0, 4), pady=1)
        lbl = tk.Label(f, text=name, bg=CHIPBTN_BG, fg=ACCENT,
                       font=("Segoe UI", 9, "bold"), padx=7, pady=2, cursor="hand2")
        lbl.pack(side="left")
        Tooltip(lbl, "set_hint")
        lbl.bind("<Button-1>", lambda e, n=name: self._load_set(n))
        x = tk.Label(f, text="✕", bg=CHIPBTN_BG, fg=SYS_FG,
                     font=("Segoe UI", 8), padx=4, pady=2, cursor="hand2")
        x.pack(side="left")
        x.bind("<Button-1>", lambda e, n=name: self._delete_set(n))

    def _fav_click(self, ch):
        # добавляем избранный канал в поле «Каналы», не трогая уже набранное
        cur = parse_channels(self.set_chan_entry.get())
        if ch not in cur:
            cur.append(ch)
        self.set_chan_entry.delete(0, "end")
        self.set_chan_entry.insert(0, ", ".join(cur))

    def _add_favorite(self):
        favs = list(self.cfg.get("favorites") or [])
        for ch in parse_channels(self.set_chan_entry.get()):
            if ch not in favs:
                favs.append(ch)
        self.cfg["favorites"] = favs
        save_config(self.cfg)
        self.refresh_settings()

    def _unfavorite(self, ch):
        self.cfg["favorites"] = [c for c in (self.cfg.get("favorites") or []) if c != ch]
        save_config(self.cfg)
        self.refresh_settings()

    def _save_set(self):
        chans = parse_channels(self.set_chan_entry.get())
        if not chans:
            return
        name = ask_text(self.root, T("set_name_title"), T("set_name_label"), "")
        if not name or not name.strip():
            return
        sets = dict(self.cfg.get("sets") or {})
        sets[name.strip()[:24]] = chans
        self.cfg["sets"] = sets
        save_config(self.cfg)
        self.refresh_settings()

    def _load_set(self, name):
        chans = (self.cfg.get("sets") or {}).get(name)
        if chans:
            self.send_index = 0
            self.connect(list(chans))
        self.refresh_settings()

    def _delete_set(self, name):
        sets = dict(self.cfg.get("sets") or {})
        sets.pop(name, None)
        self.cfg["sets"] = sets
        save_config(self.cfg)
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
        self._build_icon_photos()  # иконки перерисовываем под новый кегль
        self._sync_announce_icon()
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
        self.min_btn.configure(bg=BAR_BG, fg=BTN_FG)
        self.heart_btn.configure(bg=BAR_BG, fg=ACCENT)
        self.emote_btn.configure(bg=BAR_BG)
        self.input_bar.configure(bg=BAR_BG)
        self.chan_btn.restyle(fill=CHIPBTN_BG, fg=CHIP_FG, parent_bg=BAR_BG)
        self.entry_pill.restyle(fill=ENTRY_BG, fg=FG, parent_bg=BAR_BG)
        self.entry.configure(selectbackground=SELECT_BG,
                             fg=SYS_FG if getattr(self, "_ph_on", False) else FG)
        self.newmsg_btn.restyle(fill=CHIPBTN_BG, fg=FG, parent_bg=BG)
        self.announce_btn.configure(bg=BAR_BG)
        self._build_icon_photos()  # иконки согласуем с новым акцентом темы
        self._announce_shown = None
        self._sync_announce_icon()
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
        for h in self._col_headers.values():
            h.configure(bg=BAR_BG, fg=SYS_FG)
        self._style_col_headers()
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
            self.cfg["client_id"] = info.get("client_id", "")
            self.cfg["user_id"] = info.get("user_id", "")
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
            self.input_bar.pack(side="bottom", fill="x", before=self.feed_area)
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
        # пикер смайлов показывает набор выбранного канала — обновляем
        if getattr(self, "emote_win", None) is not None and self.emote_win.winfo_exists():
            self._ep_render()

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
                         (self.cfg.get("login") or "").lower(),
                         self.cfg.get("user_id", ""), ""))

            DOWNLOAD_POOL.submit(build_echo)
        else:
            self.sys_message(T("not_sent"))

    def _send_channel(self):
        chans = self.cfg.get("channels") or []
        if not chans:
            return ""
        return chans[min(self.send_index, len(chans) - 1)]

    def _sync_announce_icon(self):
        """Показывает мегафон у поля ввода, если ты модер/стример на канале отправки."""
        show = (bool(self.cfg.get("login")) and not getattr(self, "_ph_off_hidden", False)
                and self._is_mod(self._send_channel())
                and getattr(self, "_icon_norm", {}).get("announce") is not None)
        if show == self._announce_shown:
            return
        self._announce_shown = show
        if show:
            self.announce_btn.configure(image=self._icon_norm["announce"])
            self.announce_btn.pack(side="left", padx=(0, 6), pady=5,
                                   before=self.entry_pill)
        else:
            self.announce_btn.pack_forget()

    def send_announce(self):
        if getattr(self, "_ph_on", False):
            return
        text = self.entry.get().strip()[:500]
        channel = self._send_channel()
        if not text or not channel:
            return
        token = self.cfg.get("token", "")
        cid = self.cfg.get("client_id", "")
        my_id = self.cfg.get("user_id", "")
        irc = self.irc

        def run():
            bid = irc.channel_ids.get(channel) if irc else None
            if not (token and cid and my_id and bid):
                irc.put(("sys", T("mod_err", "нет данных токена/канала")))
                return
            code, resp = helix("POST", "chat/announcements", token, cid,
                               {"broadcaster_id": bid, "moderator_id": my_id},
                               {"message": text, "color": "primary"})
            if code in (200, 204):
                irc.put(("sys", T("ann_sent")))
            elif code == 401:
                irc.put(("sys", T("mod_err_scope")))
            elif code == 403:
                irc.put(("sys", T("mod_err_notmod", channel)))
            else:
                irc.put(("sys", T("mod_err", (resp or {}).get("message") or code)))

        DOWNLOAD_POOL.submit(run)
        self.entry.delete(0, "end")

    def _btn_hot(self, btn, kind, hot):
        img = (self._icon_hot if hot else self._icon_norm).get(kind)
        if img is not None:
            try:
                btn.configure(image=img)
            except tk.TclError:
                pass

    # ---------- свернуть / полный экран ----------

    def _make_taskbar_proxy(self):
        """Невидимое окно-«прокси» с кнопкой в панели задач: у окна без рамки
        своей кнопки нет, а прокси даёт нативное сворачивание/восстановление
        без манипуляций с рамкой и миганий."""
        p = tk.Toplevel(self.root)
        p.title("Twitch Chat Overlay")
        try:
            p.iconbitmap(os.path.join(APP_DIR, "overlay.ico"))
        except Exception:
            pass
        p.geometry("1x1+-32000+-32000")
        try:
            p.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        p.protocol("WM_DELETE_WINDOW", self.quit)
        p.bind("<Map>", self._proxy_mapped)
        p.bind("<Unmap>", self._proxy_unmapped)
        self._proxy = p

    def minimize_window(self):
        self._minimized = True
        try:
            self.root.withdraw()
            self._proxy.iconify()
        except tk.TclError:
            pass

    def _proxy_mapped(self, event=None):
        # клик по кнопке в таскбаре: прокси развернулся — показываем оверлей
        if not getattr(self, "_minimized", False):
            return
        self._minimized = False
        try:
            self.root.deiconify()
            self.root.update_idletasks()
            self._force_topmost(True)
        except tk.TclError:
            pass

    def _proxy_unmapped(self, event=None):
        # клик по кнопке в таскбаре при открытом окне — сворачиваемся
        try:
            if not getattr(self, "_minimized", False) and self._proxy.state() == "iconic":
                self.minimize_window()
        except tk.TclError:
            pass

    def _monitor_rect(self):
        """Границы монитора, на котором сейчас окно (для F11)."""
        try:
            hwnd = self.user32.GetAncestor(self.root.winfo_id(), 2)
            hmon = self.user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST

            class RECT(ctypes.Structure):
                _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                            ("r", ctypes.c_long), ("b", ctypes.c_long)]

            class MI(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("mon", RECT),
                            ("work", RECT), ("flags", ctypes.c_ulong)]

            mi = MI()
            mi.cb = ctypes.sizeof(MI)
            if self.user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                m = mi.mon
                return m.l, m.t, m.r, m.b
        except Exception:
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def set_fullscreen(self, on):
        on = bool(on)
        if on == self.fullscreen:
            return
        self.fullscreen = on
        self.fs_var.set(on)
        if on:
            self._pre_fs = (self.root.winfo_x(), self.root.winfo_y(),
                            self.root.winfo_width(), self.root.winfo_height())
            l, t, r, b = self._monitor_rect()
            self.root.geometry("%dx%d+%d+%d" % (r - l, b - t, l, t))
        else:
            g = getattr(self, "_pre_fs", None)
            if g:
                self.root.geometry("%dx%d+%d+%d" % (g[2], g[3], g[0], g[1]))
        self.grip.lift()

    # ---------- F10: компактный оверлей <-> развёрнутый мультичат ----------

    def set_expanded(self, on):
        on = bool(on)
        if on == self.expanded:
            return
        self.expanded = on
        self.expand_var.set(on)
        self.newmsg_count = 0
        self.newmsg_btn.place_forget()
        if on:
            self._pre_expand = {
                "geo": (self.root.winfo_x(), self.root.winfo_y(),
                        self.root.winfo_width(), self.root.winfo_height()),
                "frameless": bool(self.frameless.get()),
                "ghost_input": bool(self.ghost_input.get()),
            }
            if self.frameless.get():
                self.frameless.set(False)
                self.apply_frameless()
            if self.ghost_input.get():
                self.ghost_input.set(False)
                self.apply_ghost_input()
            g = self.cfg.get("exp_geometry")
            if not (isinstance(g, list) and len(g) == 4):
                x, y, w, h = self._pre_expand["geo"]
                sw = self.root.winfo_screenwidth()
                sh = self.root.winfo_screenheight()
                w2 = min(int(sw * 0.62), max(760, w * 2))
                h2 = min(int(sh * 0.75), max(520, h * 2))
                g = [max(0, min(x, sw - w2 - 20)), max(0, min(y, sh - h2 - 60)), w2, h2]
            self.root.geometry("%dx%d+%d+%d" % (g[2], g[3], g[0], g[1]))
            if len(self.cfg.get("channels") or []) > 1:
                self.layout = "columns"
            self._apply_layout()
        else:
            self.cfg["exp_geometry"] = [self.root.winfo_x(), self.root.winfo_y(),
                                        self.root.winfo_width(), self.root.winfo_height()]
            save_config(self.cfg)
            pe = getattr(self, "_pre_expand", None) or {}
            geo = pe.get("geo")
            if geo:
                self.root.geometry("%dx%d+%d+%d" % (geo[2], geo[3], geo[0], geo[1]))
            self.layout = self.cfg.get("layout", "tabs")
            self._apply_layout()
            if pe.get("frameless"):
                self.frameless.set(True)
                self.apply_frameless()
            elif pe.get("ghost_input"):
                self.ghost_input.set(True)
                self.apply_ghost_input()

    # ---------- пикер смайлов (Twitch + 7TV) ----------

    def open_emote_picker(self):
        if getattr(self, "emote_win", None) is not None and self.emote_win.winfo_exists():
            self.emote_win.destroy()
            self.emote_win = None
            return
        win = tk.Toplevel(self.root)
        self.emote_win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BORDER)
        box = tk.Frame(win, bg=BAR_BG, padx=8, pady=8)
        box.pack(padx=1, pady=1)
        sp = RoundEntry(box, font=("Segoe UI", 10), fill=ENTRY_BG, fg=FG,
                        parent_bg=BAR_BG, height=28)
        self._ep_search = sp.entry
        sp.pack(fill="x")
        self._ep_search.insert(0, "")
        self._ep_search.bind("<KeyRelease>", lambda e: self._ep_render())
        cv = tk.Canvas(box, width=338, height=290, bg=BAR_BG,
                       highlightthickness=0, bd=0)
        cv.pack(pady=(8, 0))
        self._ep_canvas = cv
        self._ep_frame = tk.Frame(cv, bg=BAR_BG)
        cv.create_window(0, 0, window=self._ep_frame, anchor="nw")

        def wheel(e):
            try:
                cv.yview_scroll(-1 * (e.delta // 120), "units")
            except tk.TclError:
                pass
        self._ep_wheel = wheel
        cv.bind("<MouseWheel>", wheel)
        self._ep_frame.bind("<MouseWheel>", wheel)
        win.bind("<Escape>", lambda e: win.destroy())
        # твичевские смайлы пользователя тянем один раз за сессию
        if self.cfg.get("login") and self._tw_emotes is None:
            self._tw_emotes = []
            DOWNLOAD_POOL.submit(self._load_tw_emotes)
        self._ep_render()
        win.update_idletasks()
        bx, by = self.emote_btn.winfo_rootx(), self.emote_btn.winfo_rooty()
        w = win.winfo_width() or 356
        h = win.winfo_height() or 350
        x = max(0, min(bx - w + 34, self.root.winfo_screenwidth() - w))
        y = by - h - 8
        if y < 0:
            y = by + 26
        win.geometry("+%d+%d" % (x, y))
        dwm_round(win, small=True)
        self._ep_search.focus_set()
        self._ep_poll()

    def _ep_items(self):
        q = ""
        try:
            q = (self._ep_search.get() or "").strip().lower()
        except tk.TclError:
            pass
        maps = self.irc.seventv_maps if self.irc else {}
        ch = self._send_channel()
        seen = set()
        items = []

        def add(name, kind, eid):
            k = name.lower()
            if k in seen or (q and q not in k):
                return
            seen.add(k)
            items.append((name, kind, eid))

        # только смайлы, активные на выбранном канале: его 7TV-набор, глобальные
        # 7TV и твичевские пользователя (те работают в любом чате)
        cm = maps.get(ch) or {}
        gm = maps.get("global") or {}
        valid7 = set(cm.values()) | set(gm.values())
        for t in self.cfg.get("recent_emotes") or []:
            if t[2] == "7tv" and t[1] not in valid7:
                continue  # смайл другого канала — тут не отрисуется
            add(t[0], t[2], t[1])
        for name, eid in sorted(cm.items()):
            add(name, "7tv", eid)
        for e in (self._tw_emotes or []):
            add(e["name"], "tw", e["id"])
        for name, eid in sorted(gm.items()):
            add(name, "7tv", eid)
        return items[:128]

    def _ep_payload(self, kind, eid):
        cache = SEVENTV_IMG_CACHE if kind == "7tv" else EMOTE_CACHE
        return cache.get(eid, _MISS)

    def _ep_render(self):
        f = self._ep_frame
        for c in f.winfo_children():
            c.destroy()
        self._ep_cells = []
        for i, (name, kind, eid) in enumerate(self._ep_items()):
            key = ("e:7tv" + eid) if kind == "7tv" else ("e:" + eid)
            lbl = tk.Label(f, bg=BAR_BG, cursor="hand2", text=name[:6],
                           fg=SYS_FG, font=("Segoe UI", 7), width=6, height=2)
            payload = self._ep_payload(kind, eid)
            if payload is _MISS:
                fn = fetch_7tv_image if kind == "7tv" else fetch_emote
                DOWNLOAD_POOL.submit(fn, eid)
            elif payload:
                img = self.cached_image(key, payload)
                if img is not None:
                    lbl.configure(image=img, width=38, height=32, text="")
            lbl.grid(row=i // 8, column=i % 8, padx=1, pady=1)
            lbl.bind("<Button-1>", lambda e, n=name, k=kind, d=eid: self._ep_pick(n, k, d))
            lbl.bind("<MouseWheel>", self._ep_wheel)
            self._ep_cells.append((lbl, kind, eid, key))
        f.update_idletasks()
        self._ep_canvas.configure(scrollregion=self._ep_canvas.bbox("all") or (0, 0, 0, 0))
        self._ep_canvas.yview_moveto(0)

    def _ep_poll(self):
        win = getattr(self, "emote_win", None)
        if win is None or not win.winfo_exists():
            return
        for lbl, kind, eid, key in self._ep_cells:
            try:
                if lbl.cget("image"):
                    continue
                payload = self._ep_payload(kind, eid)
                if payload is not _MISS and payload:
                    img = self.cached_image(key, payload)
                    if img is not None:
                        lbl.configure(image=img, width=38, height=32, text="")
            except tk.TclError:
                return
        self.root.after(250, self._ep_poll)

    def _ep_pick(self, name, kind, eid):
        self._ph_clear()
        try:
            self.entry.insert("insert", name + " ")
            self.entry.focus_set()
        except tk.TclError:
            pass
        rec = [t for t in (self.cfg.get("recent_emotes") or []) if t[0] != name]
        rec.insert(0, [name, eid, kind])
        self.cfg["recent_emotes"] = rec[:24]
        save_config(self.cfg)

    def _load_tw_emotes(self):
        """Смайлы, доступные пользователю на Twitch (Helix, до 3 страниц)."""
        token = self.cfg.get("token", "")
        cid = self.cfg.get("client_id", "")
        uid = self.cfg.get("user_id", "")
        if not (token and cid and uid):
            return
        out, cursor = [], ""
        for _ in range(3):
            params = {"user_id": uid}
            if cursor:
                params["after"] = cursor
            code, resp = helix("GET", "chat/emotes/user", token, cid, params)
            if code != 200 or not isinstance(resp, dict):
                break
            for e in resp.get("data") or []:
                if e.get("name") and e.get("id"):
                    out.append({"name": e["name"], "id": e["id"]})
            cursor = ((resp.get("pagination") or {}).get("cursor")) or ""
            if not cursor:
                break
        self._tw_emotes = out
        dbg("tw emotes:", len(out))

    # ---------- внешний вид ----------

    def apply_look(self):
        self.cfg["ghost"] = bool(self.ghost.get())
        # Обычный ключ прозрачности — цвет фона. Для OBS-режима фон красится
        # чисто-пурпурным: на мониторе он так же вырезается, а «Захват окна»
        # в OBS видит пурпур, который убирается фильтром «Цветовой ключ».
        key = CHROMA_KEY if self.obs_chroma.get() else BG
        try:
            if self.ghost.get():
                # красим и подложку окна: иначе её 1px виден как боковые рамки
                self.root.configure(bg=key)
                self._paint_chat_bg(key)
                self.root.attributes("-alpha", 1.0)
                self.root.attributes("-transparentcolor", key)
            else:
                self.root.configure(bg=BORDER)
                self._paint_chat_bg(BG)
                self.root.attributes("-transparentcolor", "")
                self.root.attributes("-alpha", float(self.cfg.get("opacity", 0.88)))
        except tk.TclError:
            pass
        save_config(self.cfg)

    def _paint_chat_bg(self, color):
        self.frame.configure(bg=color)
        self.feed_area.configure(bg=color)
        for w in self.texts.values():
            w.configure(bg=color)
        self.grip.configure(bg=color)

    def _save_mod_icons(self):
        self.cfg["mod_icons"] = bool(self.mod_icons.get())
        save_config(self.cfg)

    def toggle_chroma(self):
        self.cfg["obs_chroma"] = bool(self.obs_chroma.get())
        save_config(self.cfg)
        self.apply_look()
        self.sys_message(T("chroma_on") if self.obs_chroma.get() else T("chroma_off"))

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

    def _is_bare(self):
        return bool(self.frameless.get() or self.ghost_input.get())

    def apply_frameless(self, startup=False):
        """F9 — только текст чата: без рамки, полосы, поля ввода и фона."""
        if self.frameless.get() and self.ghost_input.get():
            self.ghost_input.set(False)
        self._apply_bare(startup, key_name=self.cfg["key_frameless"]["name"],
                         msg_key="textonly_on")

    def apply_ghost_input(self, startup=False):
        """F7 — прозрачный чат, но поле ввода (и смайлы) остаются."""
        if self.ghost_input.get() and self.frameless.get():
            self.frameless.set(False)
        self._apply_bare(startup, key_name=self.cfg["key_ghostinput"]["name"],
                         msg_key="ghostinput_on")

    def _apply_bare(self, startup=False, key_name="", msg_key=""):
        bare = self._is_bare()
        was_bare = getattr(self, "_bare_now", False)
        self._bare_now = bare
        self.cfg["frameless"] = bool(self.frameless.get())
        self.cfg["ghost_input"] = bool(self.ghost_input.get())
        save_config(self.cfg)
        if bare:
            if not was_bare and not startup:
                self._ghost_before = bool(self.ghost.get())
            self.bar.pack_forget()
            self.tab_bar.pack_forget()
            self.grip.place_forget()
            self.jump_to_bottom()
            self.frame.pack_configure(padx=0, pady=0)
            if self.ghost_input.get():
                self.update_input_bar()   # поле ввода остаётся — можно писать
            else:
                self.input_bar.pack_forget()
            if not self.ghost.get():
                self.ghost.set(True)
            self.apply_look()
            if not startup and msg_key:
                self.sys_message(T(msg_key, key_name))
        else:
            self.frame.pack_configure(padx=1, pady=1)
            self.bar.pack(fill="x", before=self.feed_area)
            self.update_input_bar()
            self.grip.place(relx=1.0, rely=1.0, anchor="se")
            self.ghost.set(bool(getattr(self, "_ghost_before", False)))
            self.apply_look()
        self._apply_layout()  # заголовки колонок/вкладки прячутся в голом режиме

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
            if self._key_pressed(self.cfg["key_expand"]["vk"]):
                self.set_expanded(not self.expanded)
            if self._key_pressed(self.cfg["key_fullscreen"]["vk"]):
                self.set_fullscreen(not self.fullscreen)
            if self._key_pressed(self.cfg["key_ghostinput"]["vk"]):
                self.ghost_input.set(not self.ghost_input.get())
                self.apply_ghost_input()
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
            others = [k for k in ("key_frameless", "key_clickthrough", "key_expand",
                                  "key_fullscreen", "key_ghostinput") if k != which]
            if any(e.keycode == self.cfg[o]["vk"] for o in others):
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
        if self.fullscreen:
            return  # временная геометрия, не запоминаем
        if self.expanded:
            try:
                self.cfg["exp_geometry"] = [self.root.winfo_x(), self.root.winfo_y(),
                                            self.root.winfo_width(),
                                            self.root.winfo_height()]
                save_config(self.cfg)
            except tk.TclError:
                pass
            return
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
        self.unread = {}  # смена набора каналов — счётчики обнуляем
        for ch in list(self.texts.keys()):
            if ch != "*" and ch not in channels:
                self.texts[ch].destroy()
                del self.texts[ch]
        for ch in channels:
            if ch not in self.texts:
                self.texts[ch] = self._make_text()
        if self.cfg.get("active_tab") not in self.texts:
            self.cfg["active_tab"] = "*"
        self.active_tab = self.cfg["active_tab"]
        self._apply_layout()
        self.apply_look()  # новые ленты вкладок докрашиваются под текущий режим
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
        irc_ref = self.irc

        def load_assets():
            # для модерации нужны client_id/user_id токена — добираем на старте
            if self.cfg.get("token") and not (self.cfg.get("client_id")
                                              and self.cfg.get("user_id")):
                info = validate_token(self.cfg["token"])
                if info:
                    self.cfg["client_id"] = info["client_id"]
                    self.cfg["user_id"] = info["user_id"]
                    save_config(self.cfg)
            while True:
                try:
                    bm, ids = fetch_badge_maps(channels)
                    irc_ref.channel_ids.update(ids)
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
        self._sync_announce_icon()  # статус модерки приходит из USERSTATE асинхронно
        self.root.after(60, self.poll_queue)

    def color_tag(self, w, color, login, body=False):
        c = readable_color(color, login)
        tag = ("a" if body else "n") + c
        if tag not in w._ctags:
            w.tag_configure(tag, foreground=c,
                            font=self.font_msg if body else self.font_nick)
            w._ctags.add(tag)
        return tag

    def cached_image(self, key, payload):
        """PhotoImage по ключу; payload — b64-строка (значки) или dict (смайлы).

        Анимированные регистрируются в self._anim: один общий PhotoImage
        обновляется кадрами, и все его копии во всех лентах двигаются сами.
        """
        st = self._anim.get(key)
        if st is not None:
            return st["master"]
        img = self.images.get(key, _MISS)
        if img is not _MISS:
            return img
        b64, anim = None, None
        if isinstance(payload, str):
            b64 = payload
        elif isinstance(payload, dict):
            b64 = payload.get("master")
            anim = payload.get("anim")
        img = None
        if b64:
            try:
                img = tk.PhotoImage(data=b64)
            except tk.TclError:
                img = None
        if img is not None and anim and self.anim_enabled.get():
            frames, delays = self._build_frames(anim)
            if len(frames) > 1:
                if len(self._anim) >= 40:  # потолок одновременных анимаций
                    self._anim.pop(next(iter(self._anim)), None)
                self._anim[key] = {"master": img, "frames": frames,
                                   "delays": delays, "i": 0, "left": delays[0]}
                return img  # мастер держим вне LRU, чтобы не выселился
        self.images.put(key, img)
        return img

    def _build_frames(self, anim):
        frames, delays = [], []
        try:
            if anim[0] == "frames":
                frames = [tk.PhotoImage(data=f) for f in anim[1]]
                delays = list(anim[2])
            else:  # ("gif", b64): кадры достаёт сам tkinter
                for i in range(60):
                    try:
                        frames.append(tk.PhotoImage(data=anim[1],
                                                    format="gif -index %d" % i))
                    except tk.TclError:
                        break
        except Exception:
            pass
        if len(delays) != len(frames):
            delays = [80] * len(frames)
        return frames, delays

    def toggle_animations(self):
        on = bool(self.anim_enabled.get())
        self.cfg["animations"] = on
        save_config(self.cfg)
        if not on:
            # аккуратно замираем на первом кадре
            for st in self._anim.values():
                st["i"] = 0
                st["left"] = st["delays"][0]
                try:
                    m = st["master"]
                    m.tk.call(str(m), "copy", str(st["frames"][0]),
                              "-compositingrule", "set")
                except tk.TclError:
                    pass

    def _animate(self):
        # один тик двигает все анимированные смайлы: кадр копируется в мастер,
        # и Tk сам перерисовывает каждое его вхождение в лентах
        if not self.anim_enabled.get():
            self.root.after(200, self._animate)
            return
        for st in self._anim.values():
            st["left"] -= 50
            if st["left"] <= 0:
                st["i"] = (st["i"] + 1) % len(st["frames"])
                st["left"] = max(40, st["delays"][st["i"]])
                m = st["master"]
                try:
                    m.tk.call(str(m), "copy", str(st["frames"][st["i"]]),
                              "-compositingrule", "set")
                except tk.TclError:
                    pass
        self.root.after(50, self._animate)

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

    def _msg_highlight(self, item):
        """Упоминание ИЛИ ответ (reply) на моё сообщение — подсветка + счётчик hl."""
        if item[0] != "msg":
            return False
        if self._msg_mention(item):
            return True
        reply_parent = item[10] if len(item) > 10 else ""
        me = (self.cfg.get("login") or "").lower()
        return bool(reply_parent and me and reply_parent == me)

    def render_batch(self, items):
        """Раскладывает пачку по лентам: общий поток «*» + вкладки каналов."""
        if not items:
            return
        flagged = [(it, self._msg_highlight(it)) for it in items]
        mention_any = False
        changed = False
        for key, w in list(self.texts.items()):
            if key == "*":
                sel = flagged
            else:
                sel = [(it, hit) for it, hit in flagged
                       if it[0] == "sys" or (it[0] == "msg" and it[1] == key)]
            if not sel:
                continue
            if self._render_into(w, sel):
                mention_any = True
            # счётчики непрочитанных для невидимых сейчас лент каналов
            if key != "*" and not w.winfo_ismapped():
                msgs = [(it, h) for it, h in sel if it[0] == "msg"]
                if msgs:
                    c = self.unread.setdefault(key, {"n": 0, "hl": 0})
                    c["n"] += len(msgs)
                    c["hl"] += sum(1 for _, h in msgs if h)
                    changed = True
        if changed:
            self._style_tabs()
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
        try:
            visible = bool(w.winfo_ismapped())
        except tk.TclError:
            visible = False
        if visible:
            if at_bottom:
                w.see("end")
            elif w is self.text and self.layout != "columns" and not self._is_bare():
                # пользователь листает историю — счётчик новых снизу (одна лента)
                fresh = sum(1 for it, _ in flagged if it[0] == "msg")
                if fresh:
                    self.newmsg_count += fresh
                    self._show_newmsg_btn()
        return hit_any

    def _make_text(self):
        """Создаёт ленту чата со всеми тегами и биндами (одна на вкладку)."""
        w = tk.Text(self.feed_area, bg=BG, fg=FG, bd=0, highlightthickness=0,
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
        if self.layout != "tabs" or len(chans) < 2:
            self.tab_bar.pack_forget()
            return
        for key, text in [("*", T("tab_all"))] + [(c, "#" + c) for c in chans]:
            b = tk.Label(self.tab_bar, text=text, bg=BAR_BG, fg=SYS_FG,
                         font=("Segoe UI", 9), padx=8, pady=3, cursor="hand2")
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, k=key: self.switch_tab(k))
            self._tab_btns[key] = b
        self._style_tabs()
        if not self._is_bare():
            self.tab_bar.pack(fill="x", after=self.bar)

    def _style_tabs(self):
        for key, b in self._tab_btns.items():
            active = key == self.active_tab
            base = T("tab_all") if key == "*" else "#" + key
            c = None if active else self.unread.get(key)
            n = c["n"] if c else 0
            hl = c["hl"] if c else 0
            cap = lambda v: "99+" if v > 99 else str(v)
            if hl > 0:
                text = "%s ✱%s" % (base, cap(hl))   # упоминания/ответы
                fg = "#ff5c72"
                weight = "bold"
            elif n > 0:
                text = "%s ·%s" % (base, cap(n))     # просто непрочитанные
                fg = FG
                weight = "normal"
            else:
                text = base
                fg = ACCENT if active else SYS_FG
                weight = "bold" if active else "normal"
            b.configure(text=text, fg=fg, font=("Segoe UI", 9, weight))

    def switch_tab(self, key, force=False):
        if key not in self.texts:
            key = "*"
        if key == self.active_tab and not force:
            return
        self.newmsg_count = 0
        self.newmsg_btn.place_forget()
        self.active_tab = key
        self.unread.pop(key, None)  # открыли вкладку — счётчик сброшен
        self.cfg["active_tab"] = key
        save_config(self.cfg)
        # на вкладке канала сообщения отправляются в него
        chans = self.cfg.get("channels") or []
        if key in chans:
            self.send_index = chans.index(key)
            self.update_chan_btn()
        self._apply_layout()

    def set_layout(self, mode):
        self.layout = mode if mode in ("tabs", "unified", "columns") else "tabs"
        self.cfg["layout"] = self.layout
        save_config(self.cfg)
        self.newmsg_count = 0
        self.newmsg_btn.place_forget()
        self._apply_layout()

    def _col_header(self, ch):
        h = self._col_headers.get(ch)
        if h is None or not h.winfo_exists():
            h = tk.Label(self.feed_area, text="#" + ch, bg=BAR_BG, fg=SYS_FG,
                         font=("Segoe UI", 9, "bold"), pady=2, cursor="hand2")
            h.bind("<Button-1>", lambda e, c=ch: self._focus_column(c))
            self._col_headers[ch] = h
        return h

    def _focus_column(self, ch):
        chans = self.cfg.get("channels") or []
        if ch in chans:
            self.send_index = chans.index(ch)
            self.text = self.texts[ch]
            self.update_chan_btn()
            self._style_col_headers()

    def _style_col_headers(self):
        chans = (self.cfg.get("channels") or [])[:4]
        focus = chans[self.send_index] if self.send_index < len(chans) else (
            chans[0] if chans else None)
        for ch, h in self._col_headers.items():
            try:
                if h.winfo_ismapped():
                    h.configure(bg=BAR_BG, fg=ACCENT if ch == focus else SYS_FG)
            except tk.TclError:
                pass

    def _apply_layout(self):
        """Раскладывает feed_area под режим: вкладки / общая лента / колонки."""
        fa = self.feed_area
        for w in self.texts.values():
            try:
                w.grid_forget()
            except tk.TclError:
                pass
        for h in self._col_headers.values():
            try:
                h.grid_forget()
            except tk.TclError:
                pass
        for i in range(8):
            fa.grid_columnconfigure(i, weight=0, uniform="")
        fa.grid_rowconfigure(0, weight=0)
        fa.grid_rowconfigure(1, weight=1)
        chans = self.cfg.get("channels") or []

        bare = getattr(self, "_bare_now", False) or self._is_bare()
        if self.layout == "columns" and chans:
            cols = chans[:4]
            for i, ch in enumerate(cols):
                if not bare:  # в «голом» режиме — только сами ленты, без шапок
                    self._col_header(ch).grid(row=0, column=i, sticky="ew", padx=(0, 1))
                self.texts[ch].grid(row=0 if bare else 1, column=i,
                                    rowspan=2 if bare else 1,
                                    sticky="nsew", padx=(0, 1))
                fa.grid_columnconfigure(i, weight=1, uniform="cols")
            if bare:
                fa.grid_rowconfigure(0, weight=1)
            focus = cols[self.send_index] if self.send_index < len(cols) else cols[0]
            self.text = self.texts[focus]
            self.tab_bar.pack_forget()
            self._style_col_headers()
        elif self.layout == "unified":
            self.active_tab = "*"
            self.texts["*"].grid(row=0, column=0, rowspan=2, sticky="nsew")
            fa.grid_columnconfigure(0, weight=1)
            fa.grid_rowconfigure(0, weight=1)
            self.text = self.texts["*"]
            self.tab_bar.pack_forget()
        else:  # tabs
            key = self.active_tab if self.active_tab in self.texts else "*"
            self.texts[key].grid(row=0, column=0, rowspan=2, sticky="nsew")
            fa.grid_columnconfigure(0, weight=1)
            fa.grid_rowconfigure(0, weight=1)
            self.text = self.texts[key]
            self._rebuild_tabs()
            self._style_tabs()

        self.grip.lift()
        try:
            self.text.see("end")
        except tk.TclError:
            pass

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

    def _build_icon_photos(self):
        """(Пере)генерирует PhotoImage мод-иконок: обычная (серая) и hover (яркая)."""
        self._icon_norm = {}   # kind -> PhotoImage (обычный цвет)
        self._icon_hot = {}    # kind -> PhotoImage (при наведении)
        self._icon_to_hot = {} # имя photo -> hover-photo (для смены на лету)
        self._icon_to_norm = {}
        if not HAS_PIL:
            return
        size = max(14, int(self.cfg.get("font_size", 11) * 1.5))
        for kind, hot_color in (("ban", "#ff6b6b"), ("timeout", FG),
                                ("warn", "#ffcf5c"), ("delete", FG),
                                ("announce", ACCENT), ("emote", ACCENT)):
            n64 = draw_mod_icon(kind, size, SYS_FG)
            h64 = draw_mod_icon(kind, size, hot_color)
            if not n64 or not h64:
                continue
            try:
                pn = tk.PhotoImage(data=n64)
                ph = tk.PhotoImage(data=h64)
            except tk.TclError:
                continue
            self._icon_norm[kind] = pn
            self._icon_hot[kind] = ph
            self._icon_to_hot[str(pn)] = ph
            self._icon_to_norm[str(ph)] = pn
        # кнопки на панели ввода могли уже существовать — обновляем их картинки
        eb = getattr(self, "emote_btn", None)
        if eb is not None:
            try:
                eb.configure(image=self._icon_norm.get("emote") or "")
            except tk.TclError:
                pass
        ab = getattr(self, "announce_btn", None)
        if ab is not None and getattr(self, "_announce_shown", False):
            try:
                ab.configure(image=self._icon_norm.get("announce") or "")
            except tk.TclError:
                pass

    def _insert_mod_icon(self, w, kind, mtag):
        img = getattr(self, "_icon_norm", {}).get(kind)
        if img is None:  # без Pillow — запасной текстовый глиф
            glyph = {"ban": "⊘", "timeout": "◷", "warn": "⚠", "delete": "🗑"}[kind]
            itag = "%s.%s" % (mtag, kind[0])
            w.tag_configure(itag, foreground=SYS_FG, font=self.font_chip)
            w.tag_bind(itag, "<Button-1>",
                       lambda e, k=kind, t=mtag: self._icon_action(k, t) or "break")
            w.insert("end", glyph + " ", (itag, "nicklink"))
            return
        itag = "%s.%s" % (mtag, kind[0])
        w.image_create("end", image=img, padx=2)
        w.tag_add(itag, "end-2c", "end-1c")  # помечаем только что вставленную картинку
        w.tag_bind(itag, "<Button-1>",
                   lambda e, k=kind, t=mtag: self._icon_action(k, t) or "break")
        w.tag_bind(itag, "<Enter>", self._icon_enter)
        w.tag_bind(itag, "<Leave>", self._icon_leave)

    def _icon_enter(self, e):
        w = e.widget
        try:
            idx = w.index("@%d,%d" % (e.x, e.y))
            cur = w.image_cget(idx, "image")
            hot = self._icon_to_hot.get(cur)
            if hot is not None:
                w.image_configure(idx, image=hot)
            w.configure(cursor="hand2")
        except tk.TclError:
            pass

    def _icon_leave(self, e):
        w = e.widget
        try:
            idx = w.index("@%d,%d" % (e.x, e.y))
            cur = w.image_cget(idx, "image")
            norm = self._icon_to_norm.get(cur)
            if norm is not None:
                w.image_configure(idx, image=norm)
            w.configure(cursor="arrow")
        except tk.TclError:
            pass

    def msg_meta_tag(self, w, channel, login, name, uid, mid):
        """Тег с данными сообщения: правый клик по нику -> карточка действий."""
        if not login:
            return None
        self._msg_seq += 1
        tag = "mm%d" % self._msg_seq
        self._msg_meta[tag] = (channel, login, name, uid, mid)
        if len(self._msg_meta) > 900:
            for k in list(self._msg_meta)[:300]:
                self._msg_meta.pop(k, None)
        w.tag_bind(tag, "<Button-3>",
                   lambda e, t=tag: self.user_action_popup(e, t))
        return tag

    def _is_mod(self, channel):
        us = self.irc.userstate.get(channel) if self.irc else None
        return bool(us and len(us) > 3 and us[3])

    def user_action_popup(self, event, tag):
        meta = self._msg_meta.get(tag)
        if not meta:
            return "break"
        channel, login, name, uid, mid = meta
        if self._action_win is not None:
            try:
                self._action_win.destroy()
            except tk.TclError:
                pass
        win = tk.Toplevel(self.root)
        self._action_win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BORDER)
        box = tk.Frame(win, bg=BAR_BG, padx=6, pady=6)
        box.pack(padx=1, pady=1)
        tk.Label(box, text=name, bg=BAR_BG, fg=readable_color("", login),
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=4, pady=(0, 4))

        def item(label_key, cmd, danger=False, confirm=False):
            b = tk.Label(box, text=T(label_key), bg=BAR_BG,
                         fg="#e06c6c" if danger else FG,
                         font=("Segoe UI", 10), anchor="w", padx=8, pady=4,
                         cursor="hand2")
            b.pack(fill="x")
            b.bind("<Enter>", lambda e: b.configure(bg=CHIPBTN_BG))
            b.bind("<Leave>", lambda e: b.configure(bg=BAR_BG))
            state = {"armed": False}

            def click(e):
                if confirm and not state["armed"]:
                    state["armed"] = True
                    b.configure(text=T("act_ban_confirm"))
                    return
                win.destroy()
                cmd()
            b.bind("<Button-1>", click)

        item("act_profile", lambda: webbrowser.open("https://twitch.tv/" + login))
        me = (self.cfg.get("login") or "").lower()
        if self._is_mod(channel) and uid and login != me:
            if mid:
                item("act_delete",
                     lambda: self._mod_action("delete", channel, uid, mid, name))
            item("act_timeout",
                 lambda: self._mod_action("timeout", channel, uid, mid, name))
            item("act_warn",
                 lambda: self._mod_action("warn", channel, uid, mid, name))
            item("act_ban",
                 lambda: self._mod_action("ban", channel, uid, mid, name),
                 danger=True, confirm=True)
        win.bind("<FocusOut>", lambda e: win.destroy())
        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        x = min(event.x_root, win.winfo_screenwidth() - win.winfo_width() - 8)
        win.geometry("+%d+%d" % (x, event.y_root + 6))
        dwm_round(win, small=True)
        win.focus_force()
        return "break"

    def _icon_action(self, kind, mtag):
        meta = self._msg_meta.get(mtag)
        if not meta:
            return
        channel, login, name, uid, mid = meta
        if kind == "ban":
            key = channel + "/" + login
            if key not in self._armed_bans:
                self._armed_bans.add(key)
                self.sys_message(T("ban_arm", name))
                self.root.after(3000, lambda: self._armed_bans.discard(key))
                return
            self._armed_bans.discard(key)
        self._mod_action(kind, channel, uid, mid, name)

    def _mod_action(self, kind, channel, uid, mid, name):
        """Модерация через Helix — в пуле, результат приходит в чат sys-строкой."""
        token = self.cfg.get("token", "")
        cid = self.cfg.get("client_id", "")
        my_id = self.cfg.get("user_id", "")
        irc = self.irc

        def run():
            bid = irc.channel_ids.get(channel) if irc else None
            if not (token and cid and my_id and bid):
                irc.put(("sys", T("mod_err", "нет данных токена/канала")))
                return
            params = {"broadcaster_id": bid, "moderator_id": my_id}
            if kind == "delete":
                params["message_id"] = mid
                code, resp = helix("DELETE", "moderation/chat", token, cid, params)
                ok_msg = T("mod_deleted", name)
            elif kind == "timeout":
                code, resp = helix("POST", "moderation/bans", token, cid, params,
                                   {"data": {"user_id": uid, "duration": 600}})
                ok_msg = T("mod_timeout", name)
            elif kind == "warn":
                code, resp = helix("POST", "moderation/warnings", token, cid, params,
                                   {"data": {"user_id": uid, "reason": T("warn_reason")}})
                ok_msg = T("mod_warned", name)
            else:
                code, resp = helix("POST", "moderation/bans", token, cid, params,
                                   {"data": {"user_id": uid}})
                ok_msg = T("mod_banned", name)
            if code in (200, 204):
                irc.put(("sys", ok_msg))
            elif code == 401:
                irc.put(("sys", T("mod_err_scope")))
            elif code == 403:
                irc.put(("sys", T("mod_err_notmod", channel)))
            else:
                detail = (resp or {}).get("message") or code
                irc.put(("sys", T("mod_err", detail)))

        DOWNLOAD_POOL.submit(run)

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
        uid = item[8] if len(item) > 8 else ""
        mid = item[9] if len(item) > 9 else ""
        line_no = int(w.index("end-1c").split(".")[0])
        multi = len(self.cfg.get("channels") or []) > 1
        if multi and channel and w is self.texts.get("*"):
            w.insert("end", "#%s " % channel, "chip")
        for bkey, b64 in badges:
            bimg = self.cached_image("b:" + bkey, b64)
            if bimg is not None:
                w.image_create("end", image=bimg, padx=2)
        utag = self.user_tag(w, login or name)
        mtag = self.msg_meta_tag(w, channel, login or name.lower(), name, uid, mid)
        # мод-иконки перед сообщением, как в мод-виде Twitch: бан/таймаут/варн/удалить
        if (mtag and uid and self.mod_icons.get() and self._is_mod(channel)
                and (login or name.lower()) != (self.cfg.get("login") or "").lower()):
            for kind in ("ban", "timeout", "warn", "delete"):
                if kind == "delete" and not mid:
                    continue
                self._insert_mod_icon(w, kind, mtag)
        nick_tags = ((self.color_tag(w, color, name), "nicklink")
                     + ((utag,) if utag else ()) + ((mtag,) if mtag else ()))
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
