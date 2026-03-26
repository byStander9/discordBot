from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import os
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import yt_dlp

if TYPE_CHECKING:
    from bot import MusicBot

log = logging.getLogger("cogs.music")

YDL_BASE_OPTS: dict = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "source_address": "0.0.0.0",
}

YDL_SEARCH_OPTS: dict = {**YDL_BASE_OPTS, "default_search": "ytsearch5", "extract_flat": "in_playlist"}

YDL_EXTRACT_OPTS: dict = {
    **YDL_BASE_OPTS,
    "format": "bestaudio/best",
    "default_search": "ytsearch",
}

def _find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found

    import glob
    search_patterns = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages", "*FFmpeg*", "**", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        os.path.join(os.environ.get("USERPROFILE", ""), "scoop", "shims", "ffmpeg.exe"),
    ]
    for pattern in search_patterns:
        if "*" in pattern or "?" in pattern:
            matches = glob.glob(pattern, recursive=True)
            if matches:
                return matches[0]
        elif os.path.isfile(pattern):
            return pattern

    log.warning("FFmpeg not found! Audio playback will not work.")
    return "ffmpeg"


FFMPEG_PATH: str = _find_ffmpeg()

FFMPEG_OPTS: dict = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

INACTIVITY_TIMEOUT = 180  # seconds

_URL_PATTERN = re.compile(r"^https?://")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Song:
    title: str
    url: str
    web_url: str
    duration: int  # seconds
    requester: discord.Member | None
    is_autoplay: bool = False

    @property
    def duration_str(self) -> str:
        m, s = divmod(self.duration, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class LoopMode:
    OFF = 0
    SINGLE = 1
    ALL = 2
    _labels = {0: "끄기", 1: "한 곡 반복", 2: "전체 반복"}

    @classmethod
    def label(cls, mode: int) -> str:
        return cls._labels.get(mode, "알 수 없음")


@dataclass
class GuildState:
    queue: list[Song] = field(default_factory=list)
    current: Song | None = None
    loop: int = LoopMode.OFF
    autoplay: bool = True
    skip_votes: set[int] = field(default_factory=set)
    history: list[str] = field(default_factory=list)
    _inactivity_task: asyncio.Task | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def extract_song(query: str, *, requester: discord.Member | None, loop: asyncio.AbstractEventLoop) -> Song:
    """Extract song info from a URL or search query."""
    with yt_dlp.YoutubeDL(YDL_EXTRACT_OPTS) as ydl:
        data = await loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))
    if "entries" in data:
        data = data["entries"][0]
    return Song(
        title=data.get("title", "Unknown"),
        url=data["url"],
        web_url=data.get("webpage_url", query),
        duration=data.get("duration", 0) or 0,
        requester=requester,
    )


async def search_youtube(query: str, *, loop: asyncio.AbstractEventLoop) -> list[dict]:
    """Return flat search results (title, url, duration)."""
    with yt_dlp.YoutubeDL(YDL_SEARCH_OPTS) as ydl:
        data = await loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))
    entries = list(data.get("entries", []))
    return [
        {
            "title": e.get("title", "Unknown"),
            "url": e.get("url") or e.get("webpage_url", ""),
            "duration": int(e.get("duration", 0) or 0),
        }
        for e in entries
        if e
    ]


def _keywords_from_title(title: str) -> str:
    """Strip noise from a song title to build a search query for similar songs."""
    cleaned = re.sub(r"\[.*?]|\(.*?\)", "", title)
    cleaned = re.sub(r"(?i)(official|music|video|mv|lyrics?|audio|hd|4k|feat\.?)", "", cleaned)
    cleaned = re.sub(r"[^\w\s]", "", cleaned)
    tokens = cleaned.split()
    return " ".join(tokens[:6])


# ---------------------------------------------------------------------------
# UI Views
# ---------------------------------------------------------------------------

