import asyncio
import datetime
import json
import os
from pathlib import Path
from enum import Enum, auto

import discord
from discord import app_commands
from discord.ext import commands


DEFAULT_THRESHOLD = 5


def normalize_emoji(emoji: str | discord.Emoji | discord.PartialEmoji) -> str:
    return str(emoji).strip()


def load_server_thresholds(path: Path) -> dict[int, int]:
    try:
        with path.open("r", encoding="utf-8") as settings_file:
            saved_thresholds = json.load(settings_file)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Could not parse threshold settings in {path}.") from error

    if not isinstance(saved_thresholds, dict):
        raise RuntimeError(f"Threshold settings in {path} must be a JSON object.")

    thresholds: dict[int, int] = {}
    for server_id, threshold in saved_thresholds.items():
        try:
            server_id = int(server_id)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                f"Invalid server ID in threshold settings: {server_id!r}."
            ) from error

        if isinstance(threshold, bool) or not isinstance(threshold, int):
            raise RuntimeError(
                f"Invalid threshold for server {server_id}: {threshold!r}."
            )
        thresholds[server_id] = threshold

    return thresholds


def save_server_thresholds(thresholds: dict[int, int], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")

    try:
        with temporary_path.open("w", encoding="utf-8") as settings_file:
            json.dump(
                {str(server_id): threshold for server_id, threshold in thresholds.items()},
                settings_file,
                indent=2,
                sort_keys=True,
            )
            settings_file.write("\n")
            settings_file.flush()
            os.fsync(settings_file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)

class CustomAction(Enum):
    RESPOND = auto()
    KILL = auto()
    KICK = auto()
    BAN = auto()
    MUTE = auto()
    BOARD = auto()
    ROLE = auto()

class CustomReactionActions(commands.Cog):
    def __init__(self, bot: commands.Bot | commands.AutoShardedBot, data_path: Path | None = None):
        self.bot = bot

        self.data_path = data_path or Path(__file__).with_name("custom_react_actions.json")
        self.duration_actions = {CustomAction.MUTE}
        self.processing_messages: set[int] = set()
        self.processing_lock = asyncio.Lock()
        self.disabled_channels: set[int] = set()

        if self.data_path.is_file():
            with self.data_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                self.disabled_channels = {
                    int(channel_id) for channel_id in data.get("disabled_channels", [])
                }
                self.board_channel_id: dict[int, int] = {
                    int(guild_id): channel_id for guild_id, channel_id in data.get("board_channel_id", {}).items()
                }
                self.award_role_id: dict[int, int] = {
                    int(guild_id): role_id for guild_id, role_id in data.get("award_role_id", {}).items()
                }
                self.custom_triggers: dict[int, dict[str, tuple[CustomAction, int, int, str]]] = {
                    int(guild_id): {
                        emoji: (
                            CustomAction[trigger_data[0]],
                            trigger_data[1],
                            trigger_data[2],
                            trigger_data[3] if len(trigger_data) > 3 else "",
                        )
                        for emoji, trigger_data in triggers.items()
                    }
                    for guild_id, triggers in data.get("custom_triggers", {}).items()
                }
        else:
            self.board_channel_id: dict[int, int] = {}  # Format: {guild_id: channel_id}
            self.award_role_id: dict[int, int] = {}  # Format: {guild_id: role_id}
            self.custom_triggers: dict[int, dict[str, tuple[CustomAction, int, int, str]]] = {} # Format: {guild_id: {emoji: (action, threshold, duration, message)}}

    def save_custom_trigger(
        self,
        guild_id: int,
        emoji: str,
        action: CustomAction,
        threshold: int,
        duration: int = 0,
        response_message: str = "",
    ):
        if guild_id not in self.custom_triggers:
            self.custom_triggers[guild_id] = {}
        self.custom_triggers[guild_id][normalize_emoji(emoji)] = (
            action,
            threshold,
            duration,
            response_message,
        )
        self.save_data()

    def is_channel_disabled(self, channel_id: int) -> bool:
        return channel_id in self.disabled_channels

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id:
            return
        if self.bot.user is None or payload.user_id == self.bot.user.id:
            return
        if self.is_channel_disabled(payload.channel_id):
            return

        guild_id = payload.guild_id
        emoji = normalize_emoji(payload.emoji)

        if guild_id not in self.custom_triggers:
            return

        triggers = self.custom_triggers[guild_id]
        if emoji not in triggers:
            return

        action, threshold, duration, response_message = triggers[emoji]

        async with self.processing_lock:
            if payload.message_id in self.processing_messages:
                return
            self.processing_messages.add(payload.message_id)

        channel = None
        author = None
        try:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return

            channel = guild.get_channel(payload.channel_id) or await self.bot.fetch_channel(
                payload.channel_id
            )
            message = await channel.fetch_message(payload.message_id)

            react = next(
                (
                    reaction
                    for reaction in message.reactions
                    if normalize_emoji(reaction.emoji) == emoji
                ),
                None,
            )
            if react is None or react.count < threshold or react.me:
                return

            author = message.author
            if not isinstance(author, discord.Member) or author.bot:
                return

            await message.add_reaction(payload.emoji)

            action_performed = False
            default_response = ""

            # Perform the action based on the defined trigger
            if action == CustomAction.KILL:
                await author.timeout(
                    datetime.timedelta(minutes=1), reason="Killed with custom trigger"
                )
                action_performed = True
                default_response = (
                    f"{author.mention} was killed with a custom trigger "
                    f"(democratically) due to {emoji} reactions."
                )

            elif action == CustomAction.KICK:
                await author.kick(reason="Kicked with custom trigger")
                action_performed = True
                default_response = (
                    f"{author.mention} was kicked with a custom trigger due to "
                    f"{emoji} reactions."
                )

            elif action == CustomAction.BAN:
                await author.ban(reason="Banned with custom trigger")
                action_performed = True
                default_response = (
                    f"{author.mention} was banned with a custom trigger due to "
                    f"{emoji} reactions."
                )

            elif action == CustomAction.MUTE:
                await author.timeout(
                    datetime.timedelta(minutes=duration), reason="Muted with custom trigger"
                )
                action_performed = True
                default_response = (
                    f"{author.mention} was muted for {duration} minutes due to "
                    f"{emoji} reactions."
                )

            elif action == CustomAction.BOARD:
                board_channel_id = self.board_channel_id.get(guild_id)
                if board_channel_id is not None:
                    board_channel = guild.get_channel(board_channel_id)
                    if board_channel is not None and isinstance(board_channel, discord.TextChannel):
                        await board_channel.send(
                            f"Message by {author.mention} reached the {emoji}board threshold:\n{message.content}"
                        )
                        action_performed = True

            elif action == CustomAction.RESPOND:
                default_response = (
                    f"{author.mention}, your message received {threshold} "
                    f"{emoji} reactions!"
                )
                action_performed = True

            elif action == CustomAction.ROLE:
                role_id = self.award_role_id.get(guild_id)
                if role_id is not None:
                    role = guild.get_role(role_id)
                    if role is not None:
                        await author.add_roles(role, reason="Role awarded with custom trigger")
                        action_performed = True
                        default_response = (
                            f"{author.mention} was awarded the {role.name} role due to "
                            f"{emoji} reactions."
                        )
                else:
                    await channel.send(
                        f"No award role is set for this server. Use /set_award_role to set one.", ephemeral=True
                    )

            if action_performed:
                response = response_message or default_response
                if response:
                    await message.reply(
                        response.replace("{user}", author.display_name)
                    )
        except discord.Forbidden:
            if message is not None:
                try:
                    await message.add_reaction(payload.emoji)
                except (discord.Forbidden, discord.HTTPException):
                    pass
            if channel is not None and author is not None:
                await channel.send(
                    f"Failed to perform action on {author.mention}. "
                    "I might not have the required permissions."
                )
        except discord.HTTPException as error:
            if message is not None:
                try:
                    await message.add_reaction(payload.emoji)
                except (discord.Forbidden, discord.HTTPException):
                    pass
            if channel is not None and author is not None:
                await channel.send(
                    f"Failed to perform action on {author.mention}. Error: {error}"
                )
        except Exception as error:
            if message is not None:
                try:
                    await message.add_reaction(payload.emoji)
                except (discord.Forbidden, discord.HTTPException):
                    pass
            if channel is not None and author is not None:
                await channel.send(
                    "An unexpected error occurred while trying to perform action on "
                    f"{author.mention}. Error: {error}"
                )
        finally:
            async with self.processing_lock:
                self.processing_messages.discard(payload.message_id)

    @app_commands.command(
        name="set_board_channel",
        description="Set the channel where messages will be posted when they reach the board threshold."
    )
    @app_commands.guild_only()
    async def set_board_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server administrators can set the board channel.",
                ephemeral=True,
            )
            return

        self.board_channel_id[interaction.guild_id] = channel.id
        self.save_data()
        await interaction.response.send_message(
            f"Board channel set to {channel.mention}.", ephemeral=True
        )

    @app_commands.command(
        name="set_award_role",
        description="Set the role to be awarded when a message reaches the role threshold."
    )
    @app_commands.guild_only()
    async def set_award_role(self, interaction: discord.Interaction, role: discord.Role):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server administrators can set the award role.",
                ephemeral=True,
            )
            return
        if not role or not isinstance(role, discord.Role):
            await interaction.response.send_message(
                "Invalid role provided.", ephemeral=True
            )
            return

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.", ephemeral=True
            )
            return

        guild = interaction.guild
        bot_member = guild.me if guild is not None else None
        if bot_member is None:
            await interaction.response.send_message(
                "I could not resolve my membership in this server.", ephemeral=True
            )
            return

        if not bot_member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I do not have permission to manage roles. Please ensure I have the 'Manage Roles' permission.",
                ephemeral=True,
            )
            return
        if bot_member.top_role <= role:
            await interaction.response.send_message(
                "I cannot assign a role that is higher than or equal to my top role. Please adjust the role hierarchy.",
                ephemeral=True,
            )
            return
        if role.managed:
            await interaction.response.send_message(
                "I cannot assign a managed role (e.g., roles from integrations). Please choose a different role.",
                ephemeral=True,
            )
            return

        self.award_role_id[interaction.guild_id] = role.id
        self.save_data()
        await interaction.response.send_message(
            f"Award role set to {role.mention}.", ephemeral=True
        )

    @app_commands.command(
        name="disable_channel",
        description="Disable reaction actions in a channel.",
    )
    @app_commands.guild_only()
    async def disable_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server administrators can disable the bot in a channel.",
                ephemeral=True,
            )
            return

        self.disabled_channels.add(channel.id)
        self.save_data()
        await interaction.response.send_message(
            f"Reaction actions disabled in {channel.mention}.", ephemeral=True
        )

    @app_commands.command(
        name="enable_channel",
        description="Enable reaction actions in a channel.",
    )
    @app_commands.guild_only()
    async def enable_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server administrators can enable the bot in a channel.",
                ephemeral=True,
            )
            return

        self.disabled_channels.discard(channel.id)
        self.save_data()
        await interaction.response.send_message(
            f"Reaction actions enabled in {channel.mention}.", ephemeral=True
        )

    @app_commands.command(
        name="sync_disabled",
        description="Disable reaction actions where a role cannot send messages.",
    )
    @app_commands.guild_only()
    async def sync_disabled(
        self, interaction: discord.Interaction, role: discord.Role
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server administrators can synchronize disabled channels.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.", ephemeral=True
            )
            return

        message_channels = [
            channel
            for channel in guild.channels
            if isinstance(channel, (discord.TextChannel, discord.ForumChannel))
        ]
        guild_channel_ids = {channel.id for channel in message_channels}
        self.disabled_channels.difference_update(guild_channel_ids)

        disabled_channels = {
            channel.id
            for channel in message_channels
            if not channel.permissions_for(role).send_messages
        }
        self.disabled_channels.update(disabled_channels)
        self.save_data()
        await interaction.response.send_message(
            f"Synchronized disabled channels for {role.mention}: "
            f"{len(disabled_channels)} channel(s) disabled.",
            ephemeral=True,
        )

    async def action_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = current.upper()
        return [
            app_commands.Choice(name=action.name, value=action.name)
            for action in CustomAction
            if current in action.name
        ][:25]

    @app_commands.command(
        name="define_custom_trigger",
        description="Define a custom reaction trigger for a specific action."
    )
    @app_commands.guild_only()
    @app_commands.autocomplete(action=action_autocomplete)
    @app_commands.describe(
        action="Action to perform when the threshold is reached (use /list_actions for valid options).",
        duration="Duration in minutes for actions that require it (e.g., MUTE).",
        response_message="Optional text for any action; use {user} for the member's display name."
    )
    async def define_custom_trigger(
        
        self,
        interaction: discord.Interaction,
        emoji: str,
        action: str,
        threshold: int,
        duration: int = 0,
        response_message: str = "",
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server administrators can define custom triggers.",
                ephemeral=True,
            )
            return

        try:
            action_enum = CustomAction[action.upper()]
        except KeyError:
            await interaction.response.send_message(
                f"Invalid action. Valid actions are: {', '.join(a.name for a in CustomAction)}",
                ephemeral=True,
            )
            return

        # Type checks for threshold and duration
        if not isinstance(threshold, int) or threshold < 1:
            await interaction.response.send_message(
                "Threshold must be a positive integer.", ephemeral=True
            )
            return
        if action_enum in self.duration_actions and (not isinstance(duration, int) or duration < 1):
            await interaction.response.send_message(
                "Duration must be a positive integer for actions that require it.", ephemeral=True
            )
            return
        elif not isinstance(duration, int) or duration < 0:
            await interaction.response.send_message(
                "Duration is not relevant, ignoring...", ephemeral=True
            )

        self.save_custom_trigger(
            interaction.guild_id,
            normalize_emoji(emoji),
            action_enum,
            threshold,
            duration,
            response_message,
        )
        await interaction.response.send_message(
            f"Custom trigger defined: {emoji} -> {action_enum.name} (Threshold: {threshold}, Duration: {duration})",
            ephemeral=True,
        )

    @app_commands.command(
        name="list_custom_triggers",
        description="List all custom reaction triggers defined for this server."
    )
    @app_commands.guild_only()
    async def list_custom_triggers(self, interaction: discord.Interaction):
        if interaction.guild_id not in self.custom_triggers or not self.custom_triggers[interaction.guild_id]:
            await interaction.response.send_message(
                "No custom triggers defined for this server.", ephemeral=True
            )
            return

        triggers = self.custom_triggers[interaction.guild_id]
        trigger_list = "\n".join(
            f"{emoji} -> {action.name} (Threshold: {threshold}, Duration: {duration})"
            for emoji, (action, threshold, duration, response_message) in triggers.items()
        )
        await interaction.response.send_message(
            f"Custom triggers for this server:\n{trigger_list}", ephemeral=True
        )

    @app_commands.command(
        name="list_actions",
        description="List all available actions for custom reaction triggers."
    )
    @app_commands.guild_only()
    async def list_actions(self, interaction: discord.Interaction):
        actions = ", ".join(a.name for a in CustomAction)
        await interaction.response.send_message(
            f"Available actions for custom triggers: {actions}", ephemeral=True
        )

    @app_commands.command(
        name="remove_custom_trigger",
        description="Remove a custom reaction trigger for this server."
    )
    @app_commands.guild_only()
    async def remove_custom_trigger(self, interaction: discord.Interaction, emoji: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server administrators can remove custom triggers.",
                ephemeral=True,
            )
            return

        emoji = normalize_emoji(emoji)
        if interaction.guild_id not in self.custom_triggers or emoji not in self.custom_triggers[interaction.guild_id]:
            await interaction.response.send_message(
                f"No custom trigger defined for {emoji}.", ephemeral=True
            )
            return

        del self.custom_triggers[interaction.guild_id][emoji]
        self.save_data()
        await interaction.response.send_message(
            f"Custom trigger for {emoji} removed.", ephemeral=True
        )

    def save_data(self):
        data = {
            "disabled_channels": sorted(self.disabled_channels),
            "board_channel_id": {str(guild_id): channel_id for guild_id, channel_id in self.board_channel_id.items()},
            "award_role_id": {str(guild_id): role_id for guild_id, role_id in self.award_role_id.items()},
            "custom_triggers": {
                str(guild_id): {
                    emoji: (action.name, threshold, duration, response_message)
                    for emoji, (action, threshold, duration, response_message) in triggers.items()
                }
                for guild_id, triggers in self.custom_triggers.items()
            },
        }
        with self.data_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

