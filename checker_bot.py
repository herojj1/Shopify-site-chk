#!/usr/bin/env python3
"""
Shopify Sites Checker — Telegram Bot
=====================================
Proxy management + site classification + smart command UX.

Commands:
  /start            — welcome + button menu
  /help             — full command list
  /addproxy         — add proxies (paste text or upload .txt)
  /listproxies      — show proxy pool
  /clearproxies     — wipe proxy pool
  /checkproxy       — test proxies (all or one) → live/dead + latency + geo
  /sites            — upload a .txt of sites → classify live/captcha/dead
  /status           — current job progress
  /cancel           — stop current job
  /export           — download live.txt / captcha.txt / dead.txt
  /stats            — aggregate counts across all past jobs

Env:
  BOT_TOKEN         — required
  BOT_ADMIN         — comma-separated telegram user IDs (optional; empty = public)
  CHECKER_THREADS   — parallel workers (default 8, cap 20)
  PROXY_TEST_URL    — default http://ip-api.com/json/ (returns country/latency)
"""
import asyncio
import concurrent.futures
import io
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
    CallbackQueryHandler,
)

from checkout_engine import (
    run_checkout_for_card, normalize_proxy, CheckStatus, parse_card_entry,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8517366800:AAHyFIca1eSMpHlNffb_24Cg3HLkV5QPf_I").strip()
ADMIN_IDS = {
    int(x) for x in (os.environ.get("BOT_ADMIN", "8871910561") or "").split(",")
    if x.strip().isdigit()
}
MAX_THREADS = min(max(int(os.environ.get("CHECKER_THREADS", "8")), 1), 20)
PROXY_TEST_URL = os.environ.get("PROXY_TEST_URL", "http://ip-api.com/json/")

TEST_CARD = "4111111111111111|12|2030|123"

STATE_DIR = Path(os.environ.get("STATE_DIR", "/tmp/shopify_checker"))
STATE_DIR.mkdir(parents=True, exist_ok=True)


# ──────────────────────── user state ──────────────────────────────────

@dataclass
class UserState:
    proxies: List[str] = field(default_factory=list)
    job: Optional[Dict[str, Any]] = None
    results: Dict[str, List] = field(default_factory=lambda: {
        "LIVE": [], "CAPTCHA": [], "DEAD": [],
    })
    proxy_status: Dict[str, Dict] = field(default_factory=dict)
    total_sites: int = 0
    total_live: int = 0
    total_captcha: int = 0
    total_dead: int = 0


_STATES: Dict[int, UserState] = {}


def state_for(uid: int) -> UserState:
    if uid not in _STATES:
        _STATES[uid] = UserState()
        _load_proxies(uid)
    return _STATES[uid]


def _proxy_file(uid: int) -> Path:
    return STATE_DIR / f"proxies_{uid}.json"


def _load_proxies(uid: int):
    p = _proxy_file(uid)
    if not p.exists():
        return
    try:
        _STATES[uid].proxies = json.loads(p.read_text())
    except Exception:
        pass


def _save_proxies(uid: int):
    try:
        _proxy_file(uid).write_text(json.dumps(_STATES[uid].proxies))
    except Exception:
        pass


def allowed(uid: int) -> bool:
    return (not ADMIN_IDS) or uid in ADMIN_IDS


# ──────────────────────── proxy parse/format ──────────────────────────

def parse_proxy_line(raw: str) -> Optional[str]:
    """Accept many shapes. Return normalized http://... or socks5://..."""
    s = (raw or "").strip()
    if not s or s.startswith("#"):
        return None
    try:
        return normalize_proxy(s)
    except Exception:
        pass

    # try ip:port:user:pass
    parts = s.split(":")
    if len(parts) == 4 and parts[1].isdigit():
        ip, port, user, pw = parts
        return f"http://{user}:{pw}@{ip}:{port}"
    # try user:pass@host:port
    if "@" in s and ":" in s.split("@", 1)[1]:
        return f"http://{s}"
    return None


def short_proxy(p: str) -> str:
    """Compact display: host:port (or user@host:port)"""
    try:
        u = urlparse(p)
        if u.hostname and u.port:
            return f"{u.hostname}:{u.port}"
    except Exception:
        pass
    return p[:40]


# ──────────────────────── proxy check ─────────────────────────────────

def check_proxy(proxy: str, timeout: float = 10.0) -> Dict:
    """Return {live, latency_ms, ip, country, city, error}"""
    t0 = time.perf_counter()
    try:
        r = requests.get(
            PROXY_TEST_URL,
            proxies={"http": proxy, "https": proxy},
            timeout=timeout,
            headers={"User-Agent": "curl/8.0"},
        )
        latency = round((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            return {"live": False, "error": f"HTTP {r.status_code}"}
        try:
            j = r.json()
        except Exception:
            j = {}
        return {
            "live": True,
            "latency_ms": latency,
            "ip": j.get("query") or "",
            "country": j.get("country") or j.get("countryCode") or "",
            "city": j.get("city") or "",
        }
    except requests.exceptions.ProxyError:
        return {"live": False, "error": "proxy refused"}
    except requests.exceptions.ConnectTimeout:
        return {"live": False, "error": "timeout"}
    except requests.exceptions.ReadTimeout:
        return {"live": False, "error": "read timeout"}
    except Exception as e:
        return {"live": False, "error": str(e)[:60]}


# ──────────────────────── site check ──────────────────────────────────

def _classify(r) -> str:
    if r.status in (CheckStatus.CHARGED, CheckStatus.APPROVED):
        return "LIVE"
    if r.status == CheckStatus.DECLINED:
        code = (r.status_code or "").upper()
        return "CAPTCHA" if "CAPTCHA" in code else "LIVE"
    code = (r.status_code or "").upper()
    return "CAPTCHA" if "CAPTCHA" in code else "DEAD"


def check_site(site: str, proxy: str, card: str) -> Dict:
    t0 = time.perf_counter()
    try:
        r = run_checkout_for_card(site, card, proxy or "", low=True)
        verdict = _classify(r)
        detail = r.status_code or r.status.name
        if r.error:
            detail = str(r.error)[:120]
        return {
            "site": site, "verdict": verdict, "detail": detail,
            "elapsed": round(time.perf_counter() - t0, 1),
        }
    except Exception as e:
        return {
            "site": site, "verdict": "DEAD",
            "detail": f"exc: {e}"[:120],
            "elapsed": round(time.perf_counter() - t0, 1),
        }


def normalize_site(raw: str) -> str:
    s = (raw or "").strip()
    if not s or s.startswith("#"):
        return ""
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    p = urlparse(s)
    if not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


# ──────────────────────── message helpers ─────────────────────────────

def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Sites",  callback_data="help_sites"),
         InlineKeyboardButton("🔌 Proxies", callback_data="help_proxies")],
        [InlineKeyboardButton("⚡ Run",    callback_data="help_run"),
         InlineKeyboardButton("📊 Status",  callback_data="help_status")],
    ])


