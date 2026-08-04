import discord
from discord.ext import commands, tasks
import feedparser
from database import SessionLocal, NewsLog, GuildConfig
from bs4 import BeautifulSoup
import html
import datetime

FEEDS = [
    {"name": "The Hacker News", "url": "https://feeds.feedburner.com/TheHackersNews", "color": 0x228B22},
    {"name": "Bleeping Computer", "url": "https://www.bleepingcomputer.com/feed/", "color": 0x1E90FF},
    {"name": "Latest CVEs", "url": "https://cvefeed.io/rssfeed/latest.xml", "color": 0xFF4500}
]
class NewsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.news_loop.start()
        
    def cog_unload(self):
        self.news_loop.cancel()
        
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

            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))
            cutoff = now - datetime.timedelta(days=7)
            db.query(NewsLog).filter(NewsLog.posted_at < cutoff).delete(synchronize_session=False)
            db.commit()

            for feed_info in FEEDS:
                feed = feedparser.parse(feed_info["url"])
                for entry in list(reversed(feed.entries))[-5:]:
                    link = getattr(entry, "link", None)
                    if not link:
                        continue

                    exists = db.query(NewsLog).filter_by(link=link).first()
                    if exists:
                        continue

                    db.add(NewsLog(link=link, posted_at=now))
                    db.commit()

                    title = getattr(entry, "title", "No Title")
                    summary = getattr(entry, "summary", getattr(entry, "description", ""))

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

                    if hasattr(entry, "published"):
                        embed.set_footer(text=entry.published)

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
        except Exception as e:
            print(f"Error fetching news: {e}")
        finally:
            db.close()

async def setup(bot):
    await bot.add_cog(NewsCog(bot))
