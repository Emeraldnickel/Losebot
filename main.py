import os
from pathlib import Path

import discord
from discord.ext import commands

from bot_features import CustomReactionActions, HammerFeatures

use_shards = False

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guild_reactions = True


if use_shards:
    class LoserBot(commands.AutoShardedBot):
        def __init__(self, *args, **kwargs):
            super().__init__(
                command_prefix="%",
                intents=intents,
                status=discord.Status.online,
                activity=discord.Game(name="Beating people with hammers"),
                *args,
                **kwargs,
            )

        async def setup_hook(self):
            # await self.add_cog(
            #     HammerFeatures(self, Path(__file__).with_name("server_thresholds.json"))
            # )
            await self.add_cog(
                CustomReactionActions(self, Path(__file__).with_name("custom_react_actions.json"))
            )
            await self.tree.sync()

        async def close(self):
            if self.is_ready() and not self.is_closed():
                try:
                    await self.change_presence(status=discord.Status.offline, activity=None)
                except discord.HTTPException:
                    pass
            await super().close()
else:
    class LoserBot(commands.Bot):
            def __init__(self, *args, **kwargs):
                super().__init__(
                    command_prefix="%",
                    intents=intents,
                    status=discord.Status.online,
                    activity=discord.Game(name="Beating people with hammers"),
                    *args,
                    **kwargs,
                )
    
            async def setup_hook(self):
                # await self.add_cog(
                #     HammerFeatures(self, Path(__file__).with_name("server_thresholds.json"))
                # )
                await self.add_cog(
                    CustomReactionActions(self, Path(__file__).with_name("custom_react_actions.json"))
                )
                await self.tree.sync()
    
            async def close(self):
                if self.is_ready() and not self.is_closed():
                    try:
                        await self.change_presence(status=discord.Status.offline, activity=None)
                    except discord.HTTPException:
                        pass
                await super().close()


bot = LoserBot()


token = os.environ.get("DISCORD_TOKEN")
if not token:
    raise RuntimeError("DISCORD_TOKEN secret is not configured.")

bot.run(token)