import discord
from discord.ext import commands, tasks
import feedparser
from database import SessionLocal, NewsLog, GuildConfig
from bs4 import BeautifulSoup
import html
import datetime
from utils.batch_cache import load_cache, save_cache, should_flush as should_flush_cache, mark_flushed

FEEDS = [
    {"name": "The Hacker News", "url": "https://feeds.feedburner.com/TheHackersNews", "color": 0x228B22},
    {"name": "Bleeping Computer", "url": "https://www.bleepingcomputer.com/feed/", "color": 0x1E90FF},
    {"name": "Latest CVEs", "url": "https://cvefeed.io/rssfeed/latest.xml", "color": 0xFF4500}
]
CACHE_NAME = "news_cache"
FLUSH_INTERVAL_HOURS = 6


class NewsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.news_loop.start()
        
    def cog_unload(self):
        self.news_loop.cancel()

    def _flush_cache_to_db(self, db):
        cache_data = load_cache(CACHE_NAME)
        entries = cache_data.get("entries", []) or []
        if not entries:
            return False

        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))
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

        mark_flushed(CACHE_NAME, now)
        return True

    @tasks.loop(minutes=30)
    async def news_loop(self):
        await self.bot.wait_until_ready()

        db = SessionLocal()
        try:
            configs = db.query(GuildConfig).all()
            enabled_configs = [
                config for config in configs
                if config.cve_and_news_enabled and config.cve_and_news_channel
            ]

            if not enabled_configs:
                return

            cache_data = load_cache(CACHE_NAME)
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))
            should_flush_now = should_flush_cache(CACHE_NAME, FLUSH_INTERVAL_HOURS)

            for feed_info in FEEDS:
                feed = feedparser.parse(feed_info["url"])
                for entry in list(reversed(feed.entries))[-5:]:
                    link = getattr(entry, "link", None)
                    if not link:
                        continue

                    existing_links = {item.get("link") for item in cache_data.get("entries", []) if item.get("link")}
                    if link in existing_links:
                        continue

                    title = getattr(entry, "title", "No Title")
                    summary = getattr(entry, "summary", getattr(entry, "description", ""))
                    published = getattr(entry, "published", None)

                    cache_data.setdefault("entries", []).append({
                        "link": link,
                        "title": title,
                        "summary": summary,
                        "published": published,
                        "source_name": feed_info["name"],
                        "posted_at": now.isoformat(),
                    })

                    soup = BeautifulSoup(summary, "html.parser")
                    clean_summary = soup.get_text()[:400] + "..." if len(soup.get_text()) > 400 else soup.get_text()
                    clean_summary = html.unescape(clean_summary)

                    embed = discord.Embed(
                        title=title[:256],
                        url=link,
                        description=clean_summary,
                        color=feed_info["color"]
                    )
                    embed.set_author(name=feed_info["name"])

                    if published:
                        embed.set_footer(text=published)

                    for config in enabled_configs:
                        guild = self.bot.get_guild(int(config.guild_id))
                        if not guild:
                            continue
                        channel = guild.get_channel(int(config.cve_and_news_channel))
                        if not channel:
                            continue
                        try:
                            await channel.send(embed=embed)
                        except discord.Forbidden:
                            pass

            save_cache(CACHE_NAME, cache_data)

            if should_flush_now:
                self._flush_cache_to_db(db)
        except Exception as e:
            print(f"Error fetching news: {e}")
        finally:
            db.close()

async def setup(bot):
    await bot.add_cog(NewsCog(bot))
