import discord
from discord.ext import commands, tasks
import feedparser
from database import SessionLocal, NewsLog
from bs4 import BeautifulSoup
import html

FEEDS = [
    {"name": "The Hacker News", "url": "https://feeds.feedburner.com/TheHackersNews", "color": 0x228B22},
    {"name": "Bleeping Computer", "url": "https://www.bleepingcomputer.com/feed/", "color": 0x1E90FF},
    {"name": "Latest CVEs", "url": "https://cvefeed.io/rssfeed/latest.xml", "color": 0xFF4500}
]
NEWS_CHANNEL_ID = 1529528609600045257

class NewsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.news_loop.start()
        
    def cog_unload(self):
        self.news_loop.cancel()
        
    @tasks.loop(minutes=30)
    async def news_loop(self):
        await self.bot.wait_until_ready()
        channel = self.bot.get_channel(NEWS_CHANNEL_ID)
        if not channel:
            return
            
        db = SessionLocal()
        try:
            for feed_info in FEEDS:
                feed = feedparser.parse(feed_info["url"])
                # Process up to 5 newest items
                for entry in list(reversed(feed.entries))[-5:]:
                    link = getattr(entry, "link", None)
                    if not link:
                        continue
                        
                    exists = db.query(NewsLog).filter_by(link=link).first()
                    if not exists:
                        # Mark as posted
                        db.add(NewsLog(link=link))
                        db.commit()
                        
                        # Post it
                        title = getattr(entry, "title", "No Title")
                        summary = getattr(entry, "summary", getattr(entry, "description", ""))
                        
                        # Clean HTML from summary
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
                            
                        await channel.send(embed=embed)
        except Exception as e:
            print(f"Error fetching news: {e}")
        finally:
            db.close()

async def setup(bot):
    await bot.add_cog(NewsCog(bot))
