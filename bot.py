# file: bot.py
import os
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import discord
from discord.ext import tasks, commands

# ------------------ CONFIGURATION ------------------
TOKEN = os.getenv("BOT_TOKEN")   # ⚠ dùng env (Railway / local .env)
CHANNELS_FILE = "tracked_channels.json"

LOG_CHANNEL_ID = 1472491858096820277

# nếu file tracked_channels.json không tồn tại, bot sẽ dùng list mặc định bên dưới
DEFAULT_CHECK_CHANNEL_IDS = [
    1457983470491013321,
    1457983538250125515,
    1457983557409439795,
    1457983571670335632,
    1457983571670335632,
    1457983598601703651,
    1469244728087416897,
    1469244760094015509,
    1469244794181255200,
    1469244861818605647,
    1469245253818122281,
    1469245345593819177,
    1469245372521250972,
    1469245416301269002,
    1469246185079443548,
    1469246225760129117,
    1469246225760129117,
    1469246676219854964,
    1469246712999837808,
    1469246740166217965
]

THRESHOLD_SECONDS = 300
CHECK_INTERVAL_SECONDS = 180
LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# MENTION CONFIG
PING_EVERYONE = True   # True = bot sẽ thêm "@everyone" khi gửi cảnh báo
PING_ROLE_IDS = [
    # ví dụ: 123456789012345678
]
# ---------------------------------------------------

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# tracked channels in-memory (mutable). Loaded from disk on start if possible.
CHECK_CHANNEL_IDS = []

# Trạng thái từng channel
channel_state = {}
"""
channel_state[cid] = {
    "last_message_time": datetime,
    "alert_count": int,
    "alert_message_id": int | None
}
"""


# ----------------- Persistence helpers -----------------
def save_tracked_channels():
    try:
        with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(CHECK_CHANNEL_IDS, f)
        print("Saved tracked channels.")
    except Exception as e:
        print("Failed to save channels:", e)


def load_tracked_channels():
    global CHECK_CHANNEL_IDS
    if os.path.exists(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # ensure ints
            CHECK_CHANNEL_IDS = [int(x) for x in data]
            print("Loaded tracked channels from file.")
            return
        except Exception as e:
            print("Failed to load channels file, using default. Error:", e)
    # fallback default
    CHECK_CHANNEL_IDS = DEFAULT_CHECK_CHANNEL_IDS.copy()
    save_tracked_channels()


# ----------------- Utility -----------------
def format_seconds(seconds: float):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s"


def local_time_str(dt):
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def parse_channel_argument(ctx, arg: str):
    """
    Accept formats:
    - <#channelid> (mention)
    - channelid (digits)
    - #channel-name (not supported)
    Returns channel id (int) or None
    """
    if not arg:
        return None
    # if mention like <#123...>
    if arg.startswith("<#") and arg.endswith(">"):
        try:
            return int(arg[2:-1])
        except:
            return None
    # direct id
    if arg.isdigit():
        return int(arg)
    return None


# ----------------- Bot events & loop -----------------
@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user}")
    load_tracked_channels()
    # initialize channel_state with last messages
    for cid in CHECK_CHANNEL_IDS:
        try:
            ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
            msgs = [m async for m in ch.history(limit=1)]
            if msgs:
                channel_state[cid] = {
                    "last_message_time": msgs[0].created_at.replace(tzinfo=timezone.utc),
                    "alert_count": 0,
                    "alert_message_id": None
                }
            else:
                channel_state[cid] = {
                    "last_message_time": datetime.now(timezone.utc),
                    "alert_count": 0,
                    "alert_message_id": None
                }
        except Exception as e:
            print(f"Init: cannot access channel {cid}: {e}")
            # still create default state
            channel_state[cid] = {
                "last_message_time": datetime.now(timezone.utc),
                "alert_count": 0,
                "alert_message_id": None
            }

    if not check_channels.is_running():
        check_channels.start()


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def check_channels():
    now = datetime.now(timezone.utc)

    for cid in CHECK_CHANNEL_IDS.copy():  # copy to allow runtime modification
        try:
            channel = bot.get_channel(cid)
            if channel is None:
                channel = await bot.fetch_channel(cid)

            msgs = [m async for m in channel.history(limit=1)]
            if not msgs:
                continue

            last_msg_time = msgs[0].created_at.replace(tzinfo=timezone.utc)

            if cid not in channel_state:
                channel_state[cid] = {
                    "last_message_time": last_msg_time,
                    "alert_count": 0,
                    "alert_message_id": None
                }

            state = channel_state[cid]

            # Nếu có tin nhắn mới → reset toàn bộ
            if last_msg_time != state["last_message_time"]:
                state["last_message_time"] = last_msg_time
                state["alert_count"] = 0

                if state["alert_message_id"]:
                    try:
                        log_ch = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
                        old_msg = await log_ch.fetch_message(state["alert_message_id"])
                        await old_msg.delete()
                    except:
                        pass

                state["alert_message_id"] = None
                print(f"Reset alert {channel.name}")
                continue

            # Kiểm tra quá threshold
            diff = (now - last_msg_time).total_seconds()

            if diff > THRESHOLD_SECONDS:
                state["alert_count"] += 1

                log_ch = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)

                # Nếu đã có alert cũ → xóa
                if state["alert_message_id"]:
                    try:
                        old_msg = await log_ch.fetch_message(state["alert_message_id"])
                        await old_msg.delete()
                    except:
                        pass

                embed = discord.Embed(
                    title=f"👉**{channel.name}**👈 quá {THRESHOLD_SECONDS//60} phút chưa xong Mission. Fix đi mấy con bò 🐄",
                    color=0xE74C3C,
                    timestamp=now
                )

                embed.add_field(name="Last message", value=local_time_str(last_msg_time), inline=True)
                embed.add_field(name="Delay", value=format_seconds(diff), inline=True)
                embed.add_field(name="Thông báo lần", value=str(state["alert_count"]), inline=True)

                # build mention content + allowed_mentions
                mention_parts = []
                if PING_EVERYONE:
                    mention_parts.append("@everyone")
                if PING_ROLE_IDS:
                    mention_parts.extend(f"<@&{rid}>" for rid in PING_ROLE_IDS)

                content_to_send = " ".join(mention_parts) if mention_parts else None
                allowed = discord.AllowedMentions(everyone=bool(PING_EVERYONE),
                                                  roles=bool(PING_ROLE_IDS),
                                                  users=False)

                sent_msg = await log_ch.send(content=content_to_send, embed=embed, allowed_mentions=allowed)

                # Lưu ID alert mới
                state["alert_message_id"] = sent_msg.id

                print(f"Alert {state['alert_count']} - {channel.name}")

        except Exception as e:
            print(f"Lỗi channel {cid}: {e}")


