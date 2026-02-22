# file: bot.py
import os
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import asyncio
import discord
from discord.ext import tasks, commands

# ------------------ CONFIGURATION ----------------
# (Nếu bạn muốn dùng env var, sửa TOKEN = os.getenv("BOT_TOKEN"))
TOKEN = os.getenv("BOT_TOKEN")
BOT_GUILD_ID = os.getenv("BOT_GUILD_ID")  # e.g. "123456789012345678"
MONITORED_FILE = "monitored.json"
CONFIG_FILE = "config.json"

MONITORED_IMAGE_PATH = "/mnt/data/93b3f5bc-2247-4f67-a02f-7eb4209abc2c.png"  # optional header image

# fallback if guild has no log configured
DEFAULT_LOG_CHANNEL_ID = 1472491858096820277

THRESHOLD_SECONDS = 300
CHECK_INTERVAL_SECONDS = 180
AUTO_DELETE_SECONDS = 300  # 5 minutes (alerts auto-delete)
UI_TEMP_DELETE_SECONDS = 10  # how long the temporary result message remains after OK/Cancel before auto-delete
LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# MENTION CONFIG
PING_EVERYONE = True
PING_ROLE_IDS = []
# ---------------------------------------------------

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# monitored: mapping channel_id (int) -> record dict (same shape as before)
monitored = {}
# config: persisted server-level settings; see load_config/save_config
config = {}
# preserved alerts when a monitor was removed but we want the alert message to remain
preserved_alerts = {}

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
        print("Saved monitored config.")
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
            print("Loaded monitored from file.")
            return
        except Exception as e:
            print("Failed to load monitored.json, will start empty. Error:", e)

    # start with empty (no default monitored)
    monitored = {}
    save_monitored()


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        print("Saved config.")
    except Exception as e:
        print("Error saving config:", e)


def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # normalize schema: ensure "guilds" exists
            if isinstance(cfg, dict):
                if "guilds" not in cfg:
                    # legacy: maybe single ui_channel_id at top-level
                    guilds = {}
                    # if old style contained ui_channel_id at top-level keep it as global fallback
                    ui = cfg.get("ui_channel_id")
                    config = {"ui_channel_id": ui, "guilds": guilds}
                else:
                    config = cfg
            else:
                config = {"ui_channel_id": None, "guilds": {}}
            print("Loaded config.")
            return
        except Exception as e:
            print("Failed to load config.json, using defaults:", e)
    # default
    config = {"ui_channel_id": None, "guilds": {}}
    save_config()


# Guild-level helpers for config


def ensure_guild_entry(guild_id: int):
    """Ensure config has an entry for this guild and return it (dict)."""
    gid = str(guild_id)
    if "guilds" not in config:
        config["guilds"] = {}
    if gid not in config["guilds"]:
        config["guilds"][gid] = {"log_channel_id": None, "ui_channel_id": None, "monitored": []}
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


# ---------------- Utility ----------------


def format_seconds(seconds: float):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s"


def local_time_str(dt):
    return dt.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y-%m-%d %H:%M:%S")


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
    """Helper: delete a message after delay seconds (best-effort)."""
    await asyncio.sleep(delay)
    try:
        m = await channel.fetch_message(message_id)
        await m.delete()
    except Exception:
        # ignore errors: message may be already deleted or no perms
        pass


async def _delete_message_and_clear(channel_id: int, message_id: int, delay: int, monitor_cid: int = None):
    await asyncio.sleep(delay)
    try:
        ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        m = await ch.fetch_message(message_id)
        await m.delete()
    except Exception:
        pass
    # clear preserved or monitored tracking
    if monitor_cid:
        if monitor_cid in preserved_alerts:
            preserved_alerts.pop(monitor_cid, None)
        rec = monitored.get(monitor_cid)
        if rec and rec.get("alert_message_id") == message_id:
            rec["alert_message_id"] = None
            rec["alert_sent_time"] = None
            save_monitored()