class MinuteOfSilenceFeatures(commands.Cog):
    def __init__(self, bot: commands.Bot | commands.AutoShardedBot):
        self.bot = bot
        self.armed_guilds = set()


    def parsedate(self, date_str: str) -> datetime.datetime:
        dt = datetime.datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)

    @app_commands.command(
        name = "arm_silence", description="Arm the keyword silence feature, or disable it if armed"
    )
    @app_commands.guild_only()
    async def arm_silence(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        if guild_id in self.armed_guilds:
            self.armed_guilds.remove(guild_id)
            await interaction.response.send_message("Disarmed the keyword silence feature in this server.")
        else:
            self.armed_guilds.add(guild_id)
            await interaction.response.send_message("Armed the keyword silence feature in this server.")


    @app_commands.command(
        name = "kword_silence", description = "Silence everyone who mentioned a keyword in a time period"
    )
    @app_commands.guild_only()
    async def kword_silence(self, interaction: discord.Interaction, keyword: str, duration: int = 1, 
                            start: str = "YYYY-MM-DD",
                            end: str = "YYYY-MM-DD"):

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server administrators can start a minute of silence.",
                ephemeral=True,
            )
            return
        if not interaction.guild_id in self.armed_guilds:
            await interaction.response.send_message(
                "This feature is not enabled in this server!",
                ephemeral=True,
            )
        
        await interaction.response.defer(thinking=True)
        
        # Set default datetimes to last day
        try:
            start_dt = datetime.datetime.now() - datetime.timedelta(days=1) if start == "YYYY-MM-DD" else self.parsedate(start)
            end_dt = datetime.datetime.now() if end == "YYYY-MM-DD" else self.parsedate(end)
        except ValueError:
            await interaction.followup.send("Invalid date format! Please use 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM'.")
            return

        found_users = set()
        async for message in interaction.channel.history(limit = None, after = start_dt, before = end_dt):
            if keyword.lower() in message.content.lower():
                found_users.add(message.author)

        await interaction.followup.send(f"Now enforcing {"1 minute" if duration == 1 else f"{duration} minutes"} of silence for {len(found_users)} users...")
        for user in found_users:
            await user.timeout(
                datetime.timedelta(minutes=duration), reason="Muted for minute/s of silence"
            )


