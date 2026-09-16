import asyncio
import datetime
import json
import os
from pathlib import Path
from enum import Enum, auto
from collections import defaultdict

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

        print(f"{interaction.user} is currently attempting to initiate keyword silence with keyword {keyword} and duration {duration} minute/s.")
        
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

class PopularityContest(commands.Cog):
    def __init__(self, bot: commands.Bot | commands.AutoShardedBot, data_path: Path | None = None):
        self.bot = bot
        self.data_path = data_path or Path(__file__).with_name("popularity_contest.json")
        self.timeout_interval = 300  # In seconds

        # PERSISTENT VARIABLES
        self.nomination_ch_id: dict[int, int] = {}
        self.poll_ch_id: dict[int, int] = {}
        self.announcement_ch_id: dict[int, int] = {}
        self.contest_role_id: dict[int, int | None] = {}
        self.contest_manager_roles: dict[int, set[int]] = {}
        self.contest_exempt_roles: dict[int, set[int]] = {}
        self.contest_start_message: defaultdict = defaultdict(lambda: "A popularity contest has started! Nominate your choices now in {channel}!")
        self.contest_end_message: defaultdict = defaultdict(lambda: "The popularity contest has concluded! Your winner is... {user}!")
        self.member_role_id: dict[int, int | None] = {}

        if self.data_path.is_file():
            print("Fetching popularity contest vars from file...")
            with self.data_path.open("r", encoding="utf-8") as data_file:
                data = json.load(data_file)

            self.nomination_ch_id = {
                int(guild_id): channel_id
                for guild_id, channel_id in data.get("nomination_ch_id", {}).items()
            }
            self.poll_ch_id = {
                int(guild_id): channel_id
                for guild_id, channel_id in data.get("poll_ch_id", {}).items()
            }
            self.announcement_ch_id = {
                int(guild_id): channel_id
                for guild_id, channel_id in data.get("announcement_ch_id", {}).items()
            }
            self.contest_role_id = {
                int(guild_id): role_id
                for guild_id, role_id in data.get("contest_role_id", {}).items()
            }
            self.contest_manager_roles = {
                int(guild_id): set(role_ids)
                for guild_id, role_ids in data.get("contest_manager_roles", {}).items()
            }
            self.contest_exempt_roles = {
                int(guild_id): set(role_ids)
                for guild_id, role_ids in data.get("contest_exempt_roles", {}).items()
            }
            self.contest_start_message.update(
                {
                    int(guild_id): message
                    for guild_id, message in data.get("contest_start_message", {}).items()
                }
            )
            self.contest_end_message.update(
                {
                    int(guild_id): message
                    for guild_id, message in data.get("contest_end_message", {}).items()
                }
            )
            self.member_role_id = {
                int(guild_id): role_id
                for guild_id, role_id in data.get("member_role_id", {}).items()
            }
        print("Initialised popularity contest functions!")


    def save_data(self):
        data = {
            "nomination_ch_id": {
                str(guild_id): channel_id
                for guild_id, channel_id in self.nomination_ch_id.items()
            },
            "poll_ch_id": {
                str(guild_id): channel_id
                for guild_id, channel_id in self.poll_ch_id.items()
            },
            "announcement_ch_id": {
                str(guild_id): channel_id
                for guild_id, channel_id in self.announcement_ch_id.items()
            },
            "contest_role_id": {
                str(guild_id): role_id
                for guild_id, role_id in self.contest_role_id.items()
            },
            "contest_manager_roles": {
                str(guild_id): sorted(role_ids)
                for guild_id, role_ids in self.contest_manager_roles.items()
            },
            "contest_exempt_roles": {
                str(guild_id): sorted(role_ids)
                for guild_id, role_ids in self.contest_exempt_roles.items()
            },
            "contest_start_message": {
                str(guild_id): message
                for guild_id, message in self.contest_start_message.items()
            },
            "contest_end_message": {
                str(guild_id): message
                for guild_id, message in self.contest_end_message.items()
            },
            "member_role_id": {
                str(guild_id): role_id
                for guild_id, role_id in self.member_role_id.items()
            },
        }
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        with self.data_path.open("w", encoding="utf-8") as data_file:
            json.dump(data, data_file, indent=2)

    async def close_channel(self, guild: discord.Guild, channel: discord.TextChannel):
        everyone_role = guild.default_role
        member_id = self.member_role_id.get(guild.id, None)
        if member_id is not None:
            member_role = guild.get_role(member_id)
            if member_role is not None:
                await channel.set_permissions(member_role, send_messages=False)
        await channel.set_permissions(everyone_role, send_messages=False)
        await channel.send("This channel is now locked.")

    async def open_channel(self, guild: discord.Guild, channel: discord.TextChannel):
        everyone_role = guild.default_role
        member_id = self.member_role_id.get(guild.id, None)
        if member_id is not None:
            member_role = guild.get_role(member_id)
            if member_role is not None:
                await channel.set_permissions(member_role, send_messages=True)
        await channel.set_permissions(everyone_role, send_messages=True)
        await channel.send("Channel unlocked!")


    @app_commands.command(name="set_nomination_channel", description="Set the channel to nominate the weekly winner in.")
    @app_commands.guild_only
    async def set_nomination_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            return
        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Only server administrators can configure popularity contest channels.",
                ephemeral=True,
            )
            return

        try:
            await self.close_channel(interaction.guild, channel)
        except discord.Forbidden:
            await interaction.response.send_message(
                "Couldn't close the nominations channel properly; I might not have the correct permissions!",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Something went wrong! API error is as follows: {e}",
                ephemeral=True,
            )
            return
        self.nomination_ch_id[interaction.guild.id] = channel.id
        self.save_data()
        await interaction.response.send_message(
            f"Set popularity contest nomination channel to {channel.mention}! Channel closed successfully."
        )

    @app_commands.command(name="set_poll_channel", description="Set the channel to send the popularity contest poll in.")
    @app_commands.guild_only
    async def set_poll_channel(self, interaction:discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            return
        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Only server administrators can configure popularity contest channels.",
                ephemeral=True,
            )
            return
        self.poll_ch_id[interaction.guild.id] = channel.id
        self.save_data()
        await interaction.response.send_message(
            f"Set popularity contest poll channel to {channel.mention}!"
        )

    @app_commands.command(name="set_announcement_channel", description="Set the channel to announce the popularity contest winner in.")
    @app_commands.guild_only
    async def set_announcement_channel(self, interaction:discord.Interaction, channel:discord.TextChannel):
        if interaction.guild is None:
            return
        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Only server administrators can configure popularity contest channels.",
                ephemeral=True,
            )
            return
        self.announcement_ch_id[interaction.guild.id] = channel.id
        self.save_data()
        await interaction.response.send_message(
            f"Set popularity contest winners' announcement channel to {channel.mention}!"
        )

    @app_commands.command(
            name="set_contest_ping", 
            description="Set contest ping role. Can also be no role (leave the option blank)."
            )
    @app_commands.guild_only
    async def set_contest_ping(self, interaction: discord.Interaction, role: discord.Role | None = None):
        if interaction.guild is None:
            return
        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Only server administrators can configure popularity contest roles.",
                ephemeral=True,
            )
            return
        if role is None:
            self.contest_role_id[interaction.guild.id] = None
            self.save_data()
            await interaction.response.send_message(
                "Removed popularity contest role.",
                ephemeral=True
            )
            return
        self.contest_role_id[interaction.guild.id] = role.id
        self.save_data()
        await interaction.response.send_message(
            f"Set {role.mention} as the popularity contest role to ping.",
            ephemeral=True,
        )
        return

    @app_commands.command(name="contest_manager_role", description="Manage popularity contest manager roles.")
    @app_commands.guild_only
    async def contest_manager_role(self, interaction: discord.Interaction, action: str, role: discord.Role):
        if interaction.guild is None:
            return
        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Only server administrators can configure popularity contest roles.",
                ephemeral=True,
            )
            return
        if interaction.guild.id not in self.contest_manager_roles.keys():
            self.contest_manager_roles[interaction.guild.id] = set()
        match action.upper():
            case "ADD":
                self.contest_manager_roles[interaction.guild.id].add(role.id)
                self.save_data()
                await interaction.response.send_message(
                    f"Added {role.mention} as a contest manager role.",
                    ephemeral=True,
                )
                return
            case "REMOVE":
                if role.id in self.contest_manager_roles[interaction.guild.id]:
                    self.contest_manager_roles[interaction.guild.id].remove(role.id)
                    self.save_data()
                    await interaction.response.send_message(
                        f"Removed {role.mention} as a contest manager role.",
                        ephemeral=True,
                    )
                    return
                else:
                    await interaction.response.send_message(
                        f"{role.mention} was already not a contest manager role, so nothing has been changed.",
                        ephemeral=True,
                    )
                    return
            case _:
                await interaction.response.send_message(
                    f"Invalid option! Valid options are ADD to add a role or REMOVE to remove a role as a contest manager role.",
                    ephemeral=True,
                )
                return

    @app_commands.command(name="contest_exempt_role", description="Manage roles whose members cannot be nominated in popularity contests.")
    @app_commands.guild_only
    async def contest_exempt_role(self, interaction: discord.Interaction, action: str, role: discord.Role):
        if interaction.guild is None:
            return
        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Only server administrators can configure popularity contest roles.",
                ephemeral=True,
            )
            return

        if interaction.guild.id not in self.contest_exempt_roles:
            self.contest_exempt_roles[interaction.guild.id] = set()

        match action.upper():
            case "ADD":
                self.contest_exempt_roles[interaction.guild.id].add(role.id)
                self.save_data()
                await interaction.response.send_message(
                    f"Added {role.mention} as a popularity contest exempt role.",
                    ephemeral=True,
                )
            case "REMOVE":
                if role.id in self.contest_exempt_roles[interaction.guild.id]:
                    self.contest_exempt_roles[interaction.guild.id].remove(role.id)
                    self.save_data()
                    await interaction.response.send_message(
                        f"Removed {role.mention} as a popularity contest exempt role.",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        f"{role.mention} was already not a popularity contest exempt role, so nothing has been changed.",
                        ephemeral=True,
                    )
            case _:
                await interaction.response.send_message(
                    "Invalid option! Valid options are ADD to add a role or REMOVE to remove a role as a popularity contest exempt role.",
                    ephemeral=True,
                )

    @app_commands.command(
        name="set_contest_start_message", 
        description="Set the starting message for a popularity contest. Use {channel} in place of the nomination channel."
        )
    @app_commands.guild_only
    async def set_contest_start_message(self, interaction: discord.Interaction, message: str):
        if interaction.guild is None:
            return
        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Only server administrators can configure popularity contest messages.",
                ephemeral=True,
            )
            return
        self.contest_start_message[interaction.guild.id] = message
        self.save_data()
        await interaction.response.send_message(
            f"Successfully changed popularity contest starting message.",
            ephemeral=True,
        )
        return

    @app_commands.command(
        name="set_contest_end_message", 
        description="Set the ending message for a popularity contest. Use {user} for user/s and {votes} for vote amount."
        )
    @app_commands.guild_only
    async def set_contest_end_message(self, interaction: discord.Interaction, message: str):
        if interaction.guild is None:
            return
        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Only server administrators can configure popularity contest messages.",
                ephemeral=True,
            )
            return
        self.contest_end_message[interaction.guild.id] = message
        self.save_data()
        await interaction.response.send_message(
            f"Successfully changed popularity contest ending message.",
            ephemeral=True,
        )
        return

    @app_commands.command(
            name="set_member_role",
            description="Set the default server member role. No role means no default member role."
    )
    @app_commands.guild_only
    async def set_member_role(self, interaction:discord.Interaction, role: discord.Role | None = None):
        if interaction.guild is None:
            return
        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Only server administrators can configure popularity contest roles.",
                ephemeral=True,
            )
            return
        self.member_role_id[interaction.guild.id] = None if role is None else role.id
        self.save_data()
        await interaction.response.send_message(
            f"Successfully changed default member role.",
            ephemeral=True,
        )
        return

    async def finish_contest(
        self,
        guild_id: int,
        poll_channel: discord.TextChannel,
        poll_message_id: int,
        announcement_channel: discord.TextChannel,
        nomination_map: dict[str, discord.User | discord.Member],
    ):
        await asyncio.sleep(3610)

        poll_message = await poll_channel.fetch_message(poll_message_id)
        end_message = self.contest_end_message[guild_id]

        if not poll_message.poll:
            return

        answers = poll_message.poll.answers
        max_votes = -1
        winner_answers = []

        for answer in answers:
            if answer.vote_count > max_votes:
                max_votes = answer.vote_count
                winner_answers = [answer]
            elif answer.vote_count == max_votes and max_votes > 0:
                winner_answers.append(answer)

        if not winner_answers or max_votes == 0:
            victory_message = end_message.replace("{user}", "nobody")
        elif len(winner_answers) == 1:
            winner_name = winner_answers[0].text
            winner_user = nomination_map.get(winner_name)

            if winner_user:
                victory_message = end_message.replace("{user}", winner_user.mention)
            else:
                victory_message = end_message.replace("{user}", f"**{winner_name}**")
        else:
            winner_mentions = []
            for answer in winner_answers:
                winning_user = nomination_map.get(answer.text)
                if winning_user:
                    winner_mentions.append(winning_user.mention)
                else:
                    winner_mentions.append(f"**{answer.text}**")
            joined_mentions = ", ".join(winner_mentions[:-1]) + f"{',' if len(winner_mentions) > 2 else ''} and " + winner_mentions[-1]
            victory_message = end_message.replace("{user}", joined_mentions)

        await announcement_channel.send(
            victory_message.replace("{votes}", str(max_votes))
        )

    async def run_finish_contest(
        self,
        guild_id: int,
        poll_channel: discord.TextChannel,
        poll_message_id: int,
        announcement_channel: discord.TextChannel,
        nomination_map: dict[str, discord.User | discord.Member],
    ):
        try:
            await self.finish_contest(
                guild_id,
                poll_channel,
                poll_message_id,
                announcement_channel,
                nomination_map,
            )
        except discord.HTTPException as error:
            print(f"Failed to finish popularity contest for guild {guild_id}: {error}")

    @app_commands.command(name="popularity_contest", description="Start a popularity contest.")
    @app_commands.guild_only
    async def popularity_contest(self, interaction:discord.Interaction, max_nominations: int = 10):
        guild = interaction.guild
        if guild is None: 
            return
        guild_id = guild.id
        await interaction.response.defer(ephemeral=True)

        manager_roles = self.contest_manager_roles.get(guild_id, None)
        if not manager_roles:
            await interaction.followup.send(
                "Configure at least one contest manager role to start a popularity contest.",
                ephemeral=True
            )
            return

        if (
            not isinstance(interaction.user, discord.Member)
            or not any(role_id in [role.id for role in interaction.user.roles] for role_id in manager_roles)
        ):
            await interaction.followup.send(
                "You don't have a role that allows you to start a popularity contest!",
                ephemeral=True,
            )
            return

        if not 1 <= max_nominations <= 10:
            await interaction.followup.send(
                "The valid range of max nominations is 1-10.",
                ephemeral=True
            )
            return

        nomination_id = self.nomination_ch_id.get(guild_id, None)
        if nomination_id is None:
            await interaction.followup.send(
                "Nomination channel not configured!",
                ephemeral=True
            )
            return
        nomination_channel = guild.get_channel(nomination_id)
        if nomination_channel is None or not isinstance(nomination_channel, discord.TextChannel):
            await interaction.followup.send(
                "Nomination channel not configured!",
                ephemeral=True
            )
            return

        announcement_id = self.announcement_ch_id.get(guild_id, None)
        if announcement_id is None:
            await interaction.followup.send(
                "Announcement channel not configured!",
                ephemeral=True
            )
            return
        announcement_channel = guild.get_channel(announcement_id)
        if announcement_channel is None or not isinstance(announcement_channel, discord.TextChannel):
            await interaction.followup.send(
                "Announcement channel not configured!",
                ephemeral=True
            )
            return

        poll_id = self.poll_ch_id.get(guild_id, None)
        if poll_id is None:
            await interaction.followup.send(
                "Poll channel not configured!",
                ephemeral=True
            )
            return
        poll_channel = guild.get_channel(poll_id)
        if poll_channel is None or not isinstance(poll_channel, discord.TextChannel):
            await interaction.followup.send(
                "Poll channel not configured!",
                ephemeral=True
            )
            return

        # 1: Send commencement message
        message = ""
        contest_role_id = self.contest_role_id.get(guild_id, None)
        if contest_role_id is not None:
            contest_role = guild.get_role(contest_role_id) # pyright: ignore[reportArgumentType]
            if contest_role is not None:
                message = contest_role.mention + " "

        message += self.contest_start_message[guild_id].replace("{channel}", nomination_channel.mention)
        await announcement_channel.send(message)

        # 2: Open nomination channel, start listening for messages and adding to nominated users list
        try:
            await self.open_channel(guild, nomination_channel)
            await nomination_channel.send("Nominations are open! Please **@mention** a user to nominate them.")
        except discord.Forbidden:
            await interaction.followup.send(
                "Couldn't open the nominations channel properly; I might not have the correct permissions!",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Something went wrong! API error is as follows: {e}",
                ephemeral=True,
            )
            return

        nominations = []
        nomination_map: dict[str, discord.User | discord.Member] = {}
        while len(nominations) < max_nominations:
            def check(m: discord.Message):
                if m.channel != nomination_channel or m.author.bot or not m.mentions:
                    return False
                if len(m.mentions) == 1:
                    return True
                return False

            try:
                message = await self.bot.wait_for('message', check=check, timeout=self.timeout_interval)

                target_user = message.mentions[0]
                if target_user.id in [u.id for u in nominations]:
                    await nomination_channel.send(f"User {target_user.display_name} is already nominated!")
                    continue

                if target_user.display_name in nomination_map.keys():
                    await nomination_channel.send(f"Can't nominate {target_user.mention} because someone with their display name is already nominated...")
                    continue

                if target_user.id == message.author.id:
                    await nomination_channel.send(f"You can't nominate yourself!")
                    continue

                exempt_role_ids = self.contest_exempt_roles.get(guild_id, set())
                if isinstance(target_user, discord.Member) and any(
                    role.id in exempt_role_ids for role in target_user.roles
                ):
                    await nomination_channel.send(
                        f"User {target_user.display_name} is exempt and cannot be nominated."
                    )
                    continue

                nominations.append(target_user)
                nomination_map[target_user.display_name] = target_user
                remaining = max_nominations - len(nominations)

                await message.add_reaction("✅")
                await message.reply(f"{message.author.mention} has nominated {target_user.mention}!")
                if remaining > 0:
                    await nomination_channel.send(f"Remaining nominations available: {remaining} more members.")
            except asyncio.TimeoutError:
                await nomination_channel.send(f"Timeout period of {self.timeout_interval} seconds has elapsed.")
                break

        await nomination_channel.send(f"{len(nominations)} nominations have been collected. Now creating poll in {poll_channel.mention}...")

        try:
            await self.close_channel(guild, nomination_channel)
        except discord.Forbidden:
            await interaction.followup.send(
                "Couldn't close the nominations channel properly; I might not have the correct permissions. Please close it manually!",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Something went wrong! API error is as follows: {e}",
                ephemeral=True,
            )

        # 3: Create poll and wait

        poll = discord.Poll(
            question="Who will win?",
            duration=datetime.timedelta(hours=1),
            multiple=False
        )

        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

        for i, user in enumerate(nominations):
            poll.add_answer(text=user.display_name, emoji=emojis[i])

        if contest_role_id is not None:
            contest_role = guild.get_role(contest_role_id) # pyright: ignore[reportArgumentType]
            if contest_role is not None:
                await poll_channel.send(f"{contest_role.mention}")

        poll_message = await poll_channel.send(poll=poll)
        asyncio.create_task(
            self.run_finish_contest(
                guild_id,
                poll_channel,
                poll_message.id,
                announcement_channel,
                nomination_map,
            )
        )
        await interaction.followup.send(
            f"Popularity contest poll created in {poll_channel.mention}. Voting is open for one hour.",
            ephemeral=True,
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
