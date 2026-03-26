from __future__ import annotations

import asyncio
import difflib
import logging
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
    "format": "bestaudio[acodec=opus]/bestaudio/best",
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

FFMPEG_BEFORE = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    "-analyzeduration 3000000 -probesize 500000"
)

FFMPEG_OPTS: dict = {
    "before_options": FFMPEG_BEFORE,
    "options": "-vn",
    "bitrate": 192,
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


AUTOPLAY_PRESETS: dict[str, str] = {
    "애니": "anime OST",
    "팝": "pop music",
    "록": "rock music",
    "재즈": "jazz",
    "클래식": "classical music",
    "힙합": "hip hop",
    "R&B": "R&B soul",
    "EDM": "EDM electronic",
    "발라드": "Korean ballad",
    "JPOP": "J-POP Japanese",
    "KPOP": "K-POP Korean",
    "게임": "game OST soundtrack",
    "로파이": "lofi chill",
}


@dataclass
class HistoryEntry:
    url: str
    artist: str
    song_name: str


@dataclass
class GuildState:
    queue: list[Song] = field(default_factory=list)
    current: Song | None = None
    loop: int = LoopMode.OFF
    autoplay: bool = True
    autoplay_tag: str = ""
    artist_variety: bool = True
    history: list[HistoryEntry] = field(default_factory=list)
    _inactivity_task: asyncio.Task | None = field(default=None, repr=False)
    _prefetch_task: asyncio.Task | None = field(default=None, repr=False)
    _prefetched_song: Song | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Similarity helpers
# ---------------------------------------------------------------------------

_TITLE_SEPARATORS = re.compile(r"\s*[-–—_/|]\s*")

SONG_SIMILARITY_THRESHOLD = 0.75
ARTIST_SIMILARITY_THRESHOLD = 0.80


def _parse_artist_title(raw_title: str) -> tuple[str, str]:
    """Try to split a YouTube title into (artist, song_name).
    Returns ("", cleaned_title) if parsing fails."""
    cleaned = re.sub(r"\[.*?]|\(.*?\)", "", raw_title)
    cleaned = re.sub(r"(?i)(official|music|video|mv|lyrics?|audio|hd|4k|feat\.?)", "", cleaned)
    cleaned = cleaned.strip()

    parts = _TITLE_SEPARATORS.split(cleaned, maxsplit=1)
    if len(parts) == 2 and len(parts[0].strip()) > 1 and len(parts[1].strip()) > 1:
        return parts[0].strip(), parts[1].strip()
    return "", cleaned


def _normalize(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower())


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ydl_extract = yt_dlp.YoutubeDL(YDL_EXTRACT_OPTS)
_ydl_search = yt_dlp.YoutubeDL(YDL_SEARCH_OPTS)


async def extract_song(query: str, *, requester: discord.Member | None, loop: asyncio.AbstractEventLoop) -> Song:
    """Extract song info from a URL or search query."""
    data = await loop.run_in_executor(None, lambda: _ydl_extract.extract_info(query, download=False))
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
    data = await loop.run_in_executor(None, lambda: _ydl_search.extract_info(query, download=False))
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
    """Presents numbered buttons (1-5) for the user to pick a search result.
    Also accepts chat input (1-5 or 'c' to cancel) in parallel."""

    def __init__(self, results: list[dict], requester: discord.Member, cog: Music, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.results = results
        self.requester = requester
        self.cog = cog
        self.picked: dict | None = None
        self.interaction_response: discord.Interaction | None = None
        self._message_task: asyncio.Task | None = None

        for i in range(min(len(results), 5)):
            self.add_item(SearchButton(index=i, label=str(i + 1)))

    def start_message_listener(self, bot: commands.Bot, channel: discord.abc.Messageable) -> None:
        """Start listening for chat input alongside button clicks."""
        self._message_task = asyncio.create_task(self._wait_for_message(bot, channel))

    async def _wait_for_message(self, bot: commands.Bot, channel: discord.abc.Messageable) -> None:
        def check(m: discord.Message) -> bool:
            if m.author.id != self.requester.id or m.channel.id != channel.id:
                return False
            return m.content.strip() in ("1", "2", "3", "4", "5", "c", "cancel")

        try:
            msg = await bot.wait_for("message", check=check, timeout=self.timeout)
        except asyncio.TimeoutError:
            return

        content = msg.content.strip().lower()
        if content in ("c", "cancel"):
            self.picked = None
        else:
            idx = int(content) - 1
            if 0 <= idx < len(self.results):
                self.picked = self.results[idx]

        self._disable_buttons()
        if self.interaction_response:
            try:
                await self.interaction_response.edit_original_response(view=self)
            except Exception:
                pass
        self.stop()

    def _disable_buttons(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[union-attr]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("요청자만 선택할 수 있습니다.", ephemeral=True)
            return False
        return True

    def _cancel_message_task(self) -> None:
        if self._message_task and not self._message_task.done():
            self._message_task.cancel()

    async def on_timeout(self) -> None:
        self._cancel_message_task()
        self._disable_buttons()
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
        self.view._cancel_message_task()
        self.view._disable_buttons()
        await interaction.response.edit_message(view=self.view)
        self.view.stop()


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

        # Autoplay – use prefetched song or search now
        if state.autoplay and state.current:
            if state._prefetched_song:
                auto_song = state._prefetched_song
                state._prefetched_song = None
            else:
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
        artist, song_name = _parse_artist_title(song.title)
        state.history.append(HistoryEntry(url=song.web_url, artist=artist, song_name=song_name))
        if len(state.history) > 50:
            state.history = state.history[-50:]

        self._cancel_inactivity_timer(state)
        vc.play(source, after=lambda e: self._play_next(guild, e))

        if state.autoplay and not state.queue:
            self._schedule_prefetch(guild)

    # ------------------------------------------------------------------
    # Autoplay – similar song discovery
    # ------------------------------------------------------------------

    async def _find_similar(self, song: Song, state: GuildState) -> Song | None:
        keywords = _keywords_from_title(song.title)
        if not keywords.strip():
            return None
        if state.autoplay_tag:
            keywords = f"{keywords} {state.autoplay_tag}"
        try:
            results = await search_youtube(keywords, loop=self.bot.loop)
        except Exception:
            return None

        history_urls = {h.url for h in state.history}
        candidates = []

        for r in results:
            if r["url"] in history_urls:
                continue

            cand_artist, cand_song_name = _parse_artist_title(r["title"])

            # Same song check (always block) – compare against all history
            if any(
                _similarity(cand_song_name, h.song_name) >= SONG_SIMILARITY_THRESHOLD
                for h in state.history
                if h.song_name
            ):
                continue

            # Artist variety check – compare against last 5 songs
            if state.artist_variety and cand_artist:
                recent = state.history[-5:]
                if any(
                    h.artist and _similarity(cand_artist, h.artist) >= ARTIST_SIMILARITY_THRESHOLD
                    for h in recent
                ):
                    continue

            candidates.append(r)

        if not candidates:
            candidates = [r for r in results if r["url"] not in history_urls]
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
    # Prefetch – search next song before current one ends
    # ------------------------------------------------------------------

    def _cancel_prefetch(self, state: GuildState) -> None:
        if state._prefetch_task and not state._prefetch_task.done():
            state._prefetch_task.cancel()
        state._prefetched_song = None

    def _schedule_prefetch(self, guild: discord.Guild) -> None:
        state = self._state(guild)
        self._cancel_prefetch(state)
        song = state.current
        if not song or song.duration < 20 or not state.autoplay:
            return
        if state.loop == LoopMode.SINGLE:
            return
        state._prefetch_task = asyncio.create_task(self._prefetch_worker(guild, song))

    async def _prefetch_worker(self, guild: discord.Guild, song: Song) -> None:
        delay = max(song.duration - 15, 0)
        await asyncio.sleep(delay)

        state = self._state(guild)
        if state.queue or not state.autoplay or state.current != song:
            return

        auto_song = await self._find_similar(song, state)
        if auto_song:
            state._prefetched_song = auto_song
            log.info("Prefetched next song: %s", auto_song.title)

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
            embed.set_footer(text="30초 내에 버튼을 클릭하거나 번호를 채팅으로 입력하세요. (취소: c)")

            view = SearchSelectView(results[:5], interaction.user, self)
            msg = await interaction.followup.send(embed=embed, view=view)
            view.interaction_response = interaction
            view.start_message_listener(self.bot, interaction.channel)

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
        state = self._state(interaction.guild)
        state.queue.clear()
        state.current = None
        state.loop = LoopMode.OFF
        self._cancel_prefetch(state)
        vc.stop()
        await interaction.response.send_message("⏹️ 재생이 중지되고 대기열이 초기화되었습니다.")

    # ------------------------------------------------------------------
    # Skip
    # ------------------------------------------------------------------

    @app_commands.command(name="skip", description="현재 노래를 건너뜁니다.")
    async def skip(self, interaction: discord.Interaction) -> None:
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            await interaction.response.send_message("재생 중인 노래가 없습니다.", ephemeral=True)
            return
        vc.stop()
        await interaction.response.send_message("⏭️ 다음 곡으로 넘어갑니다.")

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
        tag_info = f" ({state.autoplay_tag})" if state.autoplay_tag else ""
        variety_label = "켜짐" if state.artist_variety else "꺼짐"
        embed.set_footer(text=f"반복: {loop_label} | 자동재생: {autoplay_label}{tag_info} | 다양성: {variety_label} | 총 {len(state.queue)}곡 대기 중")
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

    @app_commands.command(name="autoplay", description="자동재생을 켜거나 끄고, 선호 장르를 설정합니다.")
    @app_commands.describe(genre="선호 장르 (비워두면 토글, 'off'=태그해제, 'variety'=같은가수 차단 토글)")
    async def autoplay(self, interaction: discord.Interaction, genre: str | None = None) -> None:
        state = self._state(interaction.guild)

        if genre is None:
            state.autoplay = not state.autoplay
            label = "켜짐" if state.autoplay else "꺼짐"
            tag_info = f" (장르: {state.autoplay_tag})" if state.autoplay_tag else ""
            variety_label = "켜짐" if state.artist_variety else "꺼짐"
            await interaction.response.send_message(
                f"📻 자동재생: **{label}**{tag_info} | 가수 다양성: **{variety_label}**"
            )
            return

        genre_input = genre.strip()

        if genre_input.lower() == "off":
            state.autoplay_tag = ""
            await interaction.response.send_message("📻 자동재생 장르 태그가 해제되었습니다.")
            return

        if genre_input.lower() == "variety":
            state.artist_variety = not state.artist_variety
            label = "켜짐" if state.artist_variety else "꺼짐"
            await interaction.response.send_message(
                f"📻 가수 다양성: **{label}**\n"
                f"{'연속으로 같은 가수의 곡이 추천되지 않습니다.' if state.artist_variety else '같은 가수의 곡도 자유롭게 추천됩니다.'}"
            )
            return

        resolved = AUTOPLAY_PRESETS.get(genre_input, genre_input)
        state.autoplay_tag = resolved
        state.autoplay = True

        preset_list = ", ".join(f"`{k}`" for k in AUTOPLAY_PRESETS)
        await interaction.response.send_message(
            f"📻 자동재생: **켜짐** | 장르: **{genre_input}** (`{resolved}`)\n"
            f"사용 가능한 프리셋: {preset_list}\n"
            f"프리셋 외 직접 입력도 가능합니다. | `variety`로 가수 다양성 토글"
        )

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
        embed.set_footer(text="30초 내에 버튼을 클릭하거나 번호를 채팅으로 입력하세요. (취소: c)")

        view = SearchSelectView(results[:5], interaction.user, self)
        await interaction.followup.send(embed=embed, view=view)
        view.interaction_response = interaction
        view.start_message_listener(self.bot, interaction.channel)

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
