import asyncio
import logging
import signal
import sys
from argparse import Namespace
from typing import Literal, List, Optional, Coroutine

import discord
from discord import Interaction, app_commands, InteractionResponse
from discord.app_commands import Transformer, Range, Transform
from discord.ext import commands
from discord.ext.commands import (
    Bot,
    Context,
    CommandError,
    MissingRequiredArgument,
    BadLiteralArgument,
    GroupCog,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncEngine,
    async_sessionmaker,
    AsyncSession,
)
from sqlalchemy.orm import selectinload

from models import SubmissionType, SubmissionCriterion, Keyword, User
from monitor import run_monitor

LOGGER = logging.getLogger("wemb.bot")

MONITOR_COROUTINE: Optional[Coroutine] = None

DB_ENGINE: Optional[AsyncEngine] = None
DB_SESSION: Optional[async_sessionmaker[AsyncEngine]] = None

intents = discord.Intents.default()
intents.message_content = True

bot = Bot(command_prefix="%", intents=intents)


@bot.event
async def on_ready():
    global MONITOR_COROUTINE

    LOGGER.info("Logged in!")
    LOGGER.debug("Adding cogs...")
    await bot.add_cog(Searches())

    LOGGER.info("Starting monitor...")

    if MONITOR_COROUTINE is not None:
        asyncio.get_event_loop().create_task(MONITOR_COROUTINE, name="monitor")
    else:
        LOGGER.critical(
            "MONITOR_COROUTINE was not initialized properly! PRAW will NOT start."
        )

    LOGGER.info("Ready!")


@bot.command()
@commands.is_owner()
async def sync(ctx: Context, target: Literal["global", "here"]):
    LOGGER.debug(f"Received command: %sync {target}")

    if target == "global":
        synced_commands = await ctx.bot.tree.sync()
        await ctx.send(f"Synced {len(synced_commands)} command(s) globally.")
    elif target == "here":
        if ctx.guild is None:
            await ctx.send("Not in a guild context!")
            return

        ctx.bot.tree.copy_global_to(guild=ctx.guild)
        synced_commands = await ctx.bot.tree.sync(guild=ctx.guild)
        await ctx.send(
            f"Synced {len(synced_commands)} command(s) to the current guild."
        )

    # noinspection PyUnboundLocalVariable
    # The 'else' path will never be taken based on validation rules so synced_commands should always have a value.
    LOGGER.debug(f"\tSynced the following commands:\n\t{synced_commands!s}")


@sync.error
async def sync_error(ctx: Context, error: CommandError):
    usage_string = "Usage: `%sync <here|global>`"

    if isinstance(error, MissingRequiredArgument):
        await ctx.send(
            f"A value for `{error.param.name}` is missing!\n\t{usage_string}"
        )
    elif isinstance(error, BadLiteralArgument):
        await ctx.send(
            f"`{error.param.name}` must be either `here` or `global`!\n\t{usage_string}"
        )
    else:
        LOGGER.error("Unhandled exception for 'sync' command!")
        raise error


@bot.hybrid_command(description="Pings the bot to check if it's alive")
async def ping(ctx: Context):
    LOGGER.debug(f"Received hybrid command: /ping")
    await ctx.send("pong")


