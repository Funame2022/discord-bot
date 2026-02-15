import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import discord
from discord.ext import tasks, commands

# ------------------ CONFIGURATION ------------------
import os
TOKEN = os.getenv("BOT_TOKEN")   # ⚠ NÊN dùng ENV
LOG_CHANNEL_ID = 1472491858096820277

CHECK_CHANNEL_IDS = [
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

THRESHOLD_SECONDS = 180
CHECK_INTERVAL_SECONDS = 10
LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
# ---------------------------------------------------

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Trạng thái từng channel
channel_state = {}
"""
channel_state[cid] = {
    "last_message_time": datetime,
    "alert_count": int,
    "alert_message_id": int | None
}
"""


def format_seconds(seconds: float):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s"


def local_time_str(dt):
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user}")
    if not check_channels.is_running():
        check_channels.start()


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def check_channels():
    now = datetime.now(timezone.utc)

    for cid in CHECK_CHANNEL_IDS:
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

            # ✅ Nếu có tin nhắn mới → reset toàn bộ
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

            # ❗ Kiểm tra quá 3 phút
            diff = (now - last_msg_time).total_seconds()

            if diff > THRESHOLD_SECONDS:
                state["alert_count"] += 1

                log_ch = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)

                # ❗ Nếu đã có alert cũ → xóa
                if state["alert_message_id"]:
                    try:
                        old_msg = await log_ch.fetch_message(state["alert_message_id"])
                        await old_msg.delete()
                    except:
                        pass

                embed = discord.Embed(
                    title=f"👉**{channel.name}**👈 quá 3 phút chưa xong Mission. Fix đi mấy con bò 🐄",
                    color=0xE74C3C,
                    timestamp=now
                )

                embed.add_field(name="Last message", value=local_time_str(last_msg_time), inline=True)
                embed.add_field(name="Delay", value=format_seconds(diff), inline=True)
                embed.add_field(name="Thông báo lần", value=str(state["alert_count"]), inline=True)

                sent_msg = await log_ch.send(embed=embed)

                # Lưu ID alert mới
                state["alert_message_id"] = sent_msg.id

                print(f"Alert {state['alert_count']} - {channel.name}")

        except Exception as e:
            print(f"Lỗi channel {cid}: {e}")


if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: BOT TOKEN chưa cấu hình.")
    else:
        bot.run(TOKEN)