class HammerFeatures(commands.Cog):
    def __init__(self, bot: commands.Bot, thresholds_path: Path):
        self.bot = bot

        self.processing_messages: set[int] = set()
        self.message_state_lock = asyncio.Lock()
        self.thresholds_path = thresholds_path
        self.server_thresholds = load_server_thresholds(thresholds_path)

    # What to run when the bot is ready
    @commands.Cog.listener()
    async def on_ready(self):
        if self.bot.user is not None:
            print(f"Logged in as {self.bot.user.name}")

    async def mark_hammered(self, message: discord.Message) -> None:
        try:
            await message.add_reaction("🔨")
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id:
            return
        if self.bot.user is None or payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) != "🔨":
            return

        custom_actions = self.bot.get_cog("CustomReactionActions")
        if custom_actions is not None and custom_actions.is_channel_disabled(
            payload.channel_id
        ):
            return

        async with self.message_state_lock:
            if payload.message_id in self.processing_messages:
                return
            self.processing_messages.add(payload.message_id)

        channel = None
        message = None
        author = None
        try:
            guild = self.bot.get_guild(payload.guild_id)
            if guild is None:
                return

            channel = guild.get_channel(payload.channel_id) or await self.bot.fetch_channel(
                payload.channel_id
            )
            message = await channel.fetch_message(payload.message_id)

            hammer_react = discord.utils.get(message.reactions, emoji="🔨")
            threshold = self.server_thresholds.get(
                payload.guild_id, DEFAULT_THRESHOLD
            )
            if (
                hammer_react is None
                or hammer_react.count < threshold
                or hammer_react.me
            ):
                return

            author = message.author
            if not isinstance(author, discord.Member) or author.bot:
                return

            if not author.guild_permissions.administrator:
                await author.timeout(
                    datetime.timedelta(minutes=1), reason="Killed with hammers"
                )
                await self.mark_hammered(message)
                await channel.send(
                    f"{author.mention} was killed with hammers (democratically)"
                )
            else:
                await self.mark_hammered(message)
                await channel.send(
                    f"I tried to kill {author.mention} with hammers but I couldn't. Please glare at them angrily instead"
                )
                return
            print(
                f"Successfully timed out {author} for 1 minute due to hammer reactions."
            )
        except discord.Forbidden:
            if message is not None:
                await self.mark_hammered(message)
            if channel is not None and author is not None:
                await channel.send(
                    f"Failed to timeout {author.mention}. "
                    "I might not have the required permissions."
                )
        except discord.HTTPException as error:
            if message is not None:
                await self.mark_hammered(message)
            if channel is not None and author is not None:
                await channel.send(
                    f"Failed to timeout {author.mention}. Error: {error}"
                )
        except Exception as error:
            if message is not None:
                await self.mark_hammered(message)
            if channel is not None and author is not None:
                await channel.send(
                    "An unexpected error occurred while trying to timeout "
                    f"{author.mention}. Error: {error}"
                )
        finally:
            async with self.message_state_lock:
                self.processing_messages.discard(payload.message_id)

    @app_commands.command(name="ping", description="Check the bot's latency.")
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(
            f"Pong! (with hammer)\nLatency: {latency}ms"
        )

    @app_commands.command(
        name="set_threshold", description="Set the hammer threshold for timeout."
    )
    @app_commands.guild_only()
    async def set_threshold(self, interaction: discord.Interaction, value: int):
        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Only server administrators can change the hammer threshold.",
                ephemeral=True,
            )
            return

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.", ephemeral=True
            )
            return

        if value < 1:
            await interaction.response.send_message(
                "The hammer threshold must be at least 1.", ephemeral=True
            )
            return

        updated_thresholds = {**self.server_thresholds, interaction.guild_id: value}
        save_server_thresholds(updated_thresholds, self.thresholds_path)
        self.server_thresholds[interaction.guild_id] = value
        await interaction.response.send_message(
            f"Hammer threshold for this server set to {value}.", ephemeral=True
        )

    @app_commands.command(
        name="reset_memory", description="Reset temporary hammer-processing state."
    )
    @app_commands.guild_only()
    async def reset_memory(self, interaction: discord.Interaction):
        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Only server administrators can reset the hammered-message memory.",
                ephemeral=True,
            )
            return

        async with self.message_state_lock:
            self.processing_messages.clear()
        await interaction.response.send_message(
            "Temporary hammer-processing state has been reset. Existing hammer reactions remain as idempotency markers.",
            ephemeral=True,
        )