# ---------------- Confirm Button View (alerts) ----------------
class ConfirmView(discord.ui.View):
    def __init__(self, monitor_cid: int, *, timeout: int = None):
        # set timeout=None so the view doesn't auto-timeout; Discord will keep it until bot restarts
        super().__init__(timeout=None)
        self.monitor_cid = monitor_cid

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success, custom_id="confirm_button")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        # permission check: require Manage Channels or Administrator
        if not (user.guild_permissions.manage_channels or user.guild_permissions.administrator):
            # send public small reply and auto-delete (so everyone sees that permission missing briefly)
            try:
                await interaction.response.send_message("❌ Bạn không có quyền xác nhận (Manage Channels required).", delete_after=5)
            except:
                pass
            return

        cid = self.monitor_cid
        rec = monitored.get(cid)
        now = datetime.now(timezone.utc)

        # if monitor was removed earlier, check preserved_alerts
        preserved = None
        if rec is None:
            preserved = preserved_alerts.get(cid)
            if not preserved:
                try:
                    await interaction.response.send_message("❌ Monitor không tồn tại (và không có alert được giữ lại).", delete_after=5)
                except:
                    pass
                return

        # set confirmed on monitored entry if present
        if rec:
            rec["confirmed"] = True
            rec["confirmed_by"] = user.id
            save_monitored()

        # edit the alert message (interaction.message) to show confirmed and disable buttons
        try:
            orig_msg = interaction.message
            embeds = orig_msg.embeds
            if embeds:
                e = embeds[0]
                new_e = discord.Embed.from_dict(e.to_dict())
                new_e.add_field(name="✅ Confirmed by", value=f"{user.mention}", inline=False)
            else:
                new_e = discord.Embed(title="Confirmed", description=f"Confirmed by {user.mention}")

            for child in self.children:
                child.disabled = True

            await interaction.response.edit_message(embed=new_e, view=self)
        except Exception:
            # As fallback, send a public short message then auto-delete
            try:
                await interaction.response.send_message(f"✅ Đã xác nhận monitor {cid} bởi {user.mention}", delete_after=8)
            except:
                pass

        # If this alert was preserved (monitor removed while alert older than threshold), schedule deletion after 60s
        try:
            if preserved:
                alert_sent_time = preserved.get("alert_sent_time")
                if isinstance(alert_sent_time, str):
                    alert_sent_time = from_iso(alert_sent_time)
                if alert_sent_time and (now - alert_sent_time).total_seconds() > THRESHOLD_SECONDS:
                    # schedule delete after 60s
                    asyncio.create_task(_delete_message_and_clear(preserved.get("log_channel"), preserved.get("alert_message_id"), 60, monitor_cid=cid))
                    return
        except Exception:
            pass

        # If monitored exists and the rec.alert_sent_time is older than threshold, schedule deletion as well
        try:
            if rec:
                ast = rec.get("alert_sent_time")
                if ast and (now - ast).total_seconds() > THRESHOLD_SECONDS:
                    # schedule delete after 60s
                    log_ch_id = rec.get("log_channel") or DEFAULT_LOG_CHANNEL_ID
                    asyncio.create_task(_delete_message_and_clear(log_ch_id, rec.get("alert_message_id"), 60, monitor_cid=cid))
        except Exception:
            pass


