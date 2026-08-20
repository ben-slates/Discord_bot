import asyncio
import aiohttp
import discord
from discord.ext import commands, tasks
import feedparser
from database import SessionLocal, NewsLog, GuildConfig
from utils.db_executor import run_db_profiled
from bs4 import BeautifulSoup
import html
import datetime
import logging
import time
from utils.batch_cache import load_cache, save_cache, should_flush as should_flush_cache, mark_flushed

FEEDS = [
    {"name": "The Hacker News", "url": "https://feeds.feedburner.com/TheHackersNews", "color": 0x228B22},
    {"name": "Latest CVEs", "url": "https://cvefeed.io/rssfeed/latest.xml", "color": 0xFF4500}
]
CACHE_NAME = "news_cache"
FLUSH_INTERVAL_HOURS = 6


class NewsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._news_started = False
        self._http_session: aiohttp.ClientSession | None = None

    def cog_unload(self):
        # Cancel the loop if it exists
        try:
            if hasattr(self, 'news_loop') and self.news_loop.is_running():
                self.news_loop.cancel()
        except Exception:
            pass
        if self._http_session and not self._http_session.closed:
            asyncio.create_task(self._http_session.close())

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(total=20, connect=5, sock_read=15)
            self._http_session = aiohttp.ClientSession(timeout=timeout)
        return self._http_session

    @commands.Cog.listener()
    async def on_ready(self):
        # Start the loop once the bot is ready
        if not getattr(self, "_news_started", False):
            try:
                if not self.news_loop.is_running():
                    self.news_loop.start()
                self._news_started = True
            except Exception:
                pass

    def _flush_cache_to_db(self):
        cache_data = load_cache(CACHE_NAME)
        entries = cache_data.get("entries", []) or []
        if not entries:
            return False
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))
        db = SessionLocal()
        try:
            for entry in entries:
                link = entry.get("link")
                if not link:
                    continue
                exists = db.query(NewsLog).filter_by(link=link).first()
                if exists:
                    continue
                posted_at = entry.get("posted_at")
                if posted_at:
                    try:
                        posted_dt = datetime.datetime.fromisoformat(posted_at)
                    except ValueError:
                        posted_dt = now
                else:
                    posted_dt = now
                db.add(NewsLog(link=link, posted_at=posted_dt))

            cutoff = now - datetime.timedelta(days=7)
            old_logs = db.query(NewsLog).filter(NewsLog.posted_at < cutoff).all()
            for old_log in old_logs:
                db.delete(old_log)
            db.commit()
        finally:
            db.close()

        mark_flushed(CACHE_NAME, now)
        return True
def _news_exists(link: str) -> bool:
    db = SessionLocal()
    try:
        return db.query(NewsLog).filter_by(link=link).first() is not None
    finally:
        db.close()


def _existing_news_links(links: list[str]) -> set[str]:
    if not links:
        return set()
    db = SessionLocal()
    try:
        return {link for (link,) in db.query(NewsLog.link).filter(NewsLog.link.in_(links)).all()}
    finally:
        db.close()


def _fetch_enabled_configs():
    db = SessionLocal()
    try:
        return [
            (config.guild_id, config.cve_and_news_channel)
            for config in db.query(GuildConfig).all()
            if config.cve_and_news_enabled and config.cve_and_news_channel
        ]
    finally:
        db.close()


    # news_loop will be attached to the class below

    # `/send_news` admin command removed per user request.

async def setup(bot):
    await bot.add_cog(NewsCog(bot))

