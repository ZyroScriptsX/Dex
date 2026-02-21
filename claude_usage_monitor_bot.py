import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CLAUDE_SESSION_KEY = os.getenv("CLAUDE_SESSION_KEY")
CLAUDE_ORG_ID = os.getenv("CLAUDE_ORG_ID")

USAGE_URL = f"https://claude.ai/api/organizations/{CLAUDE_ORG_ID}/usage"

HEADERS = {
    "accept": "*/*",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "anthropic-client-platform": "web_claude_ai",
    "anthropic-client-version": "1.0.0",
    "content-type": "application/json",
    "cache-control": "no-cache",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
}


def make_cookies() -> dict[str, str]:
    return {"sessionKey": CLAUDE_SESSION_KEY or ""}


def normalize_pct(value: Any) -> float:
    pct = float(value or 0)
    # Some APIs return ratios (0-1), others percentages (0-100).
    if 0 <= pct <= 1:
        pct *= 100
    return max(0.0, min(100.0, pct))


def progress_bar(pct: float, length: int = 10) -> str:
    filled = round(pct / 100 * length)
    filled = max(0, min(length, filled))
    return "█" * filled + "░" * (length - filled)


def usage_color(pct: float) -> int:
    if pct >= 80:
        return 0xED4245  # red
    if pct >= 50:
        return 0xFEE75C  # yellow
    return 0x57F287  # green


def iso_to_discord_ts(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return f"<t:{int(dt.timestamp())}:R>"
    except (TypeError, ValueError, AttributeError):
        return "unknown"


@dataclass
class MonitorState:
    active: bool = False
    threshold: float = 80.0
    interval_minutes: int = 5
    channel_id: Optional[int] = None
    user_id: Optional[int] = None
    alerted_keys: set[str] = field(default_factory=set)
    last_error: Optional[str] = None


class ClaudeBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.monitor = MonitorState()

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=20)
        self.http_session = aiohttp.ClientSession(timeout=timeout)
        await self.tree.sync()
        print("Synced slash commands globally.", flush=True)

    async def close(self) -> None:
        if self.http_session:
            await self.http_session.close()
        await super().close()

    async def fetch_usage(self) -> dict[str, Any] | str:
        """Returns parsed JSON dict on success, or an error string on failure."""
        if not self.http_session:
            return "HTTP session is not ready yet. Try again in a few seconds."
        if not CLAUDE_ORG_ID:
            return "Missing CLAUDE_ORG_ID in environment."
        if not CLAUDE_SESSION_KEY:
            return "Missing CLAUDE_SESSION_KEY in environment."

        cookies = make_cookies()
        try:
            async with self.http_session.get(USAGE_URL, headers=HEADERS, cookies=cookies) as resp:
                if resp.status in (401, 403):
                    return (
                        "Session key expired or invalid. Update `CLAUDE_SESSION_KEY` in `.env` "
                        "and restart."
                    )
                if resp.status != 200:
                    return f"API returned HTTP {resp.status}."
                try:
                    return await resp.json(content_type=None)
                except aiohttp.ContentTypeError:
                    return "API did not return valid JSON."
        except aiohttp.ClientError as exc:
            return f"Network error: {exc}"

    async def get_messageable_channel(self, channel_id: int) -> Optional[discord.abc.Messageable]:
        channel = self.get_channel(channel_id)
        if channel and isinstance(channel, discord.abc.Messageable):
            return channel
        try:
            fetched = await self.fetch_channel(channel_id)
        except discord.DiscordException:
            return None
        return fetched if isinstance(fetched, discord.abc.Messageable) else None


bot = ClaudeBot()


def _bucket_value(data: dict[str, Any], key: str) -> Optional[dict[str, Any]]:
    bucket = data.get(key)
    return bucket if isinstance(bucket, dict) else None


def _format_bucket(bucket: dict[str, Any]) -> tuple[float, str, str]:
    pct = normalize_pct(bucket.get("utilization", 0))
    bar = progress_bar(pct)
    reset = iso_to_discord_ts(bucket.get("resets_at", ""))
    return pct, bar, reset


