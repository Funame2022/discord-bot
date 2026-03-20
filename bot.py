# file: bot-test-ephemeral-isolated.py
import os
import json
import asyncio
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import discord
from discord.ext import tasks, commands

# ------------------ CONFIGURATION ----------------
# NOTE: Use environment variable DISCORD_TOKEN for the bot token.
TOKEN = os.getenv("DISCORD_TOKEN")
BOT_GUILD_ID = os.getenv("BOT_GUILD_ID")
MONITORED_FILE = "monitored.json"
CONFIG_FILE = "config.json"
MONITORED_IMAGE_PATH = "/mnt/data/93b3f5bc-2247-4f67-a02f-7eb4209abc2c.png"

# Default alert threshold (how long since last message before sending an alert)
THRESHOLD_SECONDS = 300

# Default scanning interval (how often the bot scans). Can be changed with /st.
CHECK_INTERVAL_SECONDS = 180

AUTO_DELETE_SECONDS = 300          # non-alert bot messages in log channels will be auto-deleted
UI_TEMP_DELETE_SECONDS = 10
LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

PING_EVERYONE = True
PING_ROLE_IDS = []
# ---------------------------------------------------

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# In-memory structures
monitored = {}           # channel_id -> record
config = {}              # persisted per-guild config and global settings
preserved_alerts = {}    # alerts preserved when monitor removed
guild_locks = {}         # guild_id -> asyncio.Lock() for race-safety

# Timer utilities (for remaining-time)
next_check_time = None   # datetime of next scheduled check_loop run
timer_task = None        # asyncio.Task for the remaining-time updater

# Cache to avoid frequent edits (keyed by guild id)
# Each value: {"last_str": "MM:SS", "last_update": datetime}
remaining_cache = {}