@tasks.loop(minutes=30)
async def _news_loop(self):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await self.bot.wait_until_ready()

    try:
        profile_started = time.perf_counter()
        # Load guild configs using the bounded DB executor to avoid connection storms
        enabled_configs = await run_db_profiled("news.config", _fetch_enabled_configs)
        config_elapsed = time.perf_counter() - profile_started

        if not enabled_configs:
            return

        cache_data, should_flush_now = await asyncio.gather(
            asyncio.to_thread(load_cache, CACHE_NAME),
            asyncio.to_thread(should_flush_cache, CACHE_NAME, FLUSH_INTERVAL_HOURS),
        )
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))

        async def fetch_feed(feed_info):
            session = await self._get_http_session()
            started = time.perf_counter()
            try:
                async with session.get(feed_info["url"], headers={"User-Agent": "RynexSecurityBot/1.0"}) as response:
                    response.raise_for_status()
                    payload = await response.read()
                parsed = await asyncio.to_thread(feedparser.parse, payload)
                return feed_info, parsed, time.perf_counter() - started
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logging.warning("News feed %s failed: %s", feed_info["name"], exc)
                return feed_info, None, time.perf_counter() - started

        feed_results = await asyncio.gather(*(fetch_feed(feed_info) for feed_info in FEEDS))
        feed_elapsed = time.perf_counter() - profile_started - config_elapsed
        candidate_entries = []
        cached_links = {item.get("link") for item in cache_data.get("entries", []) if item.get("link")}
        for feed_info, feed, elapsed in feed_results:
            if elapsed >= 1:
                logging.info("News feed %s completed in %.3fs", feed_info["name"], elapsed)
            if feed is None:
                continue
            for entry in list(reversed(feed.entries))[-5:]:
                link = getattr(entry, "link", None)
                if not link or link in cached_links:
                    continue
                candidate_entries.append((feed_info, entry, link))

        persisted_links = await run_db_profiled("news.dedupe", _existing_news_links, [item[2] for item in candidate_entries])
        db_elapsed = time.perf_counter() - profile_started - config_elapsed - feed_elapsed
        pending_sends = []
        send_semaphore = asyncio.Semaphore(5)
        async def send_embed(channel, embed):
            async with send_semaphore:
                try:
                    await channel.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException):
                    logging.debug("Unable to post news to channel %s", channel.id, exc_info=True)

        for feed_info, entry, link in candidate_entries:
            if link in persisted_links:
                continue
            title = getattr(entry, "title", "No Title")
            summary = getattr(entry, "summary", getattr(entry, "description", ""))
            published = getattr(entry, "published", None)

            cache_data.setdefault("entries", []).append({
                "link": link, "title": title, "summary": summary, "published": published,
                "source_name": feed_info["name"], "posted_at": now.isoformat(),
            })

            clean_summary = await asyncio.to_thread(_clean_summary, summary)
            embed = discord.Embed(title=title[:256], url=link, description=clean_summary, color=feed_info["color"])
            embed.set_author(name=feed_info["name"])
            if published:
                embed.set_footer(text=published)

            for guild_id, channel_id in enabled_configs:
                guild = self.bot.get_guild(int(guild_id))
                channel = guild.get_channel(int(channel_id)) if guild else None
                if channel:
                    pending_sends.append(send_embed(channel, embed))

        if pending_sends:
            await asyncio.gather(*pending_sends)
        send_elapsed = time.perf_counter() - profile_started - config_elapsed - feed_elapsed - db_elapsed

        await asyncio.to_thread(save_cache, CACHE_NAME, cache_data)

        if should_flush_now:
            await run_db_profiled("news.flush_cache", self._flush_cache_to_db)
        logging.warning(
            "News timing: config_db=%.3fs feeds=%.3fs dedupe_db=%.3fs sends=%.3fs remaining=%.3fs",
            config_elapsed, feed_elapsed, db_elapsed, send_elapsed,
            time.perf_counter() - profile_started - config_elapsed - feed_elapsed - db_elapsed - send_elapsed,
        )
    except Exception:
        logging.exception("Error fetching news")
    finally:
        # Log slow iterations of the news loop for post-mortem diagnostics
        try:
            t1 = loop.time()
            dt = t1 - t0
            if dt >= 1.0:  # only log if the iteration took >= 1s
                logging.warning(f"News loop iteration took {dt:.3f}s")
        except Exception:
            pass


def _clean_summary(summary: str) -> str:
    text = BeautifulSoup(summary, "html.parser").get_text()
    return html.unescape(text[:400] + "..." if len(text) > 400 else text)

def attach_news_loop_to_cog():
    # Attach the loop as a method to NewsCog class so it can be started/stopped normally
    setattr(NewsCog, 'news_loop', _news_loop)

attach_news_loop_to_cog()