def welcome_text(name: str) -> str:
    return (
        f"👋 *Hey {name}*\n\n"
        "I check *Shopify* sites with a fake card and tell you which ones\n"
        "will charge you (LIVE) vs block you (CAPTCHA) vs are dead.\n\n"
        "*Quick start*\n"
        "1️⃣  `/addproxy` — drop in a proxy list\n"
        "2️⃣  `/checkproxy` — verify they're live\n"
        "3️⃣  `/sites` — upload a `.txt` of shop URLs\n"
        "4️⃣  `/export` — get live/captcha/dead back\n\n"
        "Tap a button for details 👇"
    )


# ──────────────────────── command handlers ────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        await update.message.reply_text("🚫 not authorized")
        return
    await update.message.reply_text(
        welcome_text(update.effective_user.first_name or "friend"),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=menu_kb(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id):
        return
    text = (
        "📖 *Commands*\n\n"
        "*Proxy pool*\n"
        "`/addproxy` — paste list or upload `.txt`\n"
        "`/listproxies` — view pool\n"
        "`/checkproxy` — test all (live/dead/latency/geo)\n"
        "`/clearproxies` — wipe pool\n\n"
        "*Site checking*\n"
        "`/sites` — upload a `.txt` of shop URLs\n"
        "  → bot tests each with a fake card, sorts into LIVE/CAPTCHA/DEAD\n\n"
        "*Job control*\n"
        "`/status` — progress of running job\n"
        "`/cancel` — stop current job\n"
        "`/export` — download results as three files\n"
        "`/stats` — lifetime totals\n\n"
        "*Proxy formats accepted*\n"
        "`host:port`\n"
        "`host:port:user:pass`\n"
        "`user:pass@host:port`\n"
        "`http://user:pass@host:port`\n"
        "`socks5://user:pass@host:port`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_addproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return

    # If arguments given inline
    if context.args:
        raw = " ".join(context.args)
        _ingest_proxies(uid, raw)
        st = state_for(uid)
        await update.message.reply_text(
            f"✅ added proxies — pool now has *{len(st.proxies)}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Otherwise ask for paste or file
    st = state_for(uid)
    st.job = st.job or {}  # placeholder not needed
    await update.message.reply_text(
        "📥 Send me a proxy list as text (one per line)\n"
        "or upload a `.txt` file.\n\n"
        "Formats: `host:port`, `host:port:user:pass`, `user:pass@host:port`",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data["awaiting"] = "proxies"


async def cmd_listproxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    st = state_for(uid)
    if not st.proxies:
        await update.message.reply_text("📭 pool is empty — `/addproxy` to fill it.",
                                         parse_mode=ParseMode.MARKDOWN)
        return
    lines = [f"🔌 *Proxy pool — {len(st.proxies)}*", ""]
    for i, p in enumerate(st.proxies[:30], 1):
        flag = ""
        ps = st.proxy_status.get(p)
        if ps:
            flag = " ✅" if ps.get("live") else " ❌"
        lines.append(f"`{i:>2}.` {short_proxy(p)}{flag}")
    if len(st.proxies) > 30:
        lines.append(f"_…and {len(st.proxies) - 30} more_")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_clearproxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    st = state_for(uid)
    n = len(st.proxies)
    st.proxies = []
    st.proxy_status = {}
    _save_proxies(uid)
    await update.message.reply_text(f"🗑 cleared {n} proxies")


async def cmd_checkproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    st = state_for(uid)
    if not st.proxies:
        await update.message.reply_text("no proxies in pool")
        return

    msg = await update.message.reply_text(
        f"🔎 testing *{len(st.proxies)}* proxies…",
        parse_mode=ParseMode.MARKDOWN,
    )

    loop = asyncio.get_running_loop()
    done = 0
    total = len(st.proxies)
    live = 0
    dead = 0
    last_edit = 0.0

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(total, 20)) as ex:
        futs = {ex.submit(check_proxy, p): p for p in st.proxies}
        for fut in concurrent.futures.as_completed(futs):
            proxy = futs[fut]
            try:
                info = await loop.run_in_executor(None, fut.result)
            except Exception as e:
                info = {"live": False, "error": str(e)[:60]}
            st.proxy_status[proxy] = info
            done += 1
            if info.get("live"):
                live += 1
            else:
                dead += 1

            now = time.time()
            if now - last_edit > 2.0 or done == total:
                last_edit = now
                try:
                    await msg.edit_text(
                        f"🔎 testing… *{done}/{total}*\n"
                        f"✅ live: `{live}`  ❌ dead: `{dead}`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

    # final summary with details
    lines = [f"📊 *Proxy check — {live}/{total} live*", ""]
    for p in st.proxies[:20]:
        info = st.proxy_status.get(p, {})
        if info.get("live"):
            lines.append(
                f"✅ `{short_proxy(p)}` "
                f"_{info.get('latency_ms','?')}ms_ "
                f"{info.get('country','')} {info.get('city','')}"
            )
        else:
            lines.append(f"❌ `{short_proxy(p)}` — {info.get('error','dead')}")
    if total > 20:
        lines.append(f"_…and {total - 20} more_")

    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ──────────────────────── sites check ─────────────────────────────────

async def cmd_sites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    st = state_for(uid)
    if st.job and st.job.get("running"):
        await update.message.reply_text("⚠️ a job is already running — `/cancel` first.",
                                         parse_mode=ParseMode.MARKDOWN)
        return
    st.results = {"LIVE": [], "CAPTCHA": [], "DEAD": []}
    context.user_data["awaiting"] = "sites"
    await update.message.reply_text(
        "📥 Send me a `.txt` of shop URLs — one per line.\n"
        "Or paste them as text.\n\n"
        f"🔌 will use *{len(st.proxies)}* proxy(ies)"
        if st.proxies else "⚠️ no proxies loaded — going direct",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    st = state_for(uid)
    j = st.job
    if not j or not j.get("running"):
        await update.message.reply_text("💤 no active job")
        return
    pct = (j["done"] / j["total"] * 100) if j["total"] else 0
    bar_len = 20
    filled = int(pct / 100 * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)
    await update.message.reply_text(
        f"⚡ *Running*\n"
        f"`{bar}` *{pct:.0f}%*\n\n"
        f"done: `{j['done']}/{j['total']}`\n"
        f"✅ live: `{j['live']}`\n"
        f"🚫 captcha: `{j['captcha']}`\n"
        f"💀 dead: `{j['dead']}`\n"
        f"⏱ elapsed: `{time.time() - j['started']:.0f}s`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    st = state_for(uid)
    if st.job and st.job.get("running"):
        st.job["cancel"] = True
        await update.message.reply_text("🛑 cancelling…")
    else:
        await update.message.reply_text("no running job")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    st = state_for(uid)
    if not any(st.results.values()):
        await update.message.reply_text("nothing to export yet")
        return

    async def send(name: str, rows: List[Dict], caption: str):
        if not rows:
            return
        buf = io.StringIO()
        buf.write(f"# {name} — {len(rows)}\n\n")
        for r in rows:
            buf.write(f"{r['site']}  # {r.get('detail','')}  ({r.get('elapsed',0)}s)\n")
        b = io.BytesIO(buf.getvalue().encode())
        await update.message.reply_document(
            document=b, filename=name, caption=caption,
        )

    await send("live.txt",    sorted(st.results["LIVE"], key=lambda x: x.get("elapsed", 0)),
               f"✅ {len(st.results['LIVE'])} LIVE")
    await send("captcha.txt", st.results["CAPTCHA"],
               f"🚫 {len(st.results['CAPTCHA'])} CAPTCHA")
    await send("dead.txt",    st.results["DEAD"],
               f"💀 {len(st.results['DEAD'])} DEAD")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    st = state_for(uid)
    await update.message.reply_text(
        f"📊 *Lifetime stats*\n\n"
        f"sites checked: `{st.total_sites}`\n"
        f"✅ live: `{st.total_live}`\n"
        f"🚫 captcha: `{st.total_captcha}`\n"
        f"💀 dead: `{st.total_dead}`\n\n"
        f"🔌 proxies in pool: `{len(st.proxies)}`",
        parse_mode=ParseMode.MARKDOWN,
    )


# ──────────────────────── text/document ingest ────────────────────────

def _ingest_proxies(uid: int, text: str):
    st = state_for(uid)
    existing = set(st.proxies)
    added = 0
    for line in text.splitlines():
        p = parse_proxy_line(line)
        if p and p not in existing:
            st.proxies.append(p)
            existing.add(p)
            added += 1
    _save_proxies(uid)
    return added


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    waiting = context.user_data.get("awaiting")
    text = update.message.text or ""

    if waiting == "proxies":
        n = _ingest_proxies(uid, text)
        context.user_data.pop("awaiting", None)
        st = state_for(uid)
        await update.message.reply_text(
            f"✅ added `{n}` new · pool now `{len(st.proxies)}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if waiting == "sites":
        context.user_data.pop("awaiting", None)
        await _run_sites_job(update, context, text)
        return

    # otherwise ignore


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    doc = update.message.document
    if not doc or not doc.file_name.endswith((".txt", ".csv")):
        return
    if doc.file_size and doc.file_size > 512 * 1024:
        await update.message.reply_text("file too big (max 512 KB)")
        return

    tg_file = await context.bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    text = buf.getvalue().decode("utf-8", errors="ignore")

    waiting = context.user_data.get("awaiting")
    if waiting == "proxies":
        n = _ingest_proxies(uid, text)
        context.user_data.pop("awaiting", None)
        st = state_for(uid)
        await update.message.reply_text(
            f"✅ added `{n}` new · pool now `{len(st.proxies)}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if waiting == "sites":
        context.user_data.pop("awaiting", None)
        await _run_sites_job(update, context, text)
        return

    # heuristic: if lines look like proxies, ingest
    looks_proxy = any(":" in ln and "@" not in ln and "." in ln for ln in text.splitlines()[:5])
    if looks_proxy:
        n = _ingest_proxies(uid, text)
        st = state_for(uid)
        await update.message.reply_text(
            f"🔌 detected proxy list — added `{n}` · pool `{len(st.proxies)}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text("🤔 I didn't know what to do with that file. try `/sites` or `/addproxy` first.")


# ──────────────────────── site job runner ─────────────────────────────

async def _run_sites_job(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    uid = update.effective_user.id
    st = state_for(uid)

    sites: List[str] = []
    seen = set()
    for ln in text.splitlines():
        s = normalize_site(ln)
        if s and s not in seen:
            seen.add(s)
            sites.append(s)

    if not sites:
        await update.message.reply_text("no valid sites found")
        return

    threads = min(MAX_THREADS, len(sites))
    st.job = {
        "running": True, "started": time.time(),
        "total": len(sites), "done": 0,
        "live": 0, "captcha": 0, "dead": 0,
        "cancel": False,
    }

    progress = await update.message.reply_text(
        f"⚡ starting *{len(sites)}* sites · {threads} workers "
        f"· {len(st.proxies) or 1} proxy(ies)",
        parse_mode=ParseMode.MARKDOWN,
    )

    loop = asyncio.get_running_loop()
    last_edit = [0.0]

    async def run_one(idx: int, site: str) -> Dict:
        proxy = st.proxies[idx % len(st.proxies)] if st.proxies else ""
        return await loop.run_in_executor(
            None, check_site, site, proxy, TEST_CARD,
        )

    async def progress_cb(done: int, total: int, r: Dict):
        st.job["done"] = done
        st.job[r["verdict"].lower()] = st.job.get(r["verdict"].lower(), 0) + 1
        now = time.time()
        if now - last_edit[0] < 2.0 and done != total:
            return
        last_edit[0] = now
        pct = done / total * 100
        bar_len = 18
        filled = int(pct / 100 * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        try:
            await progress.edit_text(
                f"`{bar}` *{pct:.0f}%*  `{done}/{total}`\n\n"
                f"✅ live: `{st.job['live']}`\n"
                f"🚫 captcha: `{st.job['captcha']}`\n"
                f"💀 dead: `{st.job['dead']}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    sem = asyncio.Semaphore(threads)
    done_counter = [0]

    async def guarded(idx: int, site: str):
        async with sem:
            if st.job.get("cancel"):
                return
            r = await run_one(idx, site)
            st.results[r["verdict"]].append(r)
            done_counter[0] += 1
            await progress_cb(done_counter[0], len(sites), r)

    tasks = [asyncio.create_task(guarded(i, s)) for i, s in enumerate(sites)]
    await asyncio.gather(*tasks, return_exceptions=True)

    st.job["running"] = False
    elapsed = time.time() - st.job["started"]

    st.total_sites   += st.job["done"]
    st.total_live    += st.job["live"]
    st.total_captcha += st.job["captcha"]
    st.total_dead    += st.job["dead"]

    summary = (
        f"✅ *done in {elapsed:.0f}s*\n\n"
        f"✅ live: `{st.job['live']}`\n"
        f"🚫 captcha: `{st.job['captcha']}`\n"
        f"💀 dead: `{st.job['dead']}`\n\n"
        f"type `/export` to download"
    )
    try:
        await progress.edit_text(summary, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)


# ──────────────────────── callback buttons ────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not allowed(q.from_user.id):
        return
    if q.data == "help_sites":
        await q.message.reply_text(
            "🌐 *Sites*\n\n"
            "`/sites` → upload a `.txt` of shop URLs\n"
            "Bot tests each with a fake card and sorts into:\n"
            "• LIVE — accepts checkout\n"
            "• CAPTCHA — blocks with captcha\n"
            "• DEAD — timeout or broken\n\n"
            "`/export` → download the three files",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif q.data == "help_proxies":
        await q.message.reply_text(
            "🔌 *Proxies*\n\n"
            "`/addproxy` → paste or upload\n"
            "`/checkproxy` → test every proxy\n"
            "`/listproxies` → view pool\n"
            "`/clearproxies` → wipe\n\n"
            "Formats: `host:port`, `host:port:user:pass`,\n"
            "`user:pass@host:port`, `socks5://...`",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif q.data == "help_run":
        await q.message.reply_text(
            "⚡ *Run*\n\n"
            "1. load proxies → `/addproxy`\n"
            "2. verify → `/checkproxy`\n"
            "3. load sites → `/sites`\n"
            "4. watch → `/status`\n"
            "5. download → `/export`",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif q.data == "help_status":
        await cmd_status(update, context)


# ──────────────────────── boot ────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN env var is required")

    print(f"[bot] starting — admins: {ADMIN_IDS or 'public'}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("addproxy",     cmd_addproxy))
    app.add_handler(CommandHandler("listproxies",  cmd_listproxies))
    app.add_handler(CommandHandler("checkproxy",   cmd_checkproxy))
    app.add_handler(CommandHandler("clearproxies", cmd_clearproxies))
    app.add_handler(CommandHandler("sites",        cmd_sites))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("cancel",       cmd_cancel))
    app.add_handler(CommandHandler("export",       cmd_export))
    app.add_handler(CommandHandler("stats",        cmd_stats))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