class SearchSelectView(discord.ui.View):
    """Presents numbered buttons (1-5) for the user to pick a search result."""

    def __init__(self, results: list[dict], requester: discord.Member, cog: Music, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.results = results
        self.requester = requester
        self.cog = cog
        self.picked: dict | None = None
        self.interaction_response: discord.Interaction | None = None

        for i in range(min(len(results), 5)):
            self.add_item(SearchButton(index=i, label=str(i + 1)))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("요청자만 선택할 수 있습니다.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[union-attr]
        if self.interaction_response:
            try:
                await self.interaction_response.edit_original_response(view=self)
            except Exception:
                pass


class SearchButton(discord.ui.Button["SearchSelectView"]):
    def __init__(self, index: int, label: str):
        super().__init__(style=discord.ButtonStyle.primary, label=label)
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view.picked = self.view.results[self.index]
        for child in self.view.children:
            child.disabled = True  # type: ignore[union-attr]
        await interaction.response.edit_message(view=self.view)
        self.view.stop()


class SkipVoteView(discord.ui.View):
    """A persistent button for skip voting."""

    def __init__(self, cog: Music, guild: discord.Guild, required: int, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.required = required
        self.voters: set[int] = set()
        self.resolved = False
        self.message: discord.Message | None = None

    def _vote_label(self) -> str:
        return f"스킵 투표 ({len(self.voters)}/{self.required})"

    @discord.ui.button(label="스킵 투표 (0/0)", style=discord.ButtonStyle.danger, emoji="⏭️")
    async def vote_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vc = self.guild.voice_client
        if not vc or not vc.channel:
            await interaction.response.send_message("봇이 음성 채널에 없습니다.", ephemeral=True)
            return

        if not interaction.user.voice or interaction.user.voice.channel != vc.channel:
            await interaction.response.send_message("같은 음성 채널에 있어야 투표할 수 있습니다.", ephemeral=True)
            return

        self.voters.add(interaction.user.id)

        # Recalculate required in case people left/joined
        humans = [m for m in vc.channel.members if not m.bot]
        self.required = math.ceil(len(humans) / 2)
        button.label = self._vote_label()

        if len(self.voters) >= self.required:
            self.resolved = True
            button.label = "스킵 투표 통과!"
            button.disabled = True
            button.style = discord.ButtonStyle.success
            await interaction.response.edit_message(view=self)
            self.stop()

            state = self.cog._state(self.guild)
            state.skip_votes.clear()
            if vc.is_playing():
                vc.stop()
        else:
            voter_names = []
            for vid in self.voters:
                member = self.guild.get_member(vid)
                if member:
                    voter_names.append(member.display_name)
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed:
                for i, f in enumerate(embed.fields):
                    if f.name == "투표 현황":
                        embed.set_field_at(i, name="투표 현황", value=", ".join(voter_names) or "-", inline=False)
                        break
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        if not self.resolved:
            for child in self.children:
                child.disabled = True  # type: ignore[union-attr]
                child.label = "투표 시간 만료"  # type: ignore[union-attr]
            if self.message:
                try:
                    await self.message.edit(view=self)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Music(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot
        self.states: dict[int, GuildState] = {}
        log.info("FFmpeg path: %s", FFMPEG_PATH)

    def _state(self, guild: discord.Guild) -> GuildState:
        if guild.id not in self.states:
            self.states[guild.id] = GuildState()
        return self.states[guild.id]

    def _reset_state(self, guild_id: int) -> None:
        self.states.pop(guild_id, None)

    # ------------------------------------------------------------------
    # Inactivity auto-leave
    # ------------------------------------------------------------------

    def _start_inactivity_timer(self, guild: discord.Guild) -> None:
        state = self._state(guild)
        self._cancel_inactivity_timer(state)
        state._inactivity_task = asyncio.create_task(self._inactivity_disconnect(guild))

    def _cancel_inactivity_timer(self, state: GuildState) -> None:
        if state._inactivity_task and not state._inactivity_task.done():
            state._inactivity_task.cancel()

    async def _inactivity_disconnect(self, guild: discord.Guild) -> None:
        await asyncio.sleep(INACTIVITY_TIMEOUT)
        vc = guild.voice_client
        if vc and not vc.is_playing():
            await vc.disconnect()
            self._reset_state(guild.id)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        vc = member.guild.voice_client
        if vc is None or vc.channel is None:
            return
        humans = [m for m in vc.channel.members if not m.bot]
        if len(humans) == 0:
            await asyncio.sleep(30)
            vc = member.guild.voice_client
            if vc and len([m for m in vc.channel.members if not m.bot]) == 0:
                await vc.disconnect()
                self._reset_state(member.guild.id)

    # ------------------------------------------------------------------
    # Internal playback engine
    # ------------------------------------------------------------------

    def _play_next(self, guild: discord.Guild, error: Exception | None = None) -> None:
        if error:
            log.error("Player error: %s", error)
        asyncio.run_coroutine_threadsafe(self._advance(guild), self.bot.loop)

    async def _advance(self, guild: discord.Guild) -> None:
        state = self._state(guild)
        vc: discord.VoiceClient | None = guild.voice_client
        if vc is None:
            return

        state.skip_votes.clear()

        # Loop single
        if state.loop == LoopMode.SINGLE and state.current:
            await self._start_playing(guild, state.current)
            return

        # Loop all – push current to end
        if state.loop == LoopMode.ALL and state.current:
            state.queue.append(state.current)

        if state.queue:
            song = state.queue.pop(0)
            await self._start_playing(guild, song)
            return

        # Autoplay – find a similar song
        if state.autoplay and state.current:
            auto_song = await self._find_similar(state.current, state)
            if auto_song:
                await self._start_playing(guild, auto_song)
                return

        state.current = None
        self._start_inactivity_timer(guild)

    async def _start_playing(self, guild: discord.Guild, song: Song) -> None:
        state = self._state(guild)
        vc: discord.VoiceClient = guild.voice_client  # type: ignore[assignment]

        try:
            source = discord.FFmpegOpusAudio(song.url, executable=FFMPEG_PATH, **FFMPEG_OPTS)
        except Exception:
            # Re-extract in case the stream URL expired
            try:
                fresh = await extract_song(
                    song.web_url, requester=song.requester, loop=self.bot.loop
                )
                song.url = fresh.url
                source = discord.FFmpegOpusAudio(song.url, executable=FFMPEG_PATH, **FFMPEG_OPTS)
            except Exception as exc:
                log.error("Failed to re-extract %s: %s", song.title, exc)
                await self._advance(guild)
                return

        state.current = song
        state.history.append(song.web_url)
        if len(state.history) > 30:
            state.history = state.history[-30:]

        self._cancel_inactivity_timer(state)
        vc.play(source, after=lambda e: self._play_next(guild, e))

    # ------------------------------------------------------------------
    # Autoplay – similar song discovery
    # ------------------------------------------------------------------

    async def _find_similar(self, song: Song, state: GuildState) -> Song | None:
        keywords = _keywords_from_title(song.title)
        if not keywords.strip():
            return None
        try:
            results = await search_youtube(keywords, loop=self.bot.loop)
        except Exception:
            return None

        # Filter out recently played songs
        history_set = set(state.history)
        candidates = [r for r in results if r["url"] not in history_set]
        if not candidates:
            candidates = results

        if not candidates:
            return None

        pick = random.choice(candidates[:3])
        try:
            auto_song = await extract_song(pick["url"], requester=None, loop=self.bot.loop)
            auto_song.is_autoplay = True
            return auto_song
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Embed helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now_playing_embed(song: Song) -> discord.Embed:
        embed = discord.Embed(
            title="Now Playing 🎶",
            description=f"[{song.title}]({song.web_url})",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="길이", value=song.duration_str, inline=True)
        if song.requester:
            embed.add_field(name="요청자", value=song.requester.mention, inline=True)
        if song.is_autoplay:
            embed.add_field(name="자동재생", value="유사곡 자동 선곡", inline=True)
        return embed

    # ------------------------------------------------------------------
    # Slash Commands
    # ------------------------------------------------------------------

    @app_commands.command(name="join", description="봇을 음성 채널에 입장시킵니다.")
    async def join(self, interaction: discord.Interaction) -> None:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("먼저 음성 채널에 입장해주세요.", ephemeral=True)
            return
        channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        if vc:
            await vc.move_to(channel)
        else:
            await channel.connect(self_deaf=True)
        await interaction.response.send_message(f"**{channel.name}** 채널에 입장했습니다.")

    @app_commands.command(name="leave", description="봇을 음성 채널에서 퇴장시킵니다.")
    async def leave(self, interaction: discord.Interaction) -> None:
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("봇이 음성 채널에 없습니다.", ephemeral=True)
            return
        self._reset_state(interaction.guild.id)
        await vc.disconnect()
        await interaction.response.send_message("음성 채널에서 퇴장했습니다.")

    @app_commands.command(name="play", description="노래를 재생하거나 대기열에 추가합니다.")
    @app_commands.describe(query="YouTube URL 또는 검색어")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("먼저 음성 채널에 입장해주세요.", ephemeral=True)
            return

        await interaction.response.defer()

        vc: discord.VoiceClient | None = interaction.guild.voice_client
        if not vc:
            vc = await interaction.user.voice.channel.connect(self_deaf=True)

        is_url = bool(_URL_PATTERN.match(query.strip()))

        if not is_url:
            # Search mode: show 5 results and let user pick via buttons
            results = await search_youtube(query, loop=self.bot.loop)
            if not results:
                await interaction.followup.send("검색 결과가 없습니다.")
                return

            lines: list[str] = []
            for i, r in enumerate(results[:5], 1):
                dur = r["duration"]
                m, s = divmod(dur, 60)
                lines.append(f"`{i}.` {r['title']} ({m:02d}:{s:02d})")

            embed = discord.Embed(
                title=f"🔍 검색 결과: {query}",
                description="\n".join(lines),
                color=discord.Color.blue(),
            )
            embed.set_footer(text="30초 내에 버튼을 눌러 선택하세요.")

            view = SearchSelectView(results[:5], interaction.user, self)
            msg = await interaction.followup.send(embed=embed, view=view)
            view.interaction_response = interaction

            timed_out = await view.wait()
            if timed_out or view.picked is None:
                return

            try:
                song = await extract_song(view.picked["url"], requester=interaction.user, loop=self.bot.loop)
            except Exception as exc:
                await interaction.followup.send(f"노래를 불러올 수 없습니다: {exc}")
                return
        else:
            try:
                song = await extract_song(query, requester=interaction.user, loop=self.bot.loop)
            except Exception as exc:
                await interaction.followup.send(f"노래를 찾을 수 없습니다: {exc}")
                return

        state = self._state(interaction.guild)

        if vc.is_playing() or vc.is_paused():
            state.queue.append(song)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="대기열 추가",
                    description=f"[{song.title}]({song.web_url}) ({song.duration_str})",
                    color=discord.Color.green(),
                ).set_footer(text=f"대기열 #{len(state.queue)}")
            )
        else:
            await self._start_playing(interaction.guild, song)
            await interaction.followup.send(embed=self._now_playing_embed(song))

    @app_commands.command(name="pause", description="현재 재생 중인 노래를 일시정지합니다.")
    async def pause(self, interaction: discord.Interaction) -> None:
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ 일시정지되었습니다.")
        else:
            await interaction.response.send_message("재생 중인 노래가 없습니다.", ephemeral=True)

    @app_commands.command(name="resume", description="일시정지된 노래를 다시 재생합니다.")
    async def resume(self, interaction: discord.Interaction) -> None:
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ 다시 재생합니다.")
        else:
            await interaction.response.send_message("일시정지 상태가 아닙니다.", ephemeral=True)

    @app_commands.command(name="stop", description="재생을 중지하고 대기열을 초기화합니다.")
    async def stop(self, interaction: discord.Interaction) -> None:
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("봇이 음성 채널에 없습니다.", ephemeral=True)
            return
        self._state(interaction.guild).queue.clear()
        self._state(interaction.guild).current = None
        self._state(interaction.guild).loop = LoopMode.OFF
        vc.stop()
        await interaction.response.send_message("⏹️ 재생이 중지되고 대기열이 초기화되었습니다.")

    # ------------------------------------------------------------------
    # Vote skip
    # ------------------------------------------------------------------

    @app_commands.command(name="skip", description="투표를 통해 현재 노래를 건너뜁니다.")
    async def skip(self, interaction: discord.Interaction) -> None:
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            await interaction.response.send_message("재생 중인 노래가 없습니다.", ephemeral=True)
            return

        if not interaction.user.voice or interaction.user.voice.channel != vc.channel:
            await interaction.response.send_message("같은 음성 채널에 있어야 합니다.", ephemeral=True)
            return

        humans = [m for m in vc.channel.members if not m.bot]
        required = math.ceil(len(humans) / 2)

        # 혼자 있는 경우 즉시 스킵
        if required <= 1:
            state = self._state(interaction.guild)
            state.skip_votes.clear()
            vc.stop()
            await interaction.response.send_message("⏭️ 다음 곡으로 넘어갑니다.")
            return

        state = self._state(interaction.guild)
        current_title = state.current.title if state.current else "현재 곡"

        view = SkipVoteView(cog=self, guild=interaction.guild, required=required, timeout=60)
        view.voters.add(interaction.user.id)
        view.vote_button.label = view._vote_label()

        embed = discord.Embed(
            title="⏭️ 스킵 투표",
            description=f"**{current_title}**\n\n"
                        f"채널 인원의 과반수({required}명)가 투표하면 스킵됩니다.\n"
                        f"아래 버튼을 클릭하거나 `/skip`을 입력해 투표하세요.",
            color=discord.Color.red(),
        )
        embed.add_field(name="투표 현황", value=interaction.user.display_name, inline=False)

        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        view.message = msg

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    @app_commands.command(name="queue", description="현재 대기열을 표시합니다.")
    async def queue(self, interaction: discord.Interaction) -> None:
        state = self._state(interaction.guild)
        if not state.current and not state.queue:
            await interaction.response.send_message("대기열이 비어있습니다.", ephemeral=True)
            return

        lines: list[str] = []
        if state.current:
            status = "🔂" if state.loop == LoopMode.SINGLE else "🎶"
            lines.append(f"{status} **현재 재생:** [{state.current.title}]({state.current.web_url}) ({state.current.duration_str})")
            lines.append("")

        if state.queue:
            for i, song in enumerate(state.queue[:15], 1):
                lines.append(f"`{i}.` [{song.title}]({song.web_url}) ({song.duration_str})")
            if len(state.queue) > 15:
                lines.append(f"\n... 외 **{len(state.queue) - 15}곡**")
        else:
            lines.append("_대기열이 비어있습니다._")

        embed = discord.Embed(
            title="대기열",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        loop_label = LoopMode.label(state.loop)
        autoplay_label = "켜짐" if state.autoplay else "꺼짐"
        embed.set_footer(text=f"반복: {loop_label} | 자동재생: {autoplay_label} | 총 {len(state.queue)}곡 대기 중")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="remove", description="대기열에서 특정 곡을 제거합니다.")
    @app_commands.describe(index="제거할 곡 번호 (1부터 시작)")
    async def remove(self, interaction: discord.Interaction, index: int) -> None:
        state = self._state(interaction.guild)
        if index < 1 or index > len(state.queue):
            await interaction.response.send_message(
                f"잘못된 번호입니다. 1~{len(state.queue)} 사이의 숫자를 입력해주세요.", ephemeral=True
            )
            return
        removed = state.queue.pop(index - 1)
        await interaction.response.send_message(f"🗑️ 대기열에서 제거: **{removed.title}**")

    # ------------------------------------------------------------------
    # Loop / Shuffle / Autoplay toggle
    # ------------------------------------------------------------------

    @app_commands.command(name="loop", description="반복 모드를 전환합니다.")
    @app_commands.choices(mode=[
        app_commands.Choice(name="끄기", value=0),
        app_commands.Choice(name="한 곡 반복", value=1),
        app_commands.Choice(name="전체 반복", value=2),
    ])
    async def loop(self, interaction: discord.Interaction, mode: int) -> None:
        state = self._state(interaction.guild)
        state.loop = mode
        await interaction.response.send_message(f"🔁 반복 모드: **{LoopMode.label(mode)}**")

    @app_commands.command(name="shuffle", description="대기열을 셔플합니다.")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        state = self._state(interaction.guild)
        if len(state.queue) < 2:
            await interaction.response.send_message("셔플할 곡이 부족합니다.", ephemeral=True)
            return
        random.shuffle(state.queue)
        await interaction.response.send_message(f"🔀 대기열 {len(state.queue)}곡을 셔플했습니다.")

    @app_commands.command(name="autoplay", description="대기열이 비었을 때 유사곡 자동재생을 켜거나 끕니다.")
    async def autoplay(self, interaction: discord.Interaction) -> None:
        state = self._state(interaction.guild)
        state.autoplay = not state.autoplay
        label = "켜짐" if state.autoplay else "꺼짐"
        await interaction.response.send_message(f"📻 자동재생: **{label}**")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    @app_commands.command(name="search", description="YouTube에서 노래를 검색합니다.")
    @app_commands.describe(query="검색어")
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("먼저 음성 채널에 입장해주세요.", ephemeral=True)
            return

        await interaction.response.defer()

        results = await search_youtube(query, loop=self.bot.loop)
        if not results:
            await interaction.followup.send("검색 결과가 없습니다.")
            return

        lines: list[str] = []
        for i, r in enumerate(results[:5], 1):
            dur = r["duration"]
            m, s = divmod(dur, 60)
            lines.append(f"`{i}.` {r['title']} ({m:02d}:{s:02d})")

        embed = discord.Embed(
            title=f"🔍 검색 결과: {query}",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.set_footer(text="30초 내에 버튼을 눌러 선택하세요.")

        view = SearchSelectView(results[:5], interaction.user, self)
        await interaction.followup.send(embed=embed, view=view)
        view.interaction_response = interaction

        timed_out = await view.wait()
        if timed_out or view.picked is None:
            return

        picked = view.picked
        try:
            song = await extract_song(picked["url"], requester=interaction.user, loop=self.bot.loop)
        except Exception as exc:
            await interaction.followup.send(f"노래를 불러올 수 없습니다: {exc}")
            return

        vc: discord.VoiceClient | None = interaction.guild.voice_client
        if not vc:
            vc = await interaction.user.voice.channel.connect(self_deaf=True)

        state = self._state(interaction.guild)
        if vc.is_playing() or vc.is_paused():
            state.queue.append(song)
            await interaction.followup.send(f"대기열에 추가: **{song.title}**")
        else:
            await self._start_playing(interaction.guild, song)
            await interaction.followup.send(embed=self._now_playing_embed(song))

    # ------------------------------------------------------------------
    # Now Playing
    # ------------------------------------------------------------------

    @app_commands.command(name="nowplaying", description="현재 재생 중인 곡 정보를 표시합니다.")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        state = self._state(interaction.guild)
        if not state.current:
            await interaction.response.send_message("재생 중인 노래가 없습니다.", ephemeral=True)
            return
        await interaction.response.send_message(embed=self._now_playing_embed(state.current))


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Music(bot))