# ---------------- Persistence helpers ----------------
def iso_dt(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def from_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z")
        except:
            return None


def save_monitored():
    try:
        to_save = {}
        for k, v in monitored.items():
            to_save[str(k)] = {
                "log_channel": v.get("log_channel"),
                "last_message_time": iso_dt(v.get("last_message_time")),
                "alert_count": v.get("alert_count", 0),
                "alert_message_id": v.get("alert_message_id"),
                "alert_sent_time": iso_dt(v.get("alert_sent_time")),
                "confirmed": bool(v.get("confirmed", False)),
                "confirmed_by": int(v.get("confirmed_by")) if v.get("confirmed_by") else None
            }
        with open(MONITORED_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Error saving monitored:", e)


def load_monitored():
    global monitored
    if os.path.exists(MONITORED_FILE):
        try:
            with open(MONITORED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            monitored = {}
            for k, v in data.items():
                cid = int(k)
                monitored[cid] = {
                    "log_channel": v.get("log_channel") if v.get("log_channel") is None else int(v.get("log_channel")),
                    "last_message_time": from_iso(v.get("last_message_time")),
                    "alert_count": int(v.get("alert_count", 0)),
                    "alert_message_id": v.get("alert_message_id"),
                    "alert_sent_time": from_iso(v.get("alert_sent_time")),
                    "confirmed": bool(v.get("confirmed", False)),
                    "confirmed_by": int(v.get("confirmed_by")) if v.get("confirmed_by") else None
                }
            return
        except Exception as e:
            print("Failed to load monitored.json:", e)
    monitored = {}
    save_monitored()


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Error saving config:", e)


def load_config():
    """
    Loads config and applies persistent settings:
    - ensures config structure and keys exist
    - loads saved scan interval (if any) into global CHECK_INTERVAL_SECONDS
    """
    global config, CHECK_INTERVAL_SECONDS
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                if "guilds" not in cfg:
                    ui = cfg.get("ui_channel_id")
                    cfg = {"ui_channel_id": ui, "guilds": {}}
                # ensure remaining_msg_id exists for each guild entry
                for gid, ent in cfg.get("guilds", {}).items():
                    if isinstance(ent, dict) and "remaining_msg_id" not in ent:
                        ent["remaining_msg_id"] = None
                # load saved scan interval if present
                if "scan_interval" in cfg and isinstance(cfg["scan_interval"], int):
                    CHECK_INTERVAL_SECONDS = int(cfg["scan_interval"])
                config = cfg
            else:
                config = {"ui_channel_id": None, "guilds": {}}
            return
        except Exception as e:
            print("Failed to load config.json:", e)
    # default structure
    config = {"ui_channel_id": None, "guilds": {}, "scan_interval": CHECK_INTERVAL_SECONDS}
    save_config()


# ---------------- Guild-level helpers ----------------
def ensure_guild_entry(guild_id: int):
    gid = str(guild_id)
    if "guilds" not in config:
        config["guilds"] = {}
    if gid not in config["guilds"]:
        config["guilds"][gid] = {"log_channel_id": None, "ui_channel_id": None, "monitored": [], "remaining_msg_id": None}
    else:
        if "remaining_msg_id" not in config["guilds"][gid]:
            config["guilds"][gid]["remaining_msg_id"] = None
    return config["guilds"][gid]


def get_guild_log_channel(guild_id: int):
    gid = str(guild_id)
    if "guilds" in config and gid in config["guilds"]:
        v = config["guilds"][gid].get("log_channel_id")
        return int(v) if v else None
    return None


def set_guild_log_channel(guild_id: int, channel_id: int):
    ent = ensure_guild_entry(guild_id)
    ent["log_channel_id"] = int(channel_id)
    save_config()


def get_guild_ui_channel(guild_id: int):
    gid = str(guild_id)
    if "guilds" in config and gid in config["guilds"]:
        v = config["guilds"][gid].get("ui_channel_id")
        return int(v) if v else None
    return config.get("ui_channel_id")


def set_guild_ui_channel(guild_id: int, channel_id: int):
    ent = ensure_guild_entry(guild_id)
    ent["ui_channel_id"] = int(channel_id)
    save_config()


def guild_monitored_list(guild_id: int):
    ent = ensure_guild_entry(guild_id)
    return ent.get("monitored", [])


def add_guild_monitored(guild_id: int, channel_id: int):
    ent = ensure_guild_entry(guild_id)
    arr = ent.setdefault("monitored", [])
    if channel_id not in arr:
        arr.append(channel_id)
        save_config()


def remove_guild_monitored(guild_id: int, channel_id: int):
    ent = ensure_guild_entry(guild_id)
    arr = ent.setdefault("monitored", [])
    if channel_id in arr:
        arr.remove(channel_id)
        save_config()


def get_guild_lock(guild_id: int):
    if guild_id not in guild_locks:
        guild_locks[guild_id] = asyncio.Lock()
    return guild_locks[guild_id]


def get_guild_remaining_msg_id(guild_id: int):
    ent = ensure_guild_entry(guild_id)
    mid = ent.get("remaining_msg_id")
    return int(mid) if mid else None


def set_guild_remaining_msg_id(guild_id: int, message_id: int | None):
    ent = ensure_guild_entry(guild_id)
    ent["remaining_msg_id"] = int(message_id) if message_id else None
    # Clear cache if message id removed or changed
    if message_id is None:
        remaining_cache.pop(str(guild_id), None)
    else:
        # initialize cache entry
        remaining_cache.setdefault(str(guild_id), {"last_str": None, "last_update": None})
    save_config()


def set_global_scan_interval(seconds: int):
    """
    Set global CHECK_INTERVAL_SECONDS and persist to config.
    Also update check_loop interval if running.
    """
    global CHECK_INTERVAL_SECONDS
    CHECK_INTERVAL_SECONDS = max(1, int(seconds))
    config["scan_interval"] = CHECK_INTERVAL_SECONDS
    save_config()
    # change running loop interval if running
    try:
        if check_loop.is_running():
            check_loop.change_interval(seconds=CHECK_INTERVAL_SECONDS)
    except Exception:
        pass


# ---------------- Utility ----------------
def format_seconds(seconds: float):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s"


def local_time_str(dt):
    if not dt:
        return "—"
    try:
        return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(dt)


def parse_channel_argument(arg: str):
    if not arg:
        return None
    arg = arg.strip()
    if arg.startswith("<#") and arg.endswith(">"):
        try:
            return int(arg[2:-1])
        except:
            return None
    if arg.isdigit():
        return int(arg)
    return None


async def _delete_message_later(channel: discord.abc.Messageable, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        m = await channel.fetch_message(message_id)
        await m.delete()
    except Exception:
        pass


async def _delete_message_obj_later(msg: discord.Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


async def _delete_message_and_clear(channel_id: int, message_id: int, delay: int, monitor_cid: int = None):
    await asyncio.sleep(delay)
    try:
        ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        m = await ch.fetch_message(message_id)
        await m.delete()
    except Exception:
        pass
    if monitor_cid:
        preserved_alerts.pop(monitor_cid, None)
        rec = monitored.get(monitor_cid)
        if rec and rec.get("alert_message_id") == message_id:
            rec["alert_message_id"] = None
            rec["alert_sent_time"] = None
            save_monitored()


# Delete original response (works for ephemeral & non-ephemeral original responses)
async def _delete_original_after(interaction: discord.Interaction, delay: int):
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except Exception:
        pass


# ---------------- Remaining-time embed builder (NO progress bar — only remaining time) ----------------
def build_remaining_embed(guild_id: int, remaining_seconds: int):
    rem = max(0, int(remaining_seconds))
    mmss = f"{rem // 60:02d}:{rem % 60:02d}"
    embed = discord.Embed(title="⏱️ Next scan countdown", color=0x3498DB, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Remaining", value=f"{mmss} ({rem}s)", inline=True)
    embed.set_footer(text="Scan interval (seconds): " + str(CHECK_INTERVAL_SECONDS))
    return embed


# ---------------- View for remaining message (persistent) ----------------
class RemainingView(discord.ui.View):
    def __init__(self, timeout: int = None):
        # persistent
        super().__init__(timeout=None)

    @discord.ui.button(label="🔁 Scan now", style=discord.ButtonStyle.primary, custom_id="remaining_scan")
    async def scan_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        """
        Manual scan triggered by a user pressing the button in the remaining-time message.
        To avoid the "Đang suy nghĩ..." spinner, we respond immediately and run the scan in background.
        """
        guild = interaction.guild
        if guild is None:
            try:
                await interaction.response.send_message("⚠️ Không thể quét (không có guild).", ephemeral=True)
            except:
                pass
            return

        # Respond immediately so the interaction doesn't time out / show thinking
        try:
            await interaction.response.send_message("🔁 Đã bắt đầu quét thủ công — kết quả sẽ được ghi vào log. Đếm ngược đã được đặt lại.", ephemeral=True, delete_after=6)
        except Exception:
            try:
                await interaction.response.send_message("🔁 Đã bắt đầu quét thủ công.", ephemeral=True)
            except:
                pass

        # schedule background task to perform scan and reset timer + remaining message
        asyncio.create_task(manual_scan_and_reset(guild))


async def manual_scan_and_reset(guild: discord.Guild):
    """
    Background task to perform a manual scan for a guild, then reset the next_check_time and update remaining message.
    """
    global next_check_time
    try:
        await perform_scan_for_guild(guild)
    except Exception as e:
        print(f"Error during manual_scan_for guild {guild.id}: {e}")
    # reset countdown
    next_check_time = datetime.now(timezone.utc) + timedelta(seconds=CHECK_INTERVAL_SECONDS)
    # ensure/update remaining message
    try:
        await ensure_remaining_message_for_guild(guild.id)
    except Exception:
        pass


# ---------------- Helpers to send messages into log channel ----------------
async def send_in_log_channel(log_ch, content=None, embed=None, view=None, persistent=False):
    """
    Send a message into log_ch.
    - If persistent True: message will not be auto-deleted (used for alerts & remaining message).
    - If persistent False: message will be scheduled for auto-delete after AUTO_DELETE_SECONDS.
    Returns message or None.
    """
    try:
        sent = await log_ch.send(content=content, embed=embed, view=view)
    except Exception as e:
        print(f"Failed to send in log channel {getattr(log_ch, 'id', None)}: {e}")
        return None

    if not persistent:
        try:
            asyncio.create_task(_delete_message_later(log_ch, sent.id, AUTO_DELETE_SECONDS))
        except Exception:
            pass
    return sent


# ---------------- Ensure single remaining message exists / update ----------------
async def ensure_remaining_message_for_guild(guild_id: int):
    """
    Ensure exactly one 'remaining-time' message exists in the configured log channel for this guild.
    If missing, create it and save its message id in config.
    If duplicates exist, keep one and delete the rest.
    Note: Do NOT pin the message (per user request).
    """
    ent = ensure_guild_entry(guild_id)
    log_ch_id = ent.get("log_channel_id")
    if not log_ch_id:
        return None

    # Try to fetch channel
    try:
        log_ch = bot.get_channel(int(log_ch_id)) or await bot.fetch_channel(int(log_ch_id))
    except Exception as e:
        print(f"Cannot access log channel {log_ch_id} for remaining timer: {e}")
        return None

    # If there's already a message id configured, check it and ensure it exists in this channel
    mid = ent.get("remaining_msg_id")
    now = datetime.now(timezone.utc)
    rem = CHECK_INTERVAL_SECONDS if next_check_time is None else max(0, int((next_check_time - now).total_seconds()))
    embed = build_remaining_embed(guild_id, rem)
    mmss = f"{max(0, rem) // 60:02d}:{max(0, rem) % 60:02d}"

    if mid:
        try:
            msg = await log_ch.fetch_message(int(mid))
            # update embed & view immediately (best-effort)
            try:
                await msg.edit(embed=embed, view=RemainingView())
                # update cache
                remaining_cache[str(guild_id)] = {"last_str": mmss, "last_update": datetime.now(timezone.utc)}
            except Exception:
                # If editing fails (maybe message deleted or moved), clear stored id and continue to find/create
                set_guild_remaining_msg_id(guild_id, None)
                raise
            # delete any other bot remaining messages in this channel (duplicates)
            try:
                async for m in log_ch.history(limit=200):
                    if m.id == msg.id:
                        continue
                    if m.author and m.author.id == bot.user.id and m.embeds:
                        e = m.embeds[0]
                        if e.title and "Next scan countdown" in e.title:
                            try:
                                await m.delete()
                            except:
                                pass
            except Exception:
                pass
            return msg.id
        except Exception:
            # stored id invalid (deleted or moved) -> clear and continue
            set_guild_remaining_msg_id(guild_id, None)

    # Search recent messages for existing remaining message(s) authored by bot
    candidates = []
    try:
        async for m in log_ch.history(limit=200):
            if m.author and m.author.id == bot.user.id and m.embeds:
                e = m.embeds[0]
                if e.title and "Next scan countdown" in e.title:
                    candidates.append(m)
    except Exception:
        candidates = []

    # If we found candidates, keep the most recent one (first found) and delete others
    if candidates:
        chosen = candidates[0]
        for c in candidates[1:]:
            try:
                await c.delete()
            except:
                pass
        # update chosen embed & view and save id
        try:
            await chosen.edit(embed=embed, view=RemainingView())
            remaining_cache[str(guild_id)] = {"last_str": mmss, "last_update": datetime.now(timezone.utc)}
        except Exception:
            pass
        set_guild_remaining_msg_id(guild_id, chosen.id)
        return chosen.id

    # No existing message found -> create one (persistent)
    try:
        sent = await send_in_log_channel(log_ch, embed=embed, view=RemainingView(), persistent=True)
        if not sent:
            return None
        set_guild_remaining_msg_id(guild_id, sent.id)
        remaining_cache[str(guild_id)] = {"last_str": mmss, "last_update": datetime.now(timezone.utc)}
        print(f"Created remaining-timer message {sent.id} in log channel {log_ch.id} for guild {guild_id}")
        return sent.id
    except Exception as e:
        print(f"Failed to create remaining-timer message in channel {log_ch_id}: {e}")
        return None


# ---------------- Background updater for remaining messages ----------------
async def update_remaining_messages_loop():
    """
    Background task: every 1 second evaluate whether to update remaining-time message(s) for guilds.
    Uses per-guild throttling to avoid hitting API rate limits.
    """
    global next_check_time
    while True:
        try:
            now = datetime.now(timezone.utc)
            if next_check_time is None:
                base_remaining = CHECK_INTERVAL_SECONDS
            else:
                rem_td = (next_check_time - now)
                base_remaining = max(0, int(rem_td.total_seconds()))

            guilds = list(config.get("guilds", {}).items())
            for gid, ent in guilds:
                try:
                    log_ch_id = ent.get("log_channel_id")
                    if not log_ch_id:
                        continue
                    # ensure channel object
                    try:
                        log_ch = bot.get_channel(int(log_ch_id)) or await bot.fetch_channel(int(log_ch_id))
                    except Exception:
                        continue

                    # Compute remaining for this guild — we assume a global next_check_time so same base_remaining applies,
                    # but keep function in case next_check_time differs in future per-guild.
                    remaining = base_remaining
                    if remaining < 0:
                        remaining = 0

                    # decide update frequency based on remaining seconds
                    if remaining > 300:
                        min_interval = 30
                    elif remaining > 60:
                        min_interval = 10
                    elif remaining > 10:
                        min_interval = 5
                    else:
                        min_interval = 1

                    key = str(gid)
                    cache = remaining_cache.get(key)
                    mmss = f"{remaining // 60:02d}:{remaining % 60:02d}"
                    now_ts = datetime.now(timezone.utc)

                    # If we have a cache entry and it's recent enough, skip
                    if cache:
                        last_update = cache.get("last_update")
                        last_str = cache.get("last_str")
                        if last_update and (now_ts - last_update).total_seconds() < min_interval:
                            # skip update due to throttle
                            continue
                        if last_str == mmss and last_update:
                            # content unchanged, but maybe enough time passed to update embed timestamp — we can skip to reduce edits
                            # Only force update if last_update older than min_interval * 2 to refresh timestamp
                            if (now_ts - last_update).total_seconds() < (min_interval * 2):
                                continue

                    # If we have a saved remaining message id, attempt to edit it; otherwise ensure it's created
                    mid = ent.get("remaining_msg_id")
                    if mid:
                        try:
                            msg = await log_ch.fetch_message(int(mid))
                            embed = build_remaining_embed(int(gid), remaining)
                            try:
                                await msg.edit(embed=embed)
                                # update cache
                                remaining_cache[key] = {"last_str": mmss, "last_update": now_ts}
                            except discord.HTTPException as e:
                                # On any HTTP error (including 429), don't spam edits; clear saved id if message removed
                                if e.status == 404:
                                    set_guild_remaining_msg_id(int(gid), None)
                                # otherwise just continue; discord.py will handle rate-limit backoff
                                continue
                        except discord.NotFound:
                            # message deleted -> clear stored id and attempt to recreate below
                            set_guild_remaining_msg_id(int(gid), None)
                            try:
                                await ensure_remaining_message_for_guild(int(gid))
                            except Exception:
                                pass
                        except Exception:
                            # Problem fetching -> try ensure to recreate if necessary
                            try:
                                await ensure_remaining_message_for_guild(int(gid))
                            except Exception:
                                pass
                    else:
                        # No stored id -> try to ensure/create
                        try:
                            await ensure_remaining_message_for_guild(int(gid))
                        except Exception:
                            pass

                except Exception:
                    # per-guild error should not stop the loop
                    continue
        except Exception as e:
            print("Error in update_remaining_messages_loop:", e)
        await asyncio.sleep(1)


# ---------------- Core scanning logic (reused by check_loop & manual scan) ----------------
async def perform_scan_for_guild(guild: discord.Guild):
    """
    Run one monitoring pass for a single guild. Used by manual scan and when check_loop runs.
    """
    now = datetime.now(timezone.utc)
    gm_list = guild_monitored_list(guild.id)
    for cid in list(gm_list):
        try:
            ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
            msgs = [m async for m in ch.history(limit=1)]
            if not msgs:
                continue
            last_msg_time = msgs[0].created_at.replace(tzinfo=timezone.utc)
            rec = monitored.get(cid)
            if rec is None:
                monitored[cid] = {
                    "log_channel": None,
                    "last_message_time": last_msg_time,
                    "alert_count": 0,
                    "alert_message_id": None,
                    "alert_sent_time": None,
                    "confirmed": False,
                    "confirmed_by": None
                }
                save_monitored()
                rec = monitored[cid]

            # reset on new message
            if rec.get("last_message_time") is None or last_msg_time != rec.get("last_message_time"):
                rec["last_message_time"] = last_msg_time
                rec["alert_count"] = 0
                rec["confirmed"] = False
                rec["confirmed_by"] = None

                # delete old alert if existed and log channel known
                if rec.get("alert_message_id"):
                    try:
                        log_ch_id = rec.get("log_channel") or get_guild_log_channel(ch.guild.id)
                        if log_ch_id:
                            log_ch = bot.get_channel(log_ch_id) or await bot.fetch_channel(log_ch_id)
                            old = await log_ch.fetch_message(rec.get("alert_message_id"))
                            try:
                                await old.delete()
                            except:
                                pass
                    except Exception:
                        pass
                    rec["alert_message_id"] = None
                    rec["alert_sent_time"] = None
                save_monitored()
                continue

            # skip confirmed
            if rec.get("confirmed"):
                continue

            diff = (now - rec["last_message_time"]).total_seconds()
            if diff > THRESHOLD_SECONDS:
                # avoid very-frequent double alerts (short window)
                if rec.get("alert_sent_time") and (now - rec["alert_sent_time"]).total_seconds() < 5:
                    continue

                rec["alert_count"] = rec.get("alert_count", 0) + 1
                log_ch_id = rec.get("log_channel") or get_guild_log_channel(ch.guild.id)
                if not log_ch_id:
                    print(f"Skipping alert for {ch.name} (no log configured).")
                    continue

                try:
                    log_ch = bot.get_channel(log_ch_id) or await bot.fetch_channel(log_ch_id)
                except Exception as e:
                    print(f"Cannot access log channel {log_ch_id} for monitor {cid}: {e}")
                    continue

                # delete old alert if exists
                if rec.get("alert_message_id"):
                    try:
                        old = await log_ch.fetch_message(rec["alert_message_id"])
                        try:
                            await old.delete()
                        except:
                            pass
                    except:
                        pass

                embed = discord.Embed(
                    title=f"👉**{ch.name}**👈 quá {THRESHOLD_SECONDS//60} phút chưa xong Mission.",
                    color=0xE74C3C,
                    timestamp=now
                )
                embed.add_field(name="Last message", value=local_time_str(rec["last_message_time"]), inline=True)
                embed.add_field(name="Delay", value=format_seconds(diff), inline=True)
                embed.add_field(name="Thông báo lần", value=str(rec["alert_count"]), inline=True)

                mention_parts = []
                if PING_EVERYONE:
                    mention_parts.append("@everyone")
                if PING_ROLE_IDS:
                    mention_parts.extend(f"<@&{rid}>" for rid in PING_ROLE_IDS)
                content = " ".join(mention_parts) if mention_parts else None
                allowed = discord.AllowedMentions(everyone=bool(PING_EVERYONE),
                                                  roles=bool(PING_ROLE_IDS),
                                                  users=False)
                view = ConfirmView(cid)
                try:
                    sent = await send_in_log_channel(log_ch, content=content, embed=embed, view=view, persistent=True)
                    if sent:
                        rec["alert_message_id"] = sent.id
                        rec["alert_sent_time"] = now
                        save_monitored()
                        print(f"Alert {rec['alert_count']} - {ch.name} -> sent to {log_ch.id}")
                except Exception as e:
                    print(f"Failed to send alert for {cid} to {log_ch_id}: {e}")
        except Exception as e:
            print(f"Error monitoring {cid} in guild {guild.id}: {e}")


# ---------------- Confirm View (alerts in log channel) ----------------
class ConfirmView(discord.ui.View):
    def __init__(self, monitor_cid: int = None, *, timeout: int = None):
        super().__init__(timeout=None)
        self.monitor_cid = monitor_cid

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success, custom_id="confirm_button")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        if not (user.guild_permissions.manage_channels or user.guild_permissions.administrator):
            try:
                await interaction.response.send_message("❌ Bạn không có quyền xác nhận.", ephemeral=True, delete_after=5)
            except:
                pass
            return

        cid = self.monitor_cid
        guild_id = None
        try:
            chobj = bot.get_channel(cid) or (await bot.fetch_channel(cid) if cid else None)
            if chobj and getattr(chobj, "guild", None):
                guild_id = chobj.guild.id
        except Exception:
            guild_id = interaction.guild.id if interaction.guild else None

        lock = get_guild_lock(guild_id) if guild_id else asyncio.Lock()

        async with lock:
            rec = monitored.get(cid) if cid else None
            preserved = None
            if rec is None:
                preserved = preserved_alerts.get(cid)
                if not preserved:
                    try:
                        await interaction.response.send_message("❌ Monitor không tồn tại (không có alert được giữ lại).", ephemeral=True, delete_after=5)
                    except:
                        pass
                    return
                if preserved.get("confirmed"):
                    try:
                        await interaction.response.send_message(f"❌ Đã được xác nhận bởi <@{preserved.get('confirmed_by')}>.", ephemeral=True, delete_after=6)
                    except:
                        pass
                    return
                preserved["confirmed"] = True
                preserved["confirmed_by"] = user.id
            else:
                if rec.get("confirmed"):
                    try:
                        prev = rec.get("confirmed_by")
                        await interaction.response.send_message(f"❌ Đã được xác nhận bởi <@{prev}>.", ephemeral=True, delete_after=6)
                    except:
                        pass
                    return
                rec["confirmed"] = True
                rec["confirmed_by"] = user.id
                save_monitored()

        try:
            for item in self.children:
                if isinstance(item, discord.ui.Button) and getattr(item, "custom_id", None) == "confirm_button":
                    item.disabled = True

            orig_msg = interaction.message
            embeds = orig_msg.embeds
            if embeds:
                e = embeds[0]
                new_e = discord.Embed.from_dict(e.to_dict())
                try:
                    new_e.add_field(name="✅ Confirmed by", value=f"{user.mention}", inline=False)
                except Exception:
                    pass
            else:
                new_e = discord.Embed(title="Confirmed", description=f"Confirmed by {user.mention}")

            await interaction.response.edit_message(embed=new_e, view=self)
        except Exception:
            try:
                await interaction.response.send_message(f"✅ Đã xác nhận monitor {cid} bởi {user.mention}", ephemeral=True, delete_after=8)
            except:
                pass


# ---------------- Remaining UI, Add/Remove/SetLog/List/MassCreate Views & Commands ----------------
# (Implementations are the same as prior but use ensure_remaining_message_for_guild to avoid duplicates)

class RemoveSelectView(discord.ui.View):
    def __init__(self, guild: discord.Guild, requester: discord.Member, *, timeout: int = None):
        super().__init__(timeout=300)
        self.guild = guild
        self.requester = requester
        self._orig_message = None
        self.selected = []
        self._build_options()

    def _build_options(self, options: list = None):
        for item in list(self.children):
            if isinstance(item, discord.ui.Select):
                try:
                    self.remove_item(item)
                except Exception:
                    pass

        gm_list = guild_monitored_list(self.guild.id)
        opts = []
        if options is None:
            for cid in gm_list:
                ch = self.guild.get_channel(cid)
                if ch:
                    kind = "voice" if isinstance(ch, discord.VoiceChannel) else "text"
                    opts.append(discord.SelectOption(label=ch.name, value=str(cid), description=f"{kind} • {cid}"))
        else:
            opts = options

        if not opts:
            self.no_options = True
            return
        self.no_options = False
        maxv = min(25, len(opts))
        self.sel = discord.ui.Select(placeholder="Chọn channel (tối đa 25) để xóa...", options=opts[:25], min_values=1, max_values=maxv)
        self.sel.callback = self._sel_cb
        self.add_item(self.sel)

    async def _sel_cb(self, interaction: discord.Interaction):
        try:
            self.selected = [int(v) for v in self.sel.values]
        except:
            self.selected = []
        try:
            await interaction.response.defer(thinking=False)
        except:
            try:
                await interaction.response.send_message("Đã chọn.", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="Chọn tất cả", style=discord.ButtonStyle.secondary, custom_id="remove_select_all")
    async def select_all_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id and not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("❌ Bạn không có quyền.", ephemeral=True, delete_after=5)
            return
        if getattr(self, "no_options", False) or not getattr(self, "sel", None):
            try:
                await interaction.response.send_message("Không có mục nào để chọn.", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass
            return
        values = [opt.value for opt in self.sel.options]
        self.selected = [int(v) for v in values]
        new_opts = [discord.SelectOption(label=o.label, value=o.value, description=o.description, default=True) for o in self.sel.options]
        self.sel.options = new_opts
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"✅ Đã chọn tất cả ({len(self.selected)})", ephemeral=True)
            else:
                await interaction.response.edit_message(view=self)
        except:
            try:
                await interaction.response.send_message(f"✅ Đã chọn tất cả ({len(self.selected)})", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="🔎 Search", style=discord.ButtonStyle.secondary, custom_id="remove_search")
    async def search_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id and not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("❌ Bạn không có quyền.", ephemeral=True, delete_after=5)
            return

        class SearchModal(discord.ui.Modal, title="Search monitored channels to remove"):
            query = discord.ui.TextInput(label="Tên channel (một phần)", required=True, max_length=100)

            def __init__(self, parent_view: "RemoveSelectView"):
                super().__init__()
                self.parent_view = parent_view

            async def on_submit(self, modal_interaction: discord.Interaction):
                q = self.query.value.strip().lower()
                if not q:
                    try:
                        await modal_interaction.response.send_message("❗ Query trống.", ephemeral=True, delete_after=6)
                    except:
                        pass
                    return
                matches = []
                gm_list = guild_monitored_list(self.parent_view.guild.id)
                for cid in gm_list:
                    ch = self.parent_view.guild.get_channel(cid)
                    if not ch:
                        continue
                    if q in ch.name.lower():
                        kind = "voice" if isinstance(ch, discord.VoiceChannel) else "text"
                        matches.append(discord.SelectOption(label=ch.name, value=str(cid), description=f"{kind} • {cid}"))
                if not matches:
                    try:
                        await modal_interaction.response.send_message("Không tìm thấy channel khớp.", ephemeral=True, delete_after=6)
                    except:
                        pass
                    return
                limited = matches[:25]

                updated_embed = discord.Embed(
                    title="Remove monitor — Search results",
                    description=f"**Kết quả:** \"{q}\" — {len(matches)} (hiển thị tối đa 25)\nChọn rồi bấm Delete.",
                    color=0x95A5A6,
                    timestamp=datetime.now(timezone.utc)
                )
                try:
                    self.parent_view._build_options(options=limited)
                    if getattr(self.parent_view, "_orig_message", None):
                        try:
                            await self.parent_view._orig_message.edit(embed=updated_embed, view=self.parent_view)
                            try:
                                await modal_interaction.response.send_message("✅ Đã cập nhật dropdown trên giao diện hiện tại.", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
                            except:
                                pass
                            return
                        except Exception:
                            pass
                    try:
                        await modal_interaction.response.edit_message(embed=updated_embed, view=self.parent_view)
                        try:
                            await modal_interaction.followup.send("✅ Đã cập nhật dropdown trên giao diện (không thể lấy message gốc).", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
                        except:
                            pass
                        return
                    except Exception:
                        pass
                    new_view = RemoveSelectView(self.parent_view.guild, self.parent_view.requester)
                    if not getattr(new_view, "no_options", False) and getattr(new_view, "sel", None):
                        new_view.sel.options = limited
                    await modal_interaction.response.send_message(embed=updated_embed, view=new_view, ephemeral=True)
                except Exception as e:
                    try:
                        await modal_interaction.response.send_message(f"Không thể cập nhật dropdown: {e}", ephemeral=True, delete_after=6)
                    except:
                        pass

        try:
            await interaction.response.send_modal(SearchModal(self))
        except Exception as e:
            try:
                await interaction.response.send_message(f"Không thể mở modal: {e}", ephemeral=True, delete_after=6)
            except:
                pass

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, custom_id="remove_ok")
    async def ok_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except:
            pass

        if interaction.user.id != self.requester.id and not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            msg = await interaction.followup.send("❌ Bạn không có quyền thực hiện thao tác này.", ephemeral=True)
            asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
            return

        if getattr(self, "no_options", False):
            msg = await interaction.followup.send("Không có channel nào đang được theo dõi trong server này.", ephemeral=True)
            asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
            return

        if not getattr(self, "selected", None):
            msg = await interaction.followup.send("❗ Hãy chọn ít nhất 1 channel trước khi bấm Delete.", ephemeral=True)
            asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
            return

        lock = get_guild_lock(self.guild.id)
        added_removed = []
        already_missing = []
        preserved = []
        async with lock:
            now = datetime.now(timezone.utc)
            for cid in self.selected:
                gm_list = guild_monitored_list(self.guild.id)
                if cid not in gm_list:
                    already_missing.append(cid)
                    continue
                remove_guild_monitored(self.guild.id, cid)
                rec = monitored.pop(cid, None)
                if rec and rec.get("alert_message_id"):
                    log_ch_id = rec.get("log_channel") or get_guild_log_channel(self.guild.id)
                    if log_ch_id:
                        try:
                            log_ch = bot.get_channel(log_ch_id) or await bot.fetch_channel(log_ch_id)
                            old = await log_ch.fetch_message(rec.get("alert_message_id"))
                            alert_time = old.created_at if getattr(old, 'created_at', None) else None
                            if alert_time and alert_time.tzinfo is None:
                                alert_time = alert_time.replace(tzinfo=timezone.utc)
                            if alert_time and (now - alert_time).total_seconds() > CHECK_INTERVAL_SECONDS:
                                preserved_alerts[cid] = {"log_channel": log_ch.id, "alert_message_id": old.id, "alert_sent_time": alert_time}
                                preserved.append(cid)
                            else:
                                try:
                                    await old.delete()
                                except:
                                    pass
                        except Exception:
                            pass
                added_removed.append(cid)
            save_monitored()

        lines = []
        if added_removed:
            lines.append("✅ Đã xóa:\n" + "\n".join(f"- <#{c}>" for c in added_removed))
        if already_missing:
            lines.append("⚠️ Đã không tồn tại (đã bị xóa trước đó):\n" + "\n".join(f"- <#{c}>" for c in already_missing))
        if preserved:
            lines.append("ℹ️ Một số alert được giữ lại vì đã quá cũ (còn trong log):\n" + "\n".join(f"- <#{c}>" for c in preserved))

        desc = "\n\n".join(lines) if lines else "Không có thay đổi."
        embed = discord.Embed(title="Remove monitors — Kết quả", description=desc, color=0xE74C3C, timestamp=datetime.now(timezone.utc))

        try:
            msg = await interaction.followup.send(embed=embed, ephemeral=True)
            asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
        except:
            try:
                msg = await interaction.followup.send(desc, ephemeral=True)
                asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
            except:
                pass

        self.selected = []

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="remove_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Đã hủy.", ephemeral=True)
            else:
                await interaction.response.edit_message(content="Đã hủy.", embed=None, view=None)
            asyncio.create_task(_delete_original_after(interaction, UI_TEMP_DELETE_SECONDS))
        except Exception:
            try:
                await interaction.response.send_message("Đã hủy.", ephemeral=True)
                asyncio.create_task(_delete_original_after(interaction, UI_TEMP_DELETE_SECONDS))
            except:
                pass
        finally:
            self.stop()


class AddSelectView(discord.ui.View):
    def __init__(self, guild: discord.Guild, requester: discord.Member, *, timeout: int = None):
        super().__init__(timeout=300)
        self.guild = guild
        self.requester = requester
        self._orig_message = None
        self.selected = []
        self._build_options()

    def _build_options(self, options: list = None):
        for item in list(self.children):
            if isinstance(item, discord.ui.Select):
                try:
                    self.remove_item(item)
                except Exception:
                    pass
        opts = []
        if options is None:
            for ch in self.guild.channels:
                if isinstance(ch, discord.CategoryChannel):
                    continue
                if getattr(ch, "is_thread", False):
                    continue
                if ch.id in guild_monitored_list(self.guild.id):
                    continue
                kind = "voice" if isinstance(ch, discord.VoiceChannel) else "text"
                opts.append(discord.SelectOption(label=ch.name, value=str(ch.id), description=f"{kind} • {ch.id}"))
        else:
            opts = options
        if not opts:
            self.no_options = True
            return
        self.no_options = False
        maxv = min(25, len(opts))
        self.sel = discord.ui.Select(placeholder="Chọn channel (tối đa 25) để add...", options=opts[:25], min_values=1, max_values=maxv)
        self.sel.callback = self._sel_cb
        self.add_item(self.sel)

    async def _sel_cb(self, interaction: discord.Interaction):
        try:
            self.selected = [int(v) for v in self.sel.values]
        except:
            self.selected = []
        try:
            await interaction.response.defer(thinking=False)
        except:
            try:
                await interaction.response.send_message("Đã chọn.", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="Chọn tất cả", style=discord.ButtonStyle.secondary, custom_id="add_select_all")
    async def select_all_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id and not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("❌ Bạn không có quyền.", ephemeral=True, delete_after=5)
            return
        if getattr(self, "no_options", False) or not getattr(self, "sel", None):
            try:
                await interaction.response.send_message("Không có mục nào để chọn.", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass
            return
        values = [opt.value for opt in self.sel.options]
        self.selected = [int(v) for v in values]
        new_opts = [discord.SelectOption(label=o.label, value=o.value, description=o.description, default=True) for o in self.sel.options]
        self.sel.options = new_opts
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"✅ Đã chọn tất cả ({len(self.selected)})", ephemeral=True)
            else:
                await interaction.response.edit_message(view=self)
        except:
            try:
                await interaction.response.send_message(f"✅ Đã chọn tất cả ({len(self.selected)})", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="🔎 Search", style=discord.ButtonStyle.secondary, custom_id="add_search")
    async def search_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id and not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("❌ Bạn không có quyền.", ephemeral=True, delete_after=5)
            return

        class SearchModal(discord.ui.Modal, title="Search channels to add"):
            query = discord.ui.TextInput(label="Tên channel (một phần)", required=True, max_length=100)

            def __init__(self, parent_view: "AddSelectView"):
                super().__init__()
                self.parent_view = parent_view

            async def on_submit(self, modal_interaction: discord.Interaction):
                q = self.query.value.strip().lower()
                if not q:
                    try:
                        await modal_interaction.response.send_message("❗ Query trống.", ephemeral=True, delete_after=6)
                    except:
                        pass
                    return
                matches = []
                for ch in self.parent_view.guild.channels:
                    if isinstance(ch, discord.CategoryChannel):
                        continue
                    if getattr(ch, "is_thread", False):
                        continue
                    if ch.id in guild_monitored_list(self.parent_view.guild.id):
                        continue
                    if q in ch.name.lower():
                        kind = "voice" if isinstance(ch, discord.VoiceChannel) else "text"
                        matches.append(discord.SelectOption(label=ch.name, value=str(ch.id), description=f"{kind} • {ch.id}"))
                if not matches:
                    try:
                        await modal_interaction.response.send_message("Không tìm thấy channel.", ephemeral=True, delete_after=6)
                    except:
                        pass
                    return
                limited = matches[:25]
                updated_embed = discord.Embed(
                    title="Add monitor — Search results",
                    description=f"**Kết quả:** \"{q}\" — {len(matches)} (hiển thị tối đa 25)\nChọn rồi bấm Add.",
                    color=0x95A5A6,
                    timestamp=datetime.now(timezone.utc)
                )
                try:
                    self.parent_view._build_options(options=limited)
                    if getattr(self.parent_view, "_orig_message", None):
                        try:
                            await self.parent_view._orig_message.edit(embed=updated_embed, view=self.parent_view)
                            try:
                                await modal_interaction.response.send_message("✅ Đã cập nhật dropdown trên giao diện hiện tại.", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
                            except:
                                pass
                            return
                        except Exception:
                            pass
                    try:
                        await modal_interaction.response.edit_message(embed=updated_embed, view=self.parent_view)
                        try:
                            await modal_interaction.followup.send("✅ Đã cập nhật dropdown trên giao diện (không thể lấy message gốc).", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
                        except:
                            pass
                        return
                    except Exception:
                        pass
                    new_view = AddSelectView(self.parent_view.guild, self.parent_view.requester)
                    if not getattr(new_view, "no_options", False) and getattr(new_view, "sel", None):
                        new_view.sel.options = limited
                    await modal_interaction.response.send_message(embed=updated_embed, view=new_view, ephemeral=True)
                except Exception as e:
                    try:
                        await modal_interaction.response.send_message(f"Không thể cập nhật dropdown: {e}", ephemeral=True, delete_after=6)
                    except:
                        pass

        try:
            await interaction.response.send_modal(SearchModal(self))
        except Exception as e:
            try:
                await interaction.response.send_message(f"Không thể mở modal: {e}", ephemeral=True, delete_after=6)
            except:
                pass

    @discord.ui.button(label="➕ Add", style=discord.ButtonStyle.success, custom_id="add_ok")
    async def ok_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except:
            pass

        if interaction.user.id != self.requester.id and not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            msg = await interaction.followup.send("❌ Bạn không có quyền thực hiện thao tác này.", ephemeral=True)
            asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
            return

        if getattr(self, "no_options", False):
            msg = await interaction.followup.send("Không còn channel nào khả dụng để thêm vào monitor.", ephemeral=True)
            asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
            return

        if not getattr(self, "selected", None):
            msg = await interaction.followup.send("❗ Hãy chọn ít nhất 1 channel trước khi bấm Add.", ephemeral=True)
            asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
            return

        lock = get_guild_lock(self.guild.id)
        added = []
        already_existed = []
        failed = []
        async with lock:
            for cid in self.selected:
                gm_list = guild_monitored_list(self.guild.id)
                if cid in gm_list:
                    already_existed.append(cid)
                    continue
                try:
                    ch = self.guild.get_channel(cid) or await bot.fetch_channel(cid)
                except Exception:
                    failed.append((cid, "Không thể truy cập channel"))
                    continue
                last_msg_time = None
                try:
                    msgs = [m async for m in ch.history(limit=1)]
                    if msgs:
                        last_msg_time = msgs[0].created_at.replace(tzinfo=timezone.utc)
                    else:
                        last_msg_time = datetime.now(timezone.utc)
                except Exception:
                    last_msg_time = datetime.now(timezone.utc)
                monitored[cid] = {
                    "log_channel": None,
                    "last_message_time": last_msg_time,
                    "alert_count": 0,
                    "alert_message_id": None,
                    "alert_sent_time": None,
                    "confirmed": False,
                    "confirmed_by": None
                }
                add_guild_monitored(self.guild.id, cid)
                added.append(cid)
            save_monitored()

        parts = []
        if added:
            parts.append("✅ Đã thêm:\n" + "\n".join(f"- <#{c}>" for c in added))
        if already_existed:
            parts.append("⚠️ Đã tồn tại (được thêm trước đó):\n" + "\n".join(f"- <#{c}>" for c in already_existed))
        if failed:
            parts.append("❌ Thêm thất bại:\n" + "\n".join(f"- {c}: {reason}" for c, reason in failed))

        desc = "\n\n".join(parts) if parts else "Không có thay đổi."
        embed = discord.Embed(title="Add monitors — Kết quả", description=desc, color=0x2ECC71, timestamp=datetime.now(timezone.utc))

        try:
            msg = await interaction.followup.send(embed=embed, ephemeral=True)
            asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
        except:
            try:
                msg = await interaction.followup.send(desc, ephemeral=True)
                asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
            except:
                pass

        self.selected = []

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="add_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Đã hủy.", ephemeral=True)
            else:
                await interaction.response.edit_message(content="Đã hủy.", embed=None, view=None)
            asyncio.create_task(_delete_original_after(interaction, UI_TEMP_DELETE_SECONDS))
        except Exception:
            try:
                await interaction.response.send_message("Đã hủy.", ephemeral=True)
                asyncio.create_task(_delete_original_after(interaction, UI_TEMP_DELETE_SECONDS))
            except:
                pass
        finally:
            self.stop()


class SetLogView(discord.ui.View):
    def __init__(self, guild: discord.Guild, requester: discord.Member, *, timeout: int = None):
        super().__init__(timeout=300)
        self.guild = guild
        self.requester = requester
        self.selected_log = None
        self._orig_message = None
        self._build_options()

    def _build_options(self, options: list = None):
        for item in list(self.children):
            if isinstance(item, discord.ui.Select):
                try:
                    self.remove_item(item)
                except Exception:
                    pass
        current_log_id = get_guild_log_channel(self.guild.id)
        opts = []
        if options is None:
            for ch in self.guild.channels:
                if isinstance(ch, discord.CategoryChannel):
                    continue
                if getattr(ch, "is_thread", False):
                    continue
                if current_log_id and ch.id == int(current_log_id):
                    continue
                kind = "voice" if isinstance(ch, discord.VoiceChannel) else "text"
                opts.append(discord.SelectOption(label=ch.name, value=str(ch.id), description=f"{kind} • {ch.id}"))
        else:
            opts = options
        if not opts:
            self.no_options = True
            return
        self.no_options = False
        self.log_select = discord.ui.Select(
            placeholder="Chọn channel làm log cho server...",
            options=opts[:25],
            min_values=1,
            max_values=1
        )
        self.log_select.callback = self.log_selected
        self.add_item(self.log_select)

    async def log_selected(self, interaction: discord.Interaction):
        try:
            self.selected_log = int(self.log_select.values[0])
        except:
            self.selected_log = None
        try:
            await interaction.response.defer(thinking=False)
        except:
            try:
                await interaction.response.send_message("Đã chọn (ack).", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="🔎 Search", style=discord.ButtonStyle.secondary, custom_id="setlog_search")
    async def search_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id and not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("❌ Bạn không có quyền.", ephemeral=True, delete_after=5)
            return
        parent_view = self
        class SearchModal(discord.ui.Modal, title="Search channels"):
            query = discord.ui.TextInput(label="Tên channel (một phần)", required=True, max_length=100)
            def __init__(self, parent_view: "SetLogView"):
                super().__init__()
                self.parent_view = parent_view
            async def on_submit(self, modal_interaction: discord.Interaction):
                q = self.query.value.strip().lower()
                if not q:
                    try:
                        await modal_interaction.response.send_message("❗ Query trống.", ephemeral=True, delete_after=6)
                    except:
                        pass
                    return
                matches = []
                cur = get_guild_log_channel(self.parent_view.guild.id)
                for ch in self.parent_view.guild.channels:
                    if isinstance(ch, discord.CategoryChannel):
                        continue
                    if getattr(ch, "is_thread", False):
                        continue
                    if cur and ch.id == cur:
                        continue
                    if q in ch.name.lower():
                        kind = "voice" if isinstance(ch, discord.VoiceChannel) else "text"
                        matches.append(discord.SelectOption(label=ch.name, value=str(ch.id), description=f"{kind} • {ch.id}"))
                if not matches:
                    try:
                        await modal_interaction.response.send_message("Không tìm thấy channel.", ephemeral=True, delete_after=6)
                    except:
                        pass
                    return
                limited = matches[:25]
                try:
                    self.parent_view._build_options(options=limited)
                    updated_embed = discord.Embed(
                        title="Set log — Search results",
                        description=f"**Kết quả:** \"{q}\" — {len(matches)} (hiển thị tối đa 25)\nChọn rồi bấm Set log.",
                        color=0x95A5A6,
                        timestamp=datetime.now(timezone.utc)
                    )
                    if getattr(self.parent_view, "_orig_message", None):
                        try:
                            await self.parent_view._orig_message.edit(embed=updated_embed, view=self.parent_view)
                            try:
                                await modal_interaction.response.send_message("✅ Đã cập nhật danh sách trên giao diện hiện tại.", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
                            except:
                                pass
                            return
                        except Exception:
                            pass
                    try:
                        await modal_interaction.response.edit_message(embed=updated_embed, view=self.parent_view)
                        try:
                            await modal_interaction.followup.send("✅ Đã cập nhật danh sách trên giao diện (không thể lấy message gốc).", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
                        except:
                            pass
                        return
                    except Exception:
                        pass
                    try:
                        await modal_interaction.response.send_message("⚠️ Đã tìm thấy kết quả nhưng không thể cập nhật giao diện (không có quyền sửa message gốc).", ephemeral=True, delete_after=8)
                    except:
                        pass
                except Exception as e:
                    try:
                        await modal_interaction.response.send_message(f"Không thể cập nhật dropdown: {e}", ephemeral=True, delete_after=6)
                    except:
                        pass
        try:
            await interaction.response.send_modal(SearchModal(parent_view))
        except Exception as e:
            try:
                await interaction.response.send_message(f"Không thể mở modal: {e}", ephemeral=True, delete_after=6)
            except:
                pass

    @discord.ui.button(label="✅ Set log", style=discord.ButtonStyle.success, custom_id="setlog_ok")
    async def ok_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except:
            pass
        if interaction.user.id != self.requester.id and not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            msg = await interaction.followup.send("❌ Bạn không có quyền.", ephemeral=True)
            asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
            return
        if getattr(self, "no_options", False):
            msg = await interaction.followup.send("Không có channel để chọn.", ephemeral=True)
            asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
            return
        if not self.selected_log:
            msg = await interaction.followup.send("❗ Hãy chọn log channel trước khi bấm Set.", ephemeral=True)
            asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
            return
        try:
            _ = self.guild.get_channel(self.selected_log) or await bot.fetch_channel(self.selected_log)
        except Exception as e:
            msg = await interaction.followup.send(f"❌ Không thể truy cập log channel đã chọn: {e}", ephemeral=True)
            asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
            return

        prev_log_id = get_guild_log_channel(self.guild.id)
        prev_remaining_mid = get_guild_remaining_msg_id(self.guild.id)

        # set the new log channel first (persist)
        set_guild_log_channel(self.guild.id, self.selected_log)

        # If previous log channel exists and is different, delete ALL messages there (per user request),
        # including the previous remaining message. Attempt bulk purge first, fallback to manual deletion.
        if prev_log_id and prev_log_id != self.selected_log:
            try:
                prev_ch = bot.get_channel(prev_log_id) or await bot.fetch_channel(prev_log_id)
                # try bulk purge (best-effort)
                try:
                    # purge will attempt to bulk-delete recent messages (requires Manage Messages)
                    await prev_ch.purge(limit=1000)
                except Exception:
                    # fallback: iterate and delete individually
                    try:
                        async for m in prev_ch.history(limit=1000):
                            try:
                                await m.delete()
                            except:
                                pass
                    except Exception:
                        pass
                # clear stored remaining message id for this guild
                try:
                    set_guild_remaining_msg_id(self.guild.id, None)
                except:
                    pass
            except Exception:
                pass

        # ensure the remaining message exists in new log channel (will reuse existing bot message if present)
        try:
            await ensure_remaining_message_for_guild(self.guild.id)
        except Exception as e:
            print(f"Error ensuring remaining message for guild {self.guild.id}: {e}")

        desc = f"✅ Đã gán log cho server: <#{self.selected_log}>.\n(Lưu ý: log là cấu hình cấp server, không gán cho từng monitor.)"
        embed = discord.Embed(title="Set log", description=desc, color=0x2ECC71, timestamp=datetime.now(timezone.utc))
        try:
            msg = await interaction.followup.send(embed=embed, ephemeral=True)
            asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
        except:
            try:
                msg = await interaction.followup.send(desc, ephemeral=True)
                asyncio.create_task(_delete_message_obj_later(msg, UI_TEMP_DELETE_SECONDS))
            except:
                pass
        self.selected_log = None

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="setlog_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Đã hủy.", ephemeral=True)
            else:
                await interaction.response.edit_message(content="Đã hủy.", embed=None, view=None)
            asyncio.create_task(_delete_original_after(interaction, UI_TEMP_DELETE_SECONDS))
        except Exception:
            try:
                await interaction.response.send_message("Đã hủy.", ephemeral=True)
                asyncio.create_task(_delete_original_after(interaction, UI_TEMP_DELETE_SECONDS))
            except:
                pass
        finally:
            self.stop()


class MassCreateModal(discord.ui.Modal, title="Create multiple channels"):
    base_name = discord.ui.TextInput(label="Base name (optional)", placeholder="<YOUR BASE NAME> <START INDEX>", required=False, max_length=100)
    count = discord.ui.TextInput(label="Count", required=True, max_length=6)
    chan_type = discord.ui.TextInput(label="Channel type", placeholder="text or voice channel", required=False, max_length=10)
    start = discord.ui.TextInput(label="Start index", placeholder="Channel's numberic start from <START INDEX>", required=False, max_length=6)
    category = discord.ui.TextInput(label="Category (mention or id)", placeholder="PASTE <CATEROGY ID>", required=False, max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        user = interaction.user
        if not (user.guild_permissions.manage_channels or user.guild_permissions.administrator):
            try:
                await interaction.response.send_message("Bạn cần quyền Manage Channels.", ephemeral=True)
                asyncio.create_task(_delete_original_after(interaction, UI_TEMP_DELETE_SECONDS))
            except:
                pass
            return
        base_name = (self.base_name.value or "").strip()
        try:
            count = int(self.count.value.strip())
        except:
            await interaction.response.send_message("Count không hợp lệ.", ephemeral=True)
            asyncio.create_task(_delete_original_after(interaction, UI_TEMP_DELETE_SECONDS))
            return
        chan_type = (self.chan_type.value or "text").strip().lower()
        try:
            start = int((self.start.value or "1").strip())
        except:
            start = 1
        padding = 0
        category_arg = (self.category.value or "").strip()
        category_id = parse_channel_argument(category_arg) if category_arg else None
        if count <= 0 or count > 500:
            await interaction.response.send_message("Count phải trong 1..500.", ephemeral=True)
            asyncio.create_task(_delete_original_after(interaction, UI_TEMP_DELETE_SECONDS))
            return
        notify_channel = interaction.channel or (interaction.guild.system_channel if interaction.guild else None)
        if notify_channel is None:
            try:
                dm = await interaction.user.create_dm()
                notify_channel = dm
            except:
                notify_channel = None
        try:
            await interaction.response.send_message("⏳ Yêu cầu được nhận, sẽ báo khi hoàn thành.", ephemeral=True)
            asyncio.create_task(_delete_original_after(interaction, UI_TEMP_DELETE_SECONDS))
        except:
            pass
        asyncio.create_task(do_masscreate(interaction.guild, notify_channel, base_name, count, chan_type, start, padding, category_id, user))


async def do_masscreate(guild, notify_channel, base_name, count, chan_type, start, padding, category_id, author):
    created, failed = [], []
    if padding <= 0:
        max_index = start + count - 1
        padding = len(str(max_index))
    category_obj = None
    if category_id:
        try:
            cat = guild.get_channel(category_id) or await bot.fetch_channel(category_id)
            if isinstance(cat, discord.CategoryChannel):
                category_obj = cat
        except:
            category_obj = None
    progress_msg = None
    try:
        if notify_channel:
            if base_name:
                start_display = f"{base_name}-{start}"
            else:
                start_display = f"{start}"
            progress_msg = await notify_channel.send(f"⏳ Bắt đầu tạo {count} channel (từ `{start_display}`)...")
    except:
        progress_msg = None
    for i in range(start, start + count):
        number_str = str(i).zfill(padding) if padding > 0 else str(i)
        if base_name:
            chan_name = f"{base_name}-{number_str}"
        else:
            chan_name = f"{number_str}"
        try:
            if chan_type.startswith('v'):
                ch = await guild.create_voice_channel(name=chan_name, category=category_obj, reason=f"masscreate by {author} ({author.id})")
            else:
                ch = await guild.create_text_channel(name=chan_name, category=category_obj, reason=f"masscreate by {author} ({author.id})")
            created.append(ch)
            await asyncio.sleep(0.6)
            if progress_msg and len(created) % 10 == 0:
                try:
                    await progress_msg.edit(content=f"⏳ Đã tạo {len(created)}/{count} channel...")
                except:
                    pass
        except discord.HTTPException:
            try:
                await asyncio.sleep(2)
                if chan_type.startswith('v'):
                    ch = await guild.create_voice_channel(name=chan_name, category=category_obj, reason=f"retry")
                else:
                    ch = await guild.create_text_channel(name=chan_name, category=category_obj, reason=f"retry")
                created.append(ch)
            except Exception as e2:
                failed.append((chan_name, str(e2)))
                await asyncio.sleep(1)
        except Exception as e:
            failed.append((chan_name, str(e)))
            await asyncio.sleep(0.5)
    summary = f"✅ Hoàn thành. Tạo được {len(created)}/{count} channel."
    if failed:
        summary += f" Thất bại: {len(failed)}. Ví dụ: {failed[:4]}"
    try:
        if progress_msg:
            try:
                await progress_msg.edit(content=summary)
            except:
                await notify_channel.send(summary, delete_after=UI_TEMP_DELETE_SECONDS)
            else:
                asyncio.create_task(_delete_message_later(progress_msg.channel, progress_msg.id, UI_TEMP_DELETE_SECONDS))
        elif notify_channel:
            await notify_channel.send(summary, delete_after=UI_TEMP_DELETE_SECONDS)
    except:
        try:
            await author.send(summary, delete_after=UI_TEMP_DELETE_SECONDS)
        except:
            pass


# ---------------- ListMonitorsView & ConfigView (unchanged, keep same behaviour) ----------------
class ListMonitorsView(discord.ui.View):
    SORT_OPTIONS = [
        ("name_asc", "Tên (A → Z)"),
        ("name_desc", "Tên (Z → A)"),
        ("lastmsg_desc", "Last message (mới → cũ)"),
        ("lastmsg_asc", "Last message (cũ → mới)"),
        ("alerts_desc", "Alert count (cao → thấp)"),
        ("confirmed_first", "Confirmed trước"),
        ("numeric_asc", "Theo số (tăng dần)"),
        ("numeric_desc", "Theo số (giảm dần)")
    ]
    DEFAULT_PAGE_SIZES = ["5", "10", "20", "100"]

    def __init__(self, guild: discord.Guild, requester: discord.Member, *, page_size: int = 10, sort: str = "name_asc", timeout: int = None):
        super().__init__(timeout=300)
        self.guild = guild
        self.requester = requester
        self.page_size = int(page_size)
        self.sort = sort
        self.page = 1
        self._rebuild_items()
        size_opts = [discord.SelectOption(label=f"{v} / trang", value=v) for v in self.DEFAULT_PAGE_SIZES]
        size_opts = [discord.SelectOption(label=o.label, value=o.value, default=(o.value == str(self.page_size))) for o in size_opts]
        self.size_select = discord.ui.Select(placeholder=f"Page size: {self.page_size}", options=size_opts, min_values=1, max_values=1)
        self.size_select.callback = self.on_size_change
        self.add_item(self.size_select)
        sort_opts = [discord.SelectOption(label=label, value=value, default=(value == self.sort)) for value, label in self.SORT_OPTIONS]
        self.sort_select = discord.ui.Select(placeholder="Sắp xếp", options=sort_opts, min_values=1, max_values=1)
        self.sort_select.callback = self.on_sort_change
        self.add_item(self.sort_select)

    def _rebuild_items(self):
        g = self.guild
        items = []
        gm_list = guild_monitored_list(g.id)
        for cid in gm_list:
            try:
                ch = g.get_channel(cid)
            except:
                ch = None
            rec = monitored.get(cid)
            items.append((cid, ch, rec))
        self.items = items
        self._apply_sort()

    def _apply_sort(self):
        def key_name(t):
            cid, ch, rec = t
            return (ch.name.lower() if ch else str(cid))
        def key_lastmsg(t):
            cid, ch, rec = t
            dt = rec.get("last_message_time") if rec else None
            return dt or datetime(1970, 1, 1, tzinfo=timezone.utc)
        def key_alerts(t):
            cid, ch, rec = t
            return rec.get("alert_count", 0) if rec else 0
        def key_confirmed_first(t):
            cid, ch, rec = t
            return not (rec and rec.get("confirmed"))
        def extract_first_int(name: str):
            if not name:
                return None
            m = re.search(r"\d+", name)
            if not m:
                return None
            try:
                return int(m.group(0))
            except:
                return None
        if self.sort == "name_asc":
            self.items.sort(key=key_name)
        elif self.sort == "name_desc":
            self.items.sort(key=key_name, reverse=True)
        elif self.sort == "lastmsg_desc":
            self.items.sort(key=key_lastmsg, reverse=True)
        elif self.sort == "lastmsg_asc":
            self.items.sort(key=key_lastmsg)
        elif self.sort == "alerts_desc":
            self.items.sort(key=key_alerts, reverse=True)
        elif self.sort == "confirmed_first":
            self.items.sort(key=key_confirmed_first)
        elif self.sort in ("numeric_asc", "numeric_desc"):
            def numeric_key(t):
                cid, ch, rec = t
                name = ch.name if ch else str(cid)
                num = extract_first_int(name)
                has_num = 0 if num is not None else 1
                return (has_num, num if num is not None else 0, name.lower())
            reverse = (self.sort == "numeric_desc")
            self.items.sort(key=numeric_key, reverse=reverse)
        else:
            self.items.sort(key=key_name)

    def total_pages(self):
        n = len(self.items)
        per = max(1, int(self.page_size))
        return max(1, (n + per - 1) // per)

    def current_page_items(self):
        per = int(self.page_size)
        start = (self.page - 1) * per
        return self.items[start:start + per]

    async def on_size_change(self, interaction: discord.Interaction):
        try:
            new_size = int(self.size_select.values[0])
            self.page_size = new_size
        except:
            self.page_size = 10
        self.page = 1
        new_opts = []
        for v in self.DEFAULT_PAGE_SIZES:
            lbl = f"{v} / trang"
            new_opts.append(discord.SelectOption(label=lbl, value=v, default=(v == str(self.page_size))))
        self.size_select.options = new_opts
        self.size_select.placeholder = f"Page size: {self.page_size}"
        self._rebuild_items()
        try:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        except:
            try:
                await interaction.response.send_message("Đã cập nhật kích thước trang.", ephemeral=True)
            except:
                pass

    async def on_sort_change(self, interaction: discord.Interaction):
        try:
            self.sort = self.sort_select.values[0]
        except:
            self.sort = "name_asc"
        self.page = 1
        new_sort_opts = []
        for value, label in self.SORT_OPTIONS:
            new_sort_opts.append(discord.SelectOption(label=label, value=value, default=(value == self.sort)))
        self.sort_select.options = new_sort_opts
        chosen_label = dict(self.SORT_OPTIONS).get(self.sort, self.sort)
        self.sort_select.placeholder = f"Sắp xếp: {chosen_label}"
        self._apply_sort()
        try:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        except:
            try:
                await interaction.response.send_message("Đã cập nhật sắp xếp.", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="⬅️ Prev", style=discord.ButtonStyle.secondary, custom_id="list_prev")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 1:
            self.page -= 1
            try:
                await interaction.response.edit_message(embed=self.build_embed(), view=self)
            except:
                try:
                    await interaction.response.send_message("Đã chuyển trang.", ephemeral=True)
                except:
                    pass
        else:
            try:
                await interaction.response.send_message("Đã ở trang đầu.", ephemeral=True, delete_after=5)
            except:
                pass

    @discord.ui.button(label="Next ➡️", style=discord.ButtonStyle.secondary, custom_id="list_next")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.total_pages():
            self.page += 1
            try:
                await interaction.response.edit_message(embed=self.build_embed(), view=self)
            except:
                try:
                    await interaction.response.send_message("Đã chuyển trang.", ephemeral=True)
                except:
                    pass
        else:
            try:
                await interaction.response.send_message("Đã ở trang cuối.", ephemeral=True, delete_after=5)
            except:
                pass

    @discord.ui.button(label="⬅️ Back", style=discord.ButtonStyle.secondary, custom_id="list_back")
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Đã đóng danh sách.", ephemeral=True)
            else:
                await interaction.response.edit_message(content="Đã đóng danh sách.", embed=None, view=None)
            asyncio.create_task(_delete_original_after(interaction, UI_TEMP_DELETE_SECONDS))
        except Exception:
            try:
                await interaction.response.send_message("Đã đóng danh sách.", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass
        finally:
            self.stop()

    def build_embed(self):
        total = len(self.items)
        pages = self.total_pages()
        cur_items = self.current_page_items()
        lines = []
        for cid, ch, rec in cur_items:
            name = ch.name if ch else f"(deleted channel {cid})"
            lid_val = rec.get("log_channel") if rec else None
            if not lid_val:
                lid_val = get_guild_log_channel(self.guild.id)
            lid_display = f"<#{lid_val}>" if lid_val else "—"
            cnt = rec.get("alert_count", 0) if rec else 0
            confirmed = "✅" if rec and rec.get("confirmed") else ""
            last_msg = local_time_str(rec.get("last_message_time")) if rec and rec.get("last_message_time") else "—"
            lines.append(f"- <#{cid}> **{name}** {confirmed}\n  last: {last_msg} • alerts: {cnt} • log: {lid_display}")
        sort_label = dict(self.SORT_OPTIONS).get(self.sort, self.sort)
        desc = f"**Monitored channels:** {total} • Trang {self.page}/{pages} • Sắp xếp: {sort_label} • Page size: {self.page_size}\n\n" + ("\n\n".join(lines) if lines else "_Không có mục nào trên trang này._")
        embed = discord.Embed(title="📋 Danh sách monitor (phân trang)", description=desc, color=0x3498DB, timestamp=datetime.now(timezone.utc))
        return embed


def generate_main_embed():
    embed = discord.Embed(
        title="Cấu hình <Check messages> (Beta)",
        description="Bảng điều khiển monitor tương tác — quản lý các kênh đang được theo dõi, thiết lập kênh ghi log — tạo hàng loạt kênh.",
        color=0x7B61FF,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Functions (Chức năng)", value="• **Add monitor** — Thêm channel để quản lí\n• **Delete monitor** — Xoá các channel đã thêm\n• **Set log** — Thiết lập log-channel\n• **Create channels** — Tạo channel theo tên (custom) + số thứ tự tăng dần", inline=False)
    if os.path.exists(MONITORED_IMAGE_PATH):
        embed.set_image(url="attachment://hydra.png")
    return embed


class ConfigView(discord.ui.View):
    def __init__(self, *, timeout: int = None):
        super().__init__(timeout=None)

    @discord.ui.button(label="📜List", style=discord.ButtonStyle.primary, custom_id="cm_list")
    async def list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Bạn cần quyền Manage Channels để sử dụng.", ephemeral=True, delete_after=5)
            return
        gm = guild_monitored_list(interaction.guild.id)
        if not gm:
            try:
                await interaction.response.send_message("Danh sách monitor trống.", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass
            return
        view = ListMonitorsView(interaction.guild, interaction.user, page_size=10, sort="name_asc")
        try:
            await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
        except:
            try:
                await interaction.response.send_message("Không thể mở danh sách (lỗi).", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass

    @discord.ui.button(label="➕Add", style=discord.ButtonStyle.success, custom_id="cm_add")
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Bạn cần quyền Manage Channels để sử dụng.", ephemeral=True, delete_after=5)
            return
        view = AddSelectView(interaction.guild, interaction.user)
        if getattr(view, "no_options", False):
            try:
                await interaction.response.send_message("Không còn channel nào để thêm vào monitor (hoặc giới hạn 25 tùy chọn đã đầy).", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass
            return
        embed = discord.Embed(title="➕ Thêm monitor", description="Chọn các channel để thêm vào monitor (private với bạn), sau đó bấm **Add** hoặc **Cancel**.", color=0x2ECC71, timestamp=datetime.now(timezone.utc))
        try:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            try:
                orig_msg = await interaction.original_response()
                view._orig_message = orig_msg
            except Exception:
                view._orig_message = None
        except:
            try:
                await interaction.response.send_message("Không thể mở giao diện thêm (lỗi)", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass

    @discord.ui.button(label="🗑️Remove", style=discord.ButtonStyle.danger, custom_id="cm_remove")
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Bạn cần quyền Manage Channels để sử dụng.", ephemeral=True, delete_after=5)
            return
        view = RemoveSelectView(interaction.guild, interaction.user)
        if getattr(view, "no_options", False):
            try:
                await interaction.response.send_message("Không có channel nào đang được theo dõi để xóa.", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass
            return
        embed = discord.Embed(title="🗑️ Xóa monitor", description="Chọn các channel cần xóa khỏi monitor (private với bạn), sau đó bấm **Delete** hoặc **Cancel**.", color=0xE74C3C, timestamp=datetime.now(timezone.utc))
        try:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            try:
                orig_msg = await interaction.original_response()
                view._orig_message = orig_msg
            except Exception:
                view._orig_message = None
        except:
            try:
                await interaction.response.send_message("Không thể mở giao diện xóa (lỗi)", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass

    @discord.ui.button(label="⚙️Set log", style=discord.ButtonStyle.secondary, custom_id="cm_setlog")
    async def setlog_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Bạn cần quyền Manage Channels để sử dụng lệnh này.", ephemeral=True, delete_after=5)
            return
        view = SetLogView(interaction.guild, interaction.user)
        if getattr(view, "no_options", False):
            try:
                await interaction.response.send_message("Không tìm thấy channel nào trong server để chọn làm log.", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass
            return
        cur = get_guild_log_channel(interaction.guild.id)
        desc = "Chọn channel làm log cho server (private với bạn). Dùng nút 🔎 Search để lọc theo tên."
        if cur:
            desc = f"**Current log:** <#{cur}>\n\n" + desc
        embed = discord.Embed(title="⚙️ Set log", description=desc, color=0x95A5A6, timestamp=datetime.now(timezone.utc))
        try:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            try:
                orig_msg = await interaction.original_response()
                view._orig_message = orig_msg
            except Exception:
                view._orig_message = None
        except:
            try:
                await interaction.response.send_message("Không thể mở giao diện Set log (lỗi).", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass

    @discord.ui.button(label="🛠️Create channels", style=discord.ButtonStyle.primary, custom_id="cm_masscreate")
    async def masscreate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Bạn cần quyền Manage Channels để sử dụng.", ephemeral=True, delete_after=5)
            return
        try:
            await interaction.response.send_modal(MassCreateModal())
        except:
            try:
                await interaction.response.send_message("Không thể mở modal Create Channels.", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass

    @discord.ui.button(label="❌Close", style=discord.ButtonStyle.secondary, custom_id="cm_close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.response.is_done():
                await interaction.followup.send("UI chính vẫn ở kênh gốc; nếu muốn tắt giao diện cho bạn, hãy đóng cửa sổ ephemeral.", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
            else:
                await interaction.response.edit_message(content="UI chính vẫn ở kênh gốc; nếu muốn tắt giao diện cho bạn, hãy đóng cửa sổ ephemeral.", embed=None, view=None)
            asyncio.create_task(_delete_original_after(interaction, UI_TEMP_DELETE_SECONDS))
        except Exception:
            try:
                await interaction.response.send_message("UI chính vẫn ở kênh gốc; nếu muốn tắt giao diện cho bạn, hãy đóng cửa sổ ephemeral.", ephemeral=True, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass


# ---------------- Post UI (public) ----------------
async def post_ui_to_channel(channel_id: int, *, guild: discord.Guild = None):
    try:
        ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    except Exception as e:
        print(f"Cannot access channel {channel_id} to post UI: {e}")
        return False

    embed = generate_main_embed()
    file = None
    try:
        if os.path.exists(MONITORED_IMAGE_PATH):
            file = discord.File(MONITORED_IMAGE_PATH, filename="hydra.png")
            embed.set_image(url="attachment://hydra.png")
    except Exception:
        file = None

    try:
        if file:
            await ch.send(embed=embed, view=ConfigView(), file=file)
        else:
            await ch.send(embed=embed, view=ConfigView())
        return True
    except Exception as e:
        print(f"Failed to send UI to channel {channel_id}: {e}")
        return False


# ---------------- on_ready & monitoring loop ----------------
@bot.event
async def on_ready():
    global next_check_time, timer_task
    print(f"Bot ready: {bot.user} (id: {bot.user.id})")
    load_config()
    load_monitored()

    # init last_message_time for monitored if missing
    for cid in list(monitored.keys()):
        try:
            ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
            msgs = [m async for m in ch.history(limit=1)]
            if msgs:
                monitored[cid]["last_message_time"] = msgs[0].created_at.replace(tzinfo=timezone.utc)
            else:
                monitored[cid]["last_message_time"] = datetime.now(timezone.utc)
        except Exception as e:
            print(f"Init: cannot access channel {cid}: {e}")
            monitored[cid]["last_message_time"] = datetime.now(timezone.utc)

    # Register persistent views
    try:
        bot.add_view(ConfigView())
    except Exception:
        pass
    try:
        bot.add_view(ConfirmView(None))
    except Exception:
        pass
    try:
        bot.add_view(RemainingView())
    except Exception:
        pass

    # Ensure remaining-message exists for configured guilds
    for gid, ent in config.get("guilds", {}).items():
        try:
            gid_int = int(gid)
            if ent.get("log_channel_id"):
                try:
                    await ensure_remaining_message_for_guild(gid_int)
                except Exception as e:
                    print(f"Error ensuring remaining message for guild {gid}: {e}")
        except Exception:
            continue

    # start the remaining-message updater background task if not running
    if timer_task is None or timer_task.done():
        timer_task = asyncio.create_task(update_remaining_messages_loop())

    try:
        if BOT_GUILD_ID:
            guild_obj = discord.Object(id=int(BOT_GUILD_ID))
            await bot.tree.sync(guild=guild_obj)
            print(f"Synced application commands to guild {BOT_GUILD_ID}.")
        else:
            await bot.tree.sync()
            print("Synced application commands (global).")
    except Exception as e:
        print("Failed to sync app commands:", e)

    # set next_check_time to now + interval so countdown begins immediately
    next_check_time = datetime.now(timezone.utc) + timedelta(seconds=CHECK_INTERVAL_SECONDS)

    # start the periodic scanner (uses configured interval)
    try:
        if not check_loop.is_running():
            check_loop.start()
    except Exception as e:
        print("Failed to start check_loop:", e)


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def check_loop():
    """
    Global periodic scanner. It sets next_check_time at the top of the run so the remaining-time updater can count down.
    """
    global next_check_time
    # set next run time at start (so update loop sees correct remaining immediately)
    next_check_time = datetime.now(timezone.utc) + timedelta(seconds=CHECK_INTERVAL_SECONDS)

    # perform scan per guild
    guilds = list(config.get("guilds", {}).keys())
    for gid in guilds:
        try:
            gid_int = int(gid)
            try:
                guild = bot.get_guild(gid_int) or await bot.fetch_guild(gid_int)
            except Exception:
                continue
            await perform_scan_for_guild(guild)
        except Exception as e:
            print(f"Error running scan for guild {gid}: {e}")


# ---------------- Management commands (kept simple) ----------------
@bot.group(name="monitor", invoke_without_command=True)
@commands.has_guild_permissions(manage_channels=True)
async def monitor_group(ctx):
    await ctx.reply("Commands: `!monitor add <#chan|id> [#log|id]`, `!monitor remove <#chan|id>`, `!monitor setlog <#chan|id> <#log|id>`, `!monitor list`", mention_author=False)


@bot.command(name="masscreate")
@commands.has_guild_permissions(manage_channels=True)
async def masscreate(ctx, base_name: str, count: int, chan_type: str = "text", start: int = 1, padding: int = 0, category: str = None):
    # same as earlier implementation
    if base_name == "-":
        base_name = ""
    base_name = (base_name or "").strip()
    if count <= 0:
        await ctx.reply("❌ count phải là số dương.", mention_author=False)
        return
    if count > 500:
        await ctx.reply("❌ count quá lớn (giới hạn 500).", mention_author=False)
        return
    guild = ctx.guild
    if guild is None:
        await ctx.reply("❌ Lệnh chỉ dùng trong server (guild).", mention_author=False)
        return
    current_channels = len(guild.channels)
    if current_channels + count > 500:
        await ctx.reply(f"❌ Không thể tạo {count} channel — server hiện có {current_channels} channel; giới hạn ~500.", mention_author=False)
        return
    chan_type = (chan_type or "text").lower()
    is_voice = chan_type.startswith("v")
    try:
        start = int(start)
    except:
        start = 1
    try:
        padding = int(padding)
    except:
        padding = 0
    category_obj = None
    if category:
        cid = parse_channel_argument(category)
        if cid is None:
            await ctx.reply("❌ category không hợp lệ (hãy dùng <#id> hoặc id).", mention_author=False)
            return
        try:
            cat = guild.get_channel(cid) or await bot.fetch_channel(cid)
            if not isinstance(cat, discord.CategoryChannel):
                await ctx.reply("❌ Channel được cung cấp không phải Category.", mention_author=False)
                return
            category_obj = cat
        except Exception as e:
            await ctx.reply(f"❌ Không thể truy cập category: {e}", mention_author=False)
            return
    if padding <= 0:
        max_index = start + count - 1
        padding = len(str(max_index))
    created = []
    failed = []
    if base_name:
        start_display = f"{base_name}-{start}"
    else:
        start_display = f"{start}"
    await ctx.reply(f"⏳ Bắt đầu tạo {count} {'voice' if is_voice else 'text'} channel từ `{start_display}` ... (padding={padding})", mention_author=False, delete_after=UI_TEMP_DELETE_SECONDS)
    for i in range(start, start + count):
        number_str = str(i).zfill(padding) if padding > 0 else str(i)
        if base_name:
            chan_name = f"{base_name}-{number_str}"
        else:
            chan_name = f"{number_str}"
        try:
            if is_voice:
                ch = await guild.create_voice_channel(name=chan_name, category=category_obj, reason=f"masscreate by {ctx.author} ({ctx.author.id})")
            else:
                ch = await guild.create_text_channel(name=chan_name, category=category_obj, reason=f"masscreate by {ctx.author} ({ctx.author.id})")
            created.append(ch)
            await asyncio.sleep(0.6)
        except discord.HTTPException:
            try:
                await asyncio.sleep(2)
                if is_voice:
                    ch = await guild.create_voice_channel(name=chan_name, category=category_obj, reason=f"retry masscreate by {ctx.author}")
                else:
                    ch = await guild.create_text_channel(name=chan_name, category=category_obj, reason=f"retry masscreate by {ctx.author}")
                created.append(ch)
            except Exception as e2:
                failed.append((chan_name, str(e2)))
                await asyncio.sleep(1)
        except Exception as e:
            failed.append((chan_name, str(e)))
            await asyncio.sleep(0.5)
    summary = f"✅ Hoàn thành. Tạo được {len(created)}/{count} channel."
    if failed:
        summary += f" Thất bại: {len(failed)}.\nCác lỗi mẫu: {failed[:5]}"
    await ctx.reply(summary, mention_author=False, delete_after=UI_TEMP_DELETE_SECONDS)


# ---------------- Slash commands: cmconfig, cmsetup (unchanged) & /st ----------------
@bot.tree.command(name="cmconfig", description="Interactive monitor configuration")
async def cmconfig(interaction: discord.Interaction):
    if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
        await interaction.response.send_message("Bạn cần quyền Manage Channels để sử dụng lệnh này.", ephemeral=True, delete_after=5)
        return
    embed = generate_main_embed()
    try:
        if os.path.exists(MONITORED_IMAGE_PATH):
            file = discord.File(MONITORED_IMAGE_PATH, filename="hydra.png")
            await interaction.response.send_message(embed=embed, view=ConfigView(), file=file)
        else:
            await interaction.response.send_message(embed=embed, view=ConfigView())
    except Exception:
        try:
            await interaction.response.send_message("Chọn hành động cấu hình:", ephemeral=True)
        except:
            pass


@bot.tree.command(name="cmsetup", description="Set channel where the interactive monitor UI will be posted")
async def cmsetup(interaction: discord.Interaction, channel: str):
    if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
        await interaction.response.send_message("Bạn cần quyền Manage Channels để sử dụng.", ephemeral=True, delete_after=5)
        return
    cid = parse_channel_argument(channel)
    if cid is None:
        await interaction.response.send_message("❌ Đầu vào không hợp lệ. Dùng <#id> hoặc id.", ephemeral=True, delete_after=6)
        return
    try:
        _ = bot.get_channel(cid) or await bot.fetch_channel(cid)
    except Exception as e:
        await interaction.response.send_message(f"❌ Không thể truy cập channel: {e}", ephemeral=True, delete_after=6)
        return
    set_guild_ui_channel(interaction.guild.id, cid)
    posted = await post_ui_to_channel(cid, guild=interaction.guild)
    if posted:
        await interaction.response.send_message(f"✅ Đã thiết lập channel giao diện: <#{cid}> và đăng giao diện ở đó.", ephemeral=True, delete_after=8)
    else:
        await interaction.response.send_message(f"⚠️ Đã lưu <#{cid}> làm channel giao diện nhưng không thể đăng (kiểm tra quyền).", ephemeral=True, delete_after=8)


@bot.tree.command(name="st", description="/st <seconds> — set global scan interval in seconds and restart countdown")
async def st_command(interaction: discord.Interaction, seconds: int):
    """
    Sets the global scan interval (CHECK_INTERVAL_SECONDS) in seconds.
    Requires Manage Channels permission.
    Behavior:
    - Updates global scan interval and persists it.
    - Resets countdown (next_check_time) so the next run occurs after the new interval.
    - Updates remaining-time messages immediately.
    - Runs an immediate scan and keeps schedule.
    """
    if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
        await interaction.response.send_message("Bạn cần quyền Manage Channels để sử dụng lệnh này.", ephemeral=True, delete_after=6)
        return
    try:
        seconds = int(seconds)
    except:
        await interaction.response.send_message("Giá trị seconds không hợp lệ.", ephemeral=True, delete_after=6)
        return
    if seconds < 1:
        await interaction.response.send_message("Giá trị seconds phải lớn hơn 0.", ephemeral=True, delete_after=6)
        return

    # set global interval and persist (also changes running task interval)
    set_global_scan_interval(seconds)

    # reset next_check_time and restart countdown
    global next_check_time
    next_check_time = datetime.now(timezone.utc) + timedelta(seconds=CHECK_INTERVAL_SECONDS)

    # update remaining messages immediately (best-effort)
    for gid, ent in config.get("guilds", {}).items():
        try:
            await ensure_remaining_message_for_guild(int(gid))
        except Exception:
            pass

    # run a scan immediately (like "start scanning again")
    try:
        for gid, ent in config.get("guilds", {}).items():
            try:
                gobj = bot.get_guild(int(gid)) or await bot.fetch_guild(int(gid))
                # run each scan in background so /st doesn't hang long
                asyncio.create_task(perform_scan_for_guild(gobj))
            except Exception:
                pass
    except Exception as e:
        print("Error running immediate scan after /st:", e)

    await interaction.response.send_message(f"✅ Đã đặt thời gian quét: {CHECK_INTERVAL_SECONDS}s và đặt lại đếm ngược; quét ngay lập tức.", ephemeral=True, delete_after=6)


# ---------------- Run ----------------
if __name__ == "__main__":
    load_config()
    load_monitored()
    if not TOKEN:
        print("ERROR: BOT TOKEN chưa cấu hình. Set DISCORD_TOKEN environment variable.")
    else:
        bot.run(TOKEN)