# ----------------- Management commands (add/remove/list) -----------------
# only allow users with Manage Channels permission (or administrator) to modify list


@bot.command(name="addchannel")
@commands.has_guild_permissions(manage_channels=True)
async def cmd_addchannel(ctx, arg: str):
    """
    Usage:
      !addchannel <#channel>  (mention)  OR
      !addchannel <channel_id>
    """
    cid = parse_channel_argument(ctx, arg)
    if cid is None:
        await ctx.send("❌ Vui lòng gửi đúng dạng: `!addchannel <#channel>` hoặc `!addchannel <channel_id>`")
        return

    if cid in CHECK_CHANNEL_IDS:
        await ctx.send(f"ℹ Channel <#{cid}> đã được theo dõi rồi.")
        return

    # verify bot can access channel
    try:
        ch = await bot.fetch_channel(cid)
    except Exception as e:
        await ctx.send(f"❌ Không tìm thấy channel hoặc bot không có quyền truy cập: {e}")
        return

    CHECK_CHANNEL_IDS.append(cid)
    # init state
    try:
        msgs = [m async for m in ch.history(limit=1)]
        last_time = msgs[0].created_at.replace(tzinfo=timezone.utc) if msgs else datetime.now(timezone.utc)
    except Exception:
        last_time = datetime.now(timezone.utc)

    channel_state[cid] = {
        "last_message_time": last_time,
        "alert_count": 0,
        "alert_message_id": None
    }

    save_tracked_channels()
    await ctx.send(f"✅ Đã thêm channel <#{cid}> vào danh sách theo dõi.")


@cmd_addchannel.error
async def addchannel_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Bạn cần quyền `Manage Channels` để sử dụng lệnh này.")
    else:
        await ctx.send(f"❌ Lỗi: {error}")


@bot.command(name="removechannel")
@commands.has_guild_permissions(manage_channels=True)
async def cmd_removechannel(ctx, arg: str):
    cid = parse_channel_argument(ctx, arg)
    if cid is None:
        await ctx.send("❌ Vui lòng gửi đúng dạng: `!removechannel <#channel>` hoặc `!removechannel <channel_id>`")
        return

    if cid not in CHECK_CHANNEL_IDS:
        await ctx.send(f"ℹ Channel <#{cid}> chưa được theo dõi.")
        return

    CHECK_CHANNEL_IDS.remove(cid)
    # delete state and try to remove alert message
    rec = channel_state.pop(cid, None)
    if rec and rec.get("alert_message_id"):
        try:
            log_ch = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
            old_msg = await log_ch.fetch_message(rec["alert_message_id"])
            await old_msg.delete()
        except:
            pass

    save_tracked_channels()
    await ctx.send(f"✅ Đã xóa channel <#{cid}> khỏi danh sách theo dõi.")


@cmd_removechannel.error
async def removechannel_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Bạn cần quyền `Manage Channels` để sử dụng lệnh này.")
    else:
        await ctx.send(f"❌ Lỗi: {error}")


@bot.command(name="listchannels")
@commands.has_guild_permissions(manage_channels=True)
async def cmd_listchannels(ctx):
    if not CHECK_CHANNEL_IDS:
        await ctx.send("Danh sách theo dõi trống.")
        return
    lines = []
    for cid in CHECK_CHANNEL_IDS:
        lines.append(f"- <#{cid}> (`{cid}`)")
    text = "**Channels đang theo dõi:**\n" + "\n".join(lines)
    await ctx.send(text)


@cmd_listchannels.error
async def listchannels_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Bạn cần quyền `Manage Channels` để sử dụng lệnh này.")
    else:
        await ctx.send(f"❌ Lỗi: {error}")


# ----------------- Run -----------------
if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: BOT TOKEN chưa cấu hình.")
    else:
        bot.run(TOKEN)