def build_usage_embed(data: dict[str, Any]) -> discord.Embed:
    max_pct = 0.0
    five = _bucket_value(data, "five_hour")
    seven = _bucket_value(data, "seven_day")

    for bucket in (five, seven):
        if bucket:
            pct, _, _ = _format_bucket(bucket)
            max_pct = max(max_pct, pct)

    embed = discord.Embed(
        title="Claude Usage",
        color=usage_color(max_pct),
        timestamp=datetime.now(timezone.utc),
    )

    if five:
        pct, bar, reset = _format_bucket(five)
        embed.add_field(
            name="5-Hour Window",
            value=f"`{bar}` **{pct:.0f}%**\nResets {reset}",
            inline=False,
        )

    if seven:
        pct, bar, reset = _format_bucket(seven)
        embed.add_field(
            name="7-Day Window",
            value=f"`{bar}` **{pct:.0f}%**\nResets {reset}",
            inline=False,
        )

    model_buckets = {
        "seven_day_sonnet": "Sonnet (7d)",
        "seven_day_opus": "Opus (7d)",
        "seven_day_cowork": "Cowork (7d)",
    }
    for key, label in model_buckets.items():
        bucket = _bucket_value(data, key)
        if bucket:
            pct, bar, reset = _format_bucket(bucket)
            embed.add_field(
                name=label,
                value=f"`{bar}` **{pct:.0f}%**\nResets {reset}",
                inline=True,
            )

    extra = data.get("extra_usage")
    if extra is not None:
        embed.add_field(name="Extra Usage", value=str(extra), inline=False)

    embed.set_footer(text="claude.ai/settings/usage")
    return embed


@bot.tree.command(name="usage", description="Check your Claude session usage limits")
async def usage_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    result = await bot.fetch_usage()
    if isinstance(result, str):
        await interaction.followup.send(f"**Error:** {result}")
        return
    embed = build_usage_embed(result)
    await interaction.followup.send(embed=embed)


@bot.tree.command(
    name="check",
    description="Start monitoring usage and get pinged when it exceeds a threshold",
)
@app_commands.describe(
    threshold="Usage percentage to alert at (1-100, default 80)",
    interval="Check interval in minutes (default 5)",
)
async def check_command(
    interaction: discord.Interaction,
    threshold: int = 80,
    interval: int = 5,
) -> None:
    threshold = max(1, min(100, threshold))
    interval = max(1, min(60, interval))

    bot.monitor = MonitorState(
        active=True,
        threshold=threshold,
        interval_minutes=interval,
        channel_id=interaction.channel_id,
        user_id=interaction.user.id,
    )

    monitor_loop.change_interval(minutes=interval)
    if not monitor_loop.is_running():
        monitor_loop.start()

    embed = discord.Embed(
        title="Monitoring Started",
        color=0x5865F2,
        description=(
            f"Checking every **{interval} min**\n"
            f"Alert threshold: **{threshold}%**\n"
            "You'll be pinged in this channel when usage exceeds the threshold."
        ),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stopcheck", description="Stop usage monitoring")
async def stopcheck_command(interaction: discord.Interaction) -> None:
    if monitor_loop.is_running():
        monitor_loop.cancel()
    bot.monitor = MonitorState()
    await interaction.response.send_message("Monitoring stopped.")


@tasks.loop(minutes=5)
async def monitor_loop() -> None:
    m = bot.monitor
    if not m.active or not m.channel_id or not m.user_id:
        return

    result = await bot.fetch_usage()
    if isinstance(result, str):
        # Avoid posting the same error repeatedly on every cycle.
        if result != m.last_error:
            channel = await bot.get_messageable_channel(m.channel_id)
            if channel:
                await channel.send(f"**Monitor error:** {result}")
        m.last_error = result
        return
    m.last_error = None

    breached: list[tuple[str, float, str]] = []
    checks = {
        "5-Hour": _bucket_value(result, "five_hour"),
        "7-Day": _bucket_value(result, "seven_day"),
        "Sonnet 7d": _bucket_value(result, "seven_day_sonnet"),
        "Opus 7d": _bucket_value(result, "seven_day_opus"),
        "Cowork 7d": _bucket_value(result, "seven_day_cowork"),
    }

    for label, bucket in checks.items():
        if not bucket:
            continue
        pct = normalize_pct(bucket.get("utilization", 0))
        if pct >= m.threshold:
            if label not in m.alerted_keys:
                breached.append((label, pct, str(bucket.get("resets_at", ""))))
                m.alerted_keys.add(label)
        else:
            m.alerted_keys.discard(label)

    if breached:
        channel = await bot.get_messageable_channel(m.channel_id)
        if not channel:
            return

        lines = []
        for label, pct, resets in breached:
            reset_ts = iso_to_discord_ts(resets)
            lines.append(f"**{label}**: {pct:.0f}% (resets {reset_ts})")

        embed = discord.Embed(
            title="Usage Threshold Exceeded",
            color=0xED4245,
            description="\n".join(lines),
        )
        await channel.send(f"<@{m.user_id}>", embed=embed)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (ID: {bot.user.id})", flush=True)


def validate_env() -> None:
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not CLAUDE_SESSION_KEY:
        missing.append("CLAUDE_SESSION_KEY")
    if not CLAUDE_ORG_ID:
        missing.append("CLAUDE_ORG_ID")
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")


if __name__ == "__main__":
    validate_env()
    bot.run(DISCORD_TOKEN)