class Searches(
    GroupCog,
    name="searches",
    description="Manages submission search criteria that this bot listens for",
):
    class KeywordListTransformer(Transformer):
        async def transform(
            self, interaction: Interaction, value: str, /
        ) -> List[Keyword]:
            return [Keyword(content=keyword) for keyword in value.split()]

    @app_commands.command(name="add")
    @app_commands.describe(
        submission_type="The 'type' of post you are looking for (Want to buy / Want to sell)",
        keywords="Space-separated list of keywords to search for. Case insensitive.",
        all_required="Whether all keywords are required to match (true), or only just one (false). Default is true",
        min_transactions="The minimum transaction count of the posting user, shown in their flair. Default is 5",
    )
    async def add(
        self,
        interaction: Interaction[Bot],
        submission_type: SubmissionType,
        keywords: Transform[List[Keyword], KeywordListTransformer],
        all_required: bool = True,
        min_transactions: Range[int, 1] = 5,
    ):
        """Add a search for a particular item on the subreddit"""

        LOGGER.debug(
            f"Received slash command: /searches add {submission_type} {keywords} {all_required} {min_transactions}"
        )

        # noinspection PyTypeChecker
        response: InteractionResponse[Bot] = interaction.response

        await response.send_message("Creating...")

        session: AsyncSession
        async with DB_SESSION() as session:
            criterion = SubmissionCriterion(
                submission_type=submission_type,
                keywords=keywords,
                all_required=all_required,
                min_transactions=min_transactions,
            )
            session.add(criterion)
            await session.commit()

            await interaction.edit_original_response(content=f"Created!\n{criterion!r}")

    @app_commands.command(name="list")
    async def list(self, interaction: Interaction[Bot]):
        """Lists all current search criteria"""

        LOGGER.debug("Received slash command: /searches list")

        # noinspection PyTypeChecker
        response: InteractionResponse[Bot] = interaction.response

        await response.send_message("Please wait...")

        session: AsyncSession
        async with DB_SESSION() as session:
            # noinspection PyTypeChecker
            # Eager load .keywords, because we're printing the repr
            criteria: List[SubmissionCriterion] = await session.scalars(
                select(SubmissionCriterion).options(
                    selectinload(SubmissionCriterion.keywords)
                )
            )

            await interaction.edit_original_response(
                content="Search criteria:\n" + "\t\n".join((str(c) for c in criteria))
            )

    @app_commands.command(name="delete")
    @app_commands.describe(
        search_id="The id (primary key) of the search criterion you want to remove."
    )
    async def delete(self, interaction: Interaction[Bot], search_id: Range[int, 0]):
        """Remove a specified search criteria by ID"""

        LOGGER.debug(f"Received slash command: /searches delete {search_id}")

        # noinspection PyTypeChecker
        response: InteractionResponse[Bot] = interaction.response

        await response.send_message("Please wait...")

        session: AsyncSession
        async with DB_SESSION() as session:
            criterion: SubmissionCriterion = await session.scalar(
                select(SubmissionCriterion).where(SubmissionCriterion.id == search_id)
            )

            if criterion is None:
                await interaction.edit_original_response(
                    content=f"Search criteria not found for id {search_id}!"
                )
                return

            await session.delete(criterion)
            await session.commit()

        await interaction.edit_original_response(content="Successfully deleted!")


async def __shutdown(sig: signal.Signals, loop: asyncio.AbstractEventLoop):
    """When called, shuts down all tasks from the event loop (Discord, PRAW, and any hanging network requests), and stops the event loop gracefully."""

    LOGGER.debug(f"Signal {sig.name} was caught!")
    LOGGER.info("Shutdown called!")

    LOGGER.info("Disposing database engine...")
    if DB_ENGINE is not None:
        await DB_ENGINE.dispose()

    LOGGER.debug("Getting all tasks...")
    tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    LOGGER.debug(f"\tTasks: {tasks}")

    LOGGER.info(f"Cancelling {len(tasks)} tasks...")
    [task.cancel() for task in tasks]

    LOGGER.debug("Awaiting shutdown...")
    await asyncio.gather(*tasks, return_exceptions=True)

    LOGGER.info("Stopping event loop...")
    loop.stop()


def __dirty_shutdown(sig_num: int, frame):
    """When called, schedules a __shutdown() coroutine on the event loop. Used as a (non-asyncio) signal handler."""

    loop = asyncio.get_event_loop()
    loop.create_task(__shutdown(signal.Signals(sig_num), loop))


def run_bot(args: Namespace):
    global DB_ENGINE
    global DB_SESSION
    global MONITOR_COROUTINE

    loop = asyncio.get_event_loop()

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig, lambda sig=sig: asyncio.create_task(__shutdown(sig, loop))
            )
    except NotImplementedError:
        LOGGER.warning(
            "asyncio signals are NOT properly supported on this platform! Python's provided signal handler will be used instead, but this may cause objects to not be closed properly!"
        )

        if not args.allow_dirty_shutdown:
            LOGGER.error(
                "Please pass --allow-dirty-shutdown or set the WEMB_ALLOW_DIRTY_SHUTDOWN env var to accept these conditions."
            )
            sys.exit(1)

        signal.signal(signal.SIGINT, __dirty_shutdown)
        signal.signal(signal.SIGTERM, __dirty_shutdown)

    # Disposal is done is the shutdown hook
    # We need expire_on_commit = False, as documented here: https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#asyncio-orm-avoid-lazyloads
    DB_ENGINE = create_async_engine(args.db_connection_string)
    DB_SESSION = async_sessionmaker(DB_ENGINE, expire_on_commit=False)

    async def bot_runner():
        async with bot:
            await bot.start(args.discord_api_token, reconnect=True)

    # Save PRAW coroutine to run in on_ready()
    MONITOR_COROUTINE = run_monitor(args, session_factory=DB_SESSION)

    try:
        loop.create_task(bot_runner(), name="bot")
        loop.run_forever()
    finally:
        loop.close()
        LOGGER.info("Shutting down...")