# ---------------- Hydra-style UI: Add / Remove selection views ----------------
class RemoveSelectView(discord.ui.View):
    def __init__(self, guild: discord.Guild, requester: discord.Member, *, timeout: int = None):
        super().__init__(timeout=None)
        self.guild = guild
        self.requester = requester
        # build options from monitored keys that exist in this guild (use config.guilds monitored list)
        opts = []
        gm_list = guild_monitored_list(guild.id)
        for cid in gm_list:
            ch = guild.get_channel(cid)
            if ch:
                kind = "voice" if isinstance(ch, discord.VoiceChannel) else "text"
                opts.append(discord.SelectOption(label=ch.name, value=str(cid), description=f"{kind} • {cid}"))
        if not opts:
            self.no_options = True
            return
        self.no_options = False
        opts = opts[:25]  # discord limit
        sel = discord.ui.Select(placeholder="Chọn channel (tối đa 25) để xóa...", options=opts, min_values=1, max_values=len(opts))

        async def sel_cb(interaction: discord.Interaction):
            try:
                self.selected = [int(v) for v in sel.values]
            except:
                self.selected = []
            # quick acknowledge
            try:
                await interaction.response.defer()
            except:
                pass

        sel.callback = sel_cb
        self.add_item(sel)
        self.selected = []

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, custom_id="remove_ok")
    async def ok_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # permission check: requester or manage_channels/admin
        if interaction.user.id != self.requester.id and not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("❌ Bạn không có quyền thực hiện thao tác này.", delete_after=5)
            return

        if getattr(self, "no_options", False):
            await interaction.response.send_message("Không có channel nào đang được theo dõi trong server này.", delete_after=UI_TEMP_DELETE_SECONDS)
            return

        if not getattr(self, "selected", None):
            await interaction.response.send_message("❗ Hãy chọn ít nhất 1 channel trước khi bấm OK.", delete_after=6)
            return

        removed = []
        now = datetime.now(timezone.utc)
        for cid in self.selected:
            # remove from guild monitored list
            remove_guild_monitored(self.guild.id, cid)
            # if we also had per-channel monitored entry stored in `monitored` mapping, remove it
            rec = monitored.pop(cid, None)
            if rec and rec.get("alert_message_id"):
                try:
                    log_ch = bot.get_channel(rec.get("log_channel")) or await bot.fetch_channel(rec.get("log_channel"))
                    old = await log_ch.fetch_message(rec.get("alert_message_id"))
                    alert_time = old.created_at if getattr(old, 'created_at', None) else None
                    if alert_time and alert_time.tzinfo is None:
                        alert_time = alert_time.replace(tzinfo=timezone.utc)
                    # if the alert is older than THRESHOLD_SECONDS, preserve it instead of deleting
                    if alert_time and (now - alert_time).total_seconds() > THRESHOLD_SECONDS:
                        preserved_alerts[cid] = {
                            "log_channel": log_ch.id,
                            "alert_message_id": old.id,
                            "alert_sent_time": alert_time
                        }
                        # do not delete; leave it in log channel so users can Confirm it later
                    else:
                        try:
                            await old.delete()
                        except:
                            pass
                except Exception:
                    pass
            removed.append(cid)

        save_monitored()

        # Send a short result message (auto-delete) and return the main ConfigView (keep main UI intact)
        desc = f"✅ Đã xóa {len(removed)} monitor.\n" + ("\n".join(f"- <#{c}>" for c in removed) if removed else "Không có mục nào.")
        embed = discord.Embed(title="Remove monitors", description=desc, color=0xE74C3C, timestamp=datetime.now(timezone.utc))
        try:
            await interaction.response.send_message(embed=embed, delete_after=UI_TEMP_DELETE_SECONDS)
        except Exception:
            try:
                await interaction.response.send_message(desc, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass

        # return the main ConfigView on the original message (do NOT delete the main UI)
        try:
            await interaction.message.edit(content=None, embed=generate_main_embed(), view=ConfigView())
        except Exception:
            pass
        finally:
            self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="remove_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # permission check
        if interaction.user.id != self.requester.id and not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("❌ Bạn không có quyền thực hiện thao tác này.", delete_after=5)
            return

        try:
            await interaction.response.send_message("Đã hủy.", delete_after=UI_TEMP_DELETE_SECONDS)
        except:
            pass

        # return main UI
        try:
            await interaction.message.edit(content=None, embed=generate_main_embed(), view=ConfigView())
        except:
            pass
        self.stop()


class AddSelectView(discord.ui.View):
    def __init__(self, guild: discord.Guild, requester: discord.Member, *, timeout: int = None):
        super().__init__(timeout=None)
        self.guild = guild
        self.requester = requester
        # build options from channels in guild that are NOT in monitored
        opts = []
        for ch in guild.channels:
            if isinstance(ch, discord.CategoryChannel):
                continue
            if getattr(ch, "is_thread", False):
                continue
            # skip if already in guild monitored list
            if ch.id in guild_monitored_list(guild.id):
                continue
            kind = "voice" if isinstance(ch, discord.VoiceChannel) else "text"
            opts.append(discord.SelectOption(label=ch.name, value=str(ch.id), description=f"{kind} • {ch.id}"))
        if not opts:
            self.no_options = True
            return
        self.no_options = False
        opts = opts[:25]
        sel = discord.ui.Select(placeholder="Chọn channel (tối đa 25) để add vào monitor...", options=opts, min_values=1, max_values=len(opts))

        async def sel_cb(interaction: discord.Interaction):
            try:
                self.selected = [int(v) for v in sel.values]
            except:
                self.selected = []
            try:
                await interaction.response.defer()
            except:
                pass

        sel.callback = sel_cb
        self.add_item(sel)
        self.selected = []

    @discord.ui.button(label="➕ Add", style=discord.ButtonStyle.success, custom_id="add_ok")
    async def ok_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # permission check
        if interaction.user.id != self.requester.id and not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("❌ Bạn không có quyền thực hiện thao tác này.", delete_after=5)
            return

        if getattr(self, "no_options", False):
            await interaction.response.send_message("Không còn channel nào khả dụng để thêm vào monitor.", delete_after=UI_TEMP_DELETE_SECONDS)
            return

        if not getattr(self, "selected", None):
            await interaction.response.send_message("❗ Hãy chọn ít nhất 1 channel trước khi bấm OK.", delete_after=6)
            return

        added = []
        for cid in self.selected:
            if cid in monitored:
                # still register into guild monitored list
                add_guild_monitored(self.guild.id, cid)
                continue
            try:
                ch = self.guild.get_channel(cid) or await bot.fetch_channel(cid)
            except:
                continue
            # use guild-level log if present, else fallback
            lid = get_guild_log_channel(self.guild.id) or DEFAULT_LOG_CHANNEL_ID
            last_msg_time = None
            try:
                msgs = [m async for m in ch.history(limit=1)]
                if msgs:
                    last_msg_time = msgs[0].created_at.replace(tzinfo=timezone.utc)
                else:
                    last_msg_time = datetime.now(timezone.utc)
            except:
                last_msg_time = datetime.now(timezone.utc)
            monitored[cid] = {
                "log_channel": lid,
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

        desc = f"✅ Đã thêm {len(added)} monitor.\n" + ("\n".join(f"- <#{c}>" for c in added) if added else "Không có mục nào.")
        embed = discord.Embed(title="Add monitors", description=desc, color=0x2ECC71, timestamp=datetime.now(timezone.utc))
        try:
            await interaction.response.send_message(embed=embed, delete_after=UI_TEMP_DELETE_SECONDS)
        except:
            try:
                await interaction.response.send_message(desc, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass

        # return the main ConfigView on the original message (do NOT delete the main UI)
        try:
            await interaction.message.edit(content=None, embed=generate_main_embed(), view=ConfigView())
        except Exception:
            pass
        finally:
            self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="add_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.send_message("Đã hủy.", delete_after=UI_TEMP_DELETE_SECONDS)
        except:
            try:
                await interaction.response.send_message("Đã hủy.", delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass
        # return main UI
        try:
            await interaction.message.edit(content=None, embed=generate_main_embed(), view=ConfigView())
        except:
            pass
        self.stop()


# ---------------- Small Back view (from list -> back to config) ----------------
class BackToConfigView(discord.ui.View):
    def __init__(self, requester: discord.Member, *, timeout: int = None):
        super().__init__(timeout=None)
        self.requester = requester

    @discord.ui.button(label="⬅️ Back", style=discord.ButtonStyle.secondary, custom_id="back_to_config")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id and not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("❌ Bạn không có quyền thực hiện thao tác này.", delete_after=5)
            return
        try:
            await interaction.response.edit_message(content=None, embed=generate_main_embed(), view=ConfigView())
        except:
            try:
                await interaction.response.send_message("Không thể quay lại (lỗi).", delete_after=6)
            except:
                pass


# ---------------- Interactive Set Log View (guild-level) ----------------
class SetLogView(discord.ui.View):
    """
    View to set log channel for a guild.
    - If guild has no log yet: single dropdown listing all channels.
    - If guild has a log already: embed shows current log and dropdown lists all channels except the current (search available).
    """
    def __init__(self, guild: discord.Guild, requester: discord.Member, *, timeout: int = None):
        super().__init__(timeout=None)
        self.guild = guild
        self.requester = requester
        self.selected_log = None

        # current guild-level log
        current_log_id = get_guild_log_channel(guild.id)

        # Build options for log_select: list all channels (exclude category/thread), optionally exclude current log
        opts = []
        for ch in guild.channels:
            if isinstance(ch, discord.CategoryChannel):
                continue
            if getattr(ch, "is_thread", False):
                continue
            if current_log_id and ch.id == int(current_log_id):
                # exclude current when editing existing log
                continue
            kind = "voice" if isinstance(ch, discord.VoiceChannel) else "text"
            opts.append(discord.SelectOption(label=ch.name, value=str(ch.id), description=f"{kind} • {ch.id}"))

        if not opts:
            # nếu không tìm thấy channel nào (rất hiếm), báo cho user
            self.no_options = True
            return
        self.no_options = False
        opts = opts[:25]

        # Create log_select immediately (no need to choose monitored first)
        placeholder = "Chọn channel làm log cho server..." if not current_log_id else f"Chọn channel làm log (không gồm <#{current_log_id}>)..."
        self.log_select = discord.ui.Select(placeholder=placeholder, options=opts, min_values=1, max_values=1)
        self.log_select.callback = self.log_selected
        self.add_item(self.log_select)

    async def log_selected(self, interaction: discord.Interaction):
        try:
            self.selected_log = int(self.log_select.values[0])
        except:
            self.selected_log = None
        # quick ack
        try:
            await interaction.response.defer()
        except:
            pass

    @discord.ui.button(label="🔎 Search", style=discord.ButtonStyle.secondary, custom_id="setlog_search")
    async def search_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # permission check
        if interaction.user.id != self.requester.id and not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("❌ Bạn không có quyền thực hiện thao tác này.", delete_after=5)
            return

        class SearchModal(discord.ui.Modal, title="Search channels"):
            query = discord.ui.TextInput(label="Tên channel (một phần)", placeholder="Gõ một phần tên channel...", required=True, max_length=100)

            def __init__(self, parent_view: "SetLogView"):
                super().__init__()
                self.parent_view = parent_view

            async def on_submit(self, modal_interaction: discord.Interaction):
                q = self.query.value.strip().lower()
                if not q:
                    try:
                        await modal_interaction.response.send_message("❗ Query trống.", delete_after=6)
                    except:
                        pass
                    return

                matches = []
                for ch in self.parent_view.guild.channels:
                    if isinstance(ch, discord.CategoryChannel):
                        continue
                    if getattr(ch, "is_thread", False):
                        continue
                    # exclude current guild log to match earlier behaviour
                    cur = get_guild_log_channel(self.parent_view.guild.id)
                    if cur and ch.id == cur:
                        continue
                    if q in ch.name.lower():
                        kind = "voice" if isinstance(ch, discord.VoiceChannel) else "text"
                        matches.append(discord.SelectOption(label=ch.name, value=str(ch.id), description=f"{kind} • {ch.id}"))

                if not matches:
                    try:
                        await modal_interaction.response.send_message("Không tìm thấy channel nào khớp với query.", delete_after=6)
                    except:
                        pass
                    return

                # replace parent's select with search results (max 25)
                limited = matches[:25]
                try:
                    if hasattr(self.parent_view, "log_select"):
                        self.parent_view.remove_item(self.parent_view.log_select)
                except Exception:
                    pass

                self.parent_view.log_select = discord.ui.Select(
                    placeholder=f"Kết quả tìm kiếm ({len(matches)}). Chọn channel làm log...",
                    options=limited, min_values=1, max_values=1
                )
                self.parent_view.log_select.callback = self.parent_view.log_selected
                self.parent_view.add_item(self.parent_view.log_select)

                desc = f"**Search results:** \"{q}\" — {len(matches)} kết quả (hiển thị tối đa 25)\n\nChọn channel trong dropdown để làm log."
                embed = discord.Embed(title="Set log channel — Search results", description=desc, color=0x95A5A6, timestamp=datetime.now(timezone.utc))
                try:
                    await modal_interaction.response.edit_message(content=None, embed=embed, view=self.parent_view)
                except Exception:
                    try:
                        await modal_interaction.response.send_message("Đã cập nhật dropdown với kết quả tìm kiếm.", delete_after=6)
                    except:
                        pass

        try:
            await interaction.response.send_modal(SearchModal(self))
        except Exception as e:
            try:
                await interaction.response.send_message(f"Không thể mở modal tìm kiếm: {e}", delete_after=6)
            except:
                pass

    @discord.ui.button(label="✅ Set log", style=discord.ButtonStyle.success, custom_id="setlog_ok")
    async def ok_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # permission check
        if interaction.user.id != self.requester.id and not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("❌ Bạn không có quyền thực hiện thay đổi log.", delete_after=5)
            return

        if getattr(self, "no_options", False):
            await interaction.response.send_message("Không có channel nào để chọn làm log trong server này.", delete_after=UI_TEMP_DELETE_SECONDS)
            return

        if not self.selected_log:
            await interaction.response.send_message("❗ Hãy chọn log channel trước khi bấm Set.", delete_after=6)
            return

        # validate access to selected log channel
        try:
            _ = self.guild.get_channel(self.selected_log) or await bot.fetch_channel(self.selected_log)
        except Exception as e:
            await interaction.response.send_message(f"❌ Không thể truy cập log channel đã chọn: {e}", delete_after=6)
            return

        # apply change: update guild-level config ONLY
        set_guild_log_channel(self.guild.id, self.selected_log)

        desc = f"✅ Đã gán log cho server: <#{self.selected_log}>.\n(Lưu ý: log là cấu hình cấp server, không gán cho từng monitor.)"
        embed = discord.Embed(title="Set log", description=desc, color=0x2ECC71, timestamp=datetime.now(timezone.utc))
        try:
            await interaction.response.send_message(embed=embed, delete_after=UI_TEMP_DELETE_SECONDS)
        except:
            try:
                await interaction.response.send_message(desc, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass

        try:
            await interaction.message.edit(content=None, embed=generate_main_embed(), view=ConfigView())
        except:
            pass
        finally:
            self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="setlog_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id and not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("❌ Bạn không có quyền thực hiện thao tác này.", delete_after=5)
            return
        try:
            await interaction.response.send_message("Đã hủy.", delete_after=UI_TEMP_DELETE_SECONDS)
        except:
            pass
        try:
            await interaction.message.edit(content=None, embed=generate_main_embed(), view=ConfigView())
        except:
            pass
        self.stop()


# ---------------- Interactive slash UI: ConfigView + Modals ----------------
# Note: SetLogModal removed; replaced with SetLogView above.
class MassCreateModal(discord.ui.Modal, title="Create multiple channels"):
    base_name = discord.ui.TextInput(label="Base name", placeholder="base-name", required=True, max_length=100)
    count = discord.ui.TextInput(label="Count", placeholder="Number of channels to create", required=True, max_length=6)
    chan_type = discord.ui.TextInput(label="Channel type", placeholder="text or voice (optional)", required=False, max_length=10)
    start = discord.ui.TextInput(label="Start index", placeholder="1 (optional)", required=False, max_length=6)
    category = discord.ui.TextInput(label="Category (mention or id, optional)", placeholder="<#cat_id> or id", required=False, max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        user = interaction.user
        if not (user.guild_permissions.manage_channels or user.guild_permissions.administrator):
            try:
                await interaction.response.send_message("Bạn cần quyền Manage Channels để thực hiện tạo channel.", delete_after=6)
            except:
                pass
            return

        # parse inputs
        base_name = self.base_name.value.strip()
        try:
            count = int(self.count.value.strip())
        except:
            await interaction.response.send_message("❌ Count không hợp lệ.", delete_after=6)
            return
        chan_type = (self.chan_type.value or "text").strip().lower()
        try:
            start = int((self.start.value or "1").strip())
        except:
            start = 1
        # padding removed from modal: we'll pass padding=0 so do_masscreate will auto-calc
        padding = 0
        category_arg = (self.category.value or "").strip()
        category_id = parse_channel_argument(category_arg) if category_arg else None

        # basic validation
        if count <= 0 or count > 500:
            await interaction.response.send_message("❌ Count phải trong khoảng 1..500.", delete_after=6)
            return

        # Determine notify channel safely (modal interactions sometimes have channel)
        notify_channel = interaction.channel
        if notify_channel is None and interaction.guild and interaction.guild.system_channel:
            notify_channel = interaction.guild.system_channel
        # If still None, use user's DM
        if notify_channel is None:
            try:
                dm = await interaction.user.create_dm()
                notify_channel = dm
            except:
                notify_channel = None

        # acknowledge immediately
        try:
            await interaction.response.send_message("⏳ Yêu cầu tạo channel đã được nhận — bot sẽ gửi kết quả ở đây khi hoàn thành.", delete_after=UI_TEMP_DELETE_SECONDS)
        except:
            pass

        # create background task (padding=0 so do_masscreate will auto-calc)
        asyncio.create_task(do_masscreate(interaction.guild, notify_channel, base_name, count, chan_type, start, padding, category_id, user))


async def do_masscreate(guild: discord.Guild, notify_channel: discord.abc.Messageable, base_name: str, count: int, chan_type: str, start: int, padding: int, category_id: int, author: discord.Member):
    created = []
    failed = []
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
            progress_msg = await notify_channel.send(f"⏳ Bắt đầu tạo {count} {'voice' if chan_type.startswith('v') else 'text'} channel từ `{base_name}-{start}` ... (padding={padding})")
    except:
        progress_msg = None

    for i in range(start, start + count):
        number_str = str(i).zfill(padding) if padding > 0 else str(i)
        chan_name = f"{base_name}-{number_str}"
        try:
            if chan_type.startswith('v'):
                ch = await guild.create_voice_channel(name=chan_name, category=category_obj, reason=f"masscreate by {author} ({author.id})")
            else:
                ch = await guild.create_text_channel(name=chan_name, category=category_obj, reason=f"masscreate by {author} ({author.id})")
            created.append(ch)
            # throttle to avoid hitting API limits
            await asyncio.sleep(0.6)
            # optional: update progress every 10 channels
            if progress_msg and (len(created) % 10 == 0):
                try:
                    await progress_msg.edit(content=f"⏳ Đã tạo {len(created)}/{count} channel... (last: `{chan_name}`)")
                except:
                    pass
        except discord.HTTPException:
            try:
                # small retry
                await asyncio.sleep(2)
                if chan_type.startswith('v'):
                    ch = await guild.create_voice_channel(name=chan_name, category=category_obj, reason=f"retry masscreate by {author}")
                else:
                    ch = await guild.create_text_channel(name=chan_name, category=category_obj, reason=f"retry masscreate by {author}")
                created.append(ch)
            except Exception as e2:
                failed.append((chan_name, str(e2)))
                await asyncio.sleep(1)
        except Exception as e:
            failed.append((chan_name, str(e)))
            await asyncio.sleep(0.5)

    summary = f"✅ Hoàn thành. Tạo được {len(created)}/{count} channel."
    if failed:
        sample = failed[:6]
        summary += f"\n⚠️ Thất bại: {len(failed)}. Các lỗi mẫu: {sample}"

    # --- UPDATED: ensure the summary message is auto-deleted after UI_TEMP_DELETE_SECONDS ---
    try:
        if progress_msg:
            # edit the existing progress message to the final summary
            try:
                await progress_msg.edit(content=summary)
            except:
                # fallback: try sending new message
                await notify_channel.send(summary, delete_after=UI_TEMP_DELETE_SECONDS)
                # schedule deletion only for the newly sent message isn't needed because delete_after already set
            else:
                # schedule deletion of the progress_msg after UI_TEMP_DELETE_SECONDS
                asyncio.create_task(_delete_message_later(progress_msg.channel, progress_msg.id, UI_TEMP_DELETE_SECONDS))
        elif notify_channel:
            # send a new summary message and set delete_after
            try:
                await notify_channel.send(summary, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                # best-effort fallback: try DM to author
                try:
                    await author.send(summary, delete_after=UI_TEMP_DELETE_SECONDS)
                except:
                    pass
    except Exception:
        # as last resort, try DM to author (if available)
        try:
            await author.send(summary, delete_after=UI_TEMP_DELETE_SECONDS)
        except:
            pass


# ---------------- UI generation helpers ----------------
def generate_main_embed():
    embed = discord.Embed(
	title="Cấu hình <Check messages> (Beta)",
        description="Bảng điều khiển monitor tương tác — quản lý các kênh đang được theo dõi, thiết lập kênh ghi log — tạo hàng loạt kênh.",
        color=0x7B61FF,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Functions (Chức năng)", value="• **Add monitor** — Thêm channel để quản lí\n• **Delete monitor** — Xoá các channel đã thêm\n• **Set log** — Thiết lập log-channel\n• **Create channels** — Tạo channel theo tên (custom) + số thứ tự tăng dần", inline=False)
    embed.set_footer(text="Created by: グエン・ティン・フー (Guen tin fū)", icon_url=None)
    if os.path.exists(MONITORED_IMAGE_PATH):
        embed.set_image(url="attachment://hydra.png")
    return embed


# ---------------- Interactive ConfigView ----------------
class ConfigView(discord.ui.View):
    def __init__(self, *, timeout: int = None):
        # persistent: never timeout so users can interact at any time until they press Close
        super().__init__(timeout=None)

    @discord.ui.button(label="📜 List monitors", style=discord.ButtonStyle.primary, custom_id="cm_list")
    async def list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Bạn cần quyền Manage Channels để sử dụng.", delete_after=5)
            return
        gm = guild_monitored_list(interaction.guild.id)
        if not gm:
            try:
                await interaction.response.send_message("Danh sách monitor trống.", delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass
            return
        lines = []
        for cid in gm:
            rec = monitored.get(cid)
            lid = (rec.get("log_channel") if rec else None) or get_guild_log_channel(interaction.guild.id) or DEFAULT_LOG_CHANNEL_ID
            conf = " (CONFIRMED)" if rec and rec.get("confirmed") else ""
            cnt = rec.get("alert_count", 0) if rec else 0
            lines.append(f"- <#{cid}> -> <#{lid}> (count: {cnt}){conf}")
        desc = "**Monitored channels:**\n" + "\n".join(lines)
        embed = discord.Embed(title="📋 Danh sách monitor", description=desc, color=0x3498DB, timestamp=datetime.now(timezone.utc))
        try:
            await interaction.response.edit_message(content=None, embed=embed, view=BackToConfigView(interaction.user))
        except:
            try:
                await interaction.response.send_message(desc, delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass

    @discord.ui.button(label="➕ Add monitor", style=discord.ButtonStyle.success, custom_id="cm_add")
    async def add_button(self, interaction: discord.Interaction, button: discord.Button):
        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Bạn cần quyền Manage Channels để sử dụng.", delete_after=5)
            return
        view = AddSelectView(interaction.guild, interaction.user)
        if getattr(view, "no_options", False):
            try:
                await interaction.response.send_message("Không còn channel nào để thêm vào monitor (hoặc giới hạn 25 tùy chọn đã đầy).", delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass
            return
        embed = discord.Embed(title="➕ Thêm monitor", description="Chọn các channel để thêm vào monitor, sau đó bấm **Add** hoặc **Cancel**.", color=0x2ECC71, timestamp=datetime.now(timezone.utc))
        try:
            await interaction.response.edit_message(content=None, embed=embed, view=view)
        except:
            try:
                await interaction.response.send_message("Không thể mở giao diện thêm (lỗi)", delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass

    @discord.ui.button(label="🗑️ Remove monitor", style=discord.ButtonStyle.danger, custom_id="cm_remove")
    async def remove_button(self, interaction: discord.Interaction, button: discord.Button):
        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Bạn cần quyền Manage Channels để sử dụng.", delete_after=5)
            return
        view = RemoveSelectView(interaction.guild, interaction.user)
        if getattr(view, "no_options", False):
            try:
                await interaction.response.send_message("Không có channel nào đang được theo dõi để xóa.", delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass
            return
        embed = discord.Embed(title="🗑️ Xóa monitor", description="Chọn các channel cần xóa khỏi monitor, sau đó bấm **Delete** hoặc **Cancel**.", color=0xE74C3C, timestamp=datetime.now(timezone.utc))
        try:
            await interaction.response.edit_message(content=None, embed=embed, view=view)
        except:
            try:
                await interaction.response.send_message("Không thể mở giao diện xóa (lỗi)", delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass

    @discord.ui.button(label="⚙️ Set log", style=discord.ButtonStyle.secondary, custom_id="cm_setlog")
    async def setlog_button(self, interaction: discord.Interaction, button: discord.Button):
        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Bạn cần quyền Manage Channels để sử dụng lệnh này.", delete_after=5)
            return
        # Show the interactive SetLogView (no longer requires monitored)
        try:
            view = SetLogView(interaction.guild, interaction.user)
        except Exception as e:
            try:
                await interaction.response.send_message(f"Không thể mở giao diện Set log (lỗi build view): {e}", delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass
            return

        if getattr(view, "no_options", False):
            try:
                await interaction.response.send_message("Không tìm thấy channel nào trong server để chọn làm log.", delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass
            return

        # show current if exists
        cur = get_guild_log_channel(interaction.guild.id)
        desc = "Chọn channel làm log cho server. Nếu server có nhiều channel, dùng nút 🔎 Search để lọc theo tên."
        if cur:
            desc = f"**Current log:** <#{cur}>\n\n" + desc

        embed = discord.Embed(title="⚙️ Set log", description=desc, color=0x95A5A6, timestamp=datetime.now(timezone.utc))
        try:
            await interaction.response.edit_message(content=None, embed=embed, view=view)
        except Exception as e:
            try:
                await interaction.response.send_message(f"Không thể mở giao diện Set log (lỗi). {e}", delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass

    @discord.ui.button(label="🛠️ Create channels", style=discord.ButtonStyle.primary, custom_id="cm_masscreate")
    async def masscreate_button(self, interaction: discord.Interaction, button: discord.Button):
        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Bạn cần quyền Manage Channels để sử dụng.", delete_after=5)
            return
        try:
            await interaction.response.send_modal(MassCreateModal())
        except:
            try:
                await interaction.response.send_message("Không thể mở modal Create Channels.", delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass

    @discord.ui.button(label="❌ Close", style=discord.ButtonStyle.secondary, custom_id="cm_close")
    async def close_button(self, interaction: discord.Interaction, button: discord.Button):
        try:
            await interaction.response.edit_message(content=None, embed=discord.Embed(title="Giao diện đã đóng", description="Dùng /cmconfig để mở lại.", color=0x95A5A6), view=None)
        except:
            try:
                await interaction.response.send_message("Đã đóng.", delete_after=UI_TEMP_DELETE_SECONDS)
            except:
                pass


# ---------------- Helper to post UI ----------------
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
        sent = await ch.send(embed=embed, view=ConfigView(), file=file) if file else await ch.send(embed=embed, view=ConfigView())
        return True
    except Exception as e:
        print(f"Failed to send UI to channel {channel_id}: {e}")
        return False


# ---------------- Bot events & loop ----------------
@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user} (id: {bot.user.id})")
    load_config()
    load_monitored()

    # init last_message_time for monitored if missing
    # We'll attempt to initialize only for channels we can access
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

    # register persistent ConfigView so it won't time out
    try:
        bot.add_view(ConfigView())
    except Exception:
        pass

    # Sync application commands:
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

    if not check_loop.is_running():
        check_loop.start()


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def check_loop():
    now = datetime.now(timezone.utc)
    for cid in list(monitored.keys()):
        try:
            ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
            msgs = [m async for m in ch.history(limit=1)]
            if not msgs:
                continue
            last_msg_time = msgs[0].created_at.replace(tzinfo=timezone.utc)
            rec = monitored.get(cid)
            if rec is None:
                monitored[cid] = {
                    "log_channel": get_guild_log_channel(ch.guild.id) or DEFAULT_LOG_CHANNEL_ID,
                    "last_message_time": last_msg_time,
                    "alert_count": 0,
                    "alert_message_id": None,
                    "alert_sent_time": None,
                    "confirmed": False,
                    "confirmed_by": None
                }
                save_monitored()
                rec = monitored[cid]

            # if new message -> reset (also un-confirm)
            if rec.get("last_message_time") is None or last_msg_time != rec.get("last_message_time"):
                rec["last_message_time"] = last_msg_time
                rec["alert_count"] = 0
                rec["confirmed"] = False
                rec["confirmed_by"] = None

                # delete old alert if existed
                if rec.get("alert_message_id"):
                    try:
                        log_ch_id = rec.get("log_channel") or get_guild_log_channel(ch.guild.id) or DEFAULT_LOG_CHANNEL_ID
                        log_ch = bot.get_channel(log_ch_id) or await bot.fetch_channel(log_ch_id)
                        old = await log_ch.fetch_message(rec["alert_message_id"])
                        await old.delete()
                    except:
                        pass
                    rec["alert_message_id"] = None
                    rec["alert_sent_time"] = None
                save_monitored()
                print(f"Reset alert for {ch.name}")
                continue

            # skip sending if monitor is confirmed (user pressed CONFIRM)
            if rec.get("confirmed"):
                continue

            diff = (now - rec["last_message_time"]).total_seconds()
            if diff > THRESHOLD_SECONDS:
                rec["alert_count"] = rec.get("alert_count", 0) + 1
                log_ch_id = rec.get("log_channel") or get_guild_log_channel(ch.guild.id) or DEFAULT_LOG_CHANNEL_ID
                try:
                    log_ch = bot.get_channel(log_ch_id) or await bot.fetch_channel(log_ch_id)
                except Exception as e:
                    print(f"Cannot access log channel {log_ch_id} for monitor {cid}: {e}")
                    continue

                # delete old alert if exists
                if rec.get("alert_message_id"):
                    try:
                        old = await log_ch.fetch_message(rec["alert_message_id"])
                        await old.delete()
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

                # mentions
                mention_parts = []
                if PING_EVERYONE:
                    mention_parts.append("@everyone")
                if PING_ROLE_IDS:
                    mention_parts.extend(f"<@&{rid}>" for rid in PING_ROLE_IDS)
                content = " ".join(mention_parts) if mention_parts else None
                allowed = discord.AllowedMentions(everyone=bool(PING_EVERYONE),
                                                  roles=bool(PING_ROLE_IDS),
                                                  users=False)
                # attach confirm button
                view = ConfirmView(cid)
                try:
                    sent = await log_ch.send(content=content, embed=embed, allowed_mentions=allowed, view=view)
                    rec["alert_message_id"] = sent.id
                    rec["alert_sent_time"] = now
                    save_monitored()
                    # schedule deletion after AUTO_DELETE_SECONDS
                    asyncio.create_task(_delete_message_later(log_ch, sent.id, AUTO_DELETE_SECONDS))
                    print(f"Alert {rec['alert_count']} - {ch.name} -> sent to {log_ch.id}")
                except Exception as e:
                    print(f"Failed to send alert for {cid} to {log_ch_id}: {e}")
        except Exception as e:
            print(f"Error monitoring {cid}: {e}")


# ---------------- Management commands (unchanged) ----------------
@bot.group(name="monitor", invoke_without_command=True)
@commands.has_guild_permissions(manage_channels=True)
async def monitor_group(ctx):
    await ctx.reply("Commands: `!monitor add <#chan|id> [#log|id]`, `!monitor remove <#chan|id>`, `!monitor setlog <#chan|id> <#log|id>`, `!monitor list`", mention_author=False)


# ---------------- Mass-create channels command (ADD ONLY) ----------------
@bot.command(name="masscreate")
@commands.has_guild_permissions(manage_channels=True)
async def masscreate(ctx, base_name: str, count: int, chan_type: str = "text", start: int = 1, padding: int = 0, category: str = None):
    """Tạo nhiều channel theo pattern base_name-<number>."""
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
    # initial reply (kept visible briefly if you want)
    await ctx.reply(f"⏳ Bắt đầu tạo {count} {'voice' if is_voice else 'text'} channel từ `{base_name}-{start}` ... (padding={padding})", mention_author=False, delete_after=UI_TEMP_DELETE_SECONDS)

    for i in range(start, start + count):
        number_str = str(i).zfill(padding) if padding > 0 else str(i)
        chan_name = f"{base_name}-{number_str}"
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

    # reply summary and set it to auto-delete
    await ctx.reply(summary, mention_author=False, delete_after=UI_TEMP_DELETE_SECONDS)


# ---------------- Slash command entrypoint for interactive UI ----------------
@bot.tree.command(name="cmconfig", description="Interactive monitor configuration")
async def cmconfig(interaction: discord.Interaction):
    if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
        await interaction.response.send_message("Bạn cần quyền Manage Channels để sử dụng lệnh này.", delete_after=5)
        return
    embed = generate_main_embed()
    try:
        # send embed with view (public)
        if os.path.exists(MONITORED_IMAGE_PATH):
            file = discord.File(MONITORED_IMAGE_PATH, filename="hydra.png")
            await interaction.response.send_message(embed=embed, view=ConfigView(), file=file)
        else:
            await interaction.response.send_message(embed=embed, view=ConfigView())
    except Exception:
        # fallback: ephemeral
        try:
            await interaction.response.send_message("Chọn hành động cấu hình:", view=ConfigView(), ephemeral=True)
        except:
            pass


# ---------------- New command: setup a channel to host the UI ----------------
@bot.tree.command(name="cmsetup", description="Set channel where the interactive monitor UI will be posted")
async def cmsetup(interaction: discord.Interaction, channel: str):
    if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
        await interaction.response.send_message("Bạn cần quyền Manage Channels để sử dụng lệnh này.", delete_after=5)
        return
    cid = parse_channel_argument(channel)
    if cid is None:
        await interaction.response.send_message("❌ Đầu vào không hợp lệ. Dùng <#id> hoặc id.", delete_after=6)
        return
    try:
        _ = bot.get_channel(cid) or await bot.fetch_channel(cid)
    except Exception as e:
        await interaction.response.send_message(f"❌ Không thể truy cập channel: {e}", delete_after=6)
        return
    # Save per-guild ui channel
    set_guild_ui_channel(interaction.guild.id, cid)
    # Post the UI immediately (best-effort)
    posted = await post_ui_to_channel(cid, guild=interaction.guild)
    if posted:
        await interaction.response.send_message(f"✅ Đã thiết lập channel giao diện: <#{cid}> và đăng giao diện ở đó.", delete_after=8)
    else:
        await interaction.response.send_message(f"⚠️ Đã lưu <#{cid}> làm channel giao diện nhưng không thể đăng (kiểm tra quyền).", delete_after=8)


# ---------------- Run ----------------
if __name__ == "__main__":
    if not TOKEN or TOKEN == "REPLACE_WITH_ENV":
        print("ERROR: BOT TOKEN chưa cấu hình. Set BOT_TOKEN environment variable or edit file to add TOKEN.")
    else:
        bot.run(TOKEN)


