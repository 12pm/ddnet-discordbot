#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import re
import traceback
from datetime import datetime
from io import BytesIO
from sys import platform
from typing import Optional, Tuple

import discord
from discord.ext import commands

from utils.misc import human_join, run_process, sanitize

log = logging.getLogger(__name__)

DIR = 'data/map-testing'

SERVER_TYPES = {
    'Novice':       '👶',
    'Moderate':     '🌸',
    'Brutal':       '💪',
    'Insane':       '💀',
    'Dummy':        '♿',
    'Oldschool':    '👴',
    'Solo':         '⚡',
    'Race':         '🏁',
}


def has_map_file(message: discord.Message) -> bool:
    return message.attachments and message.attachments[0].filename.endswith('.map')


class MapTesting(commands.Cog, command_attrs=dict(hidden=True)):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild = self.bot.guild

    @property
    def mt_cat(self) -> discord.CategoryChannel:
        return discord.utils.get(self.guild.categories, name='Map Testing')

    @property
    def em_cat(self) -> discord.CategoryChannel:
        return discord.utils.get(self.guild.categories, name='Evaluated Maps')

    @property
    def announce_chan(self) -> discord.TextChannel:
        return discord.utils.get(self.guild.text_channels, name='announcements')

    @property
    def log_chan(self) -> discord.TextChannel:
        return discord.utils.get(self.guild.text_channels, name='logs')

    @property
    def tinfo_chan(self) -> discord.TextChannel:
        return discord.utils.get(self.guild.text_channels, name='📌info')

    @property
    def submit_chan(self) -> discord.TextChannel:
        return discord.utils.get(self.guild.text_channels, name='📬submit-maps')

    @property
    def testing_role(self) -> discord.Role:
        return discord.utils.get(self.guild.roles, name='testing')

    async def upload_file(self, asset_type: str, file: BytesIO, filename: str) -> int:
        url = self.bot.config.get('DDNET_UPLOAD', 'URL')

        if asset_type == 'map':
            name = 'map_name'
        elif asset_type == 'log':
            name = 'channel_name'
        elif asset_type in ('attachment', 'avatar', 'emoji'):
            name = 'asset_name'
        else:
            log.error('%s is not a valid asset_type', asset_type)
            return -1

        data = {
            'asset_type': asset_type,
            'file': file,
            name: filename
        }

        headers = {'X-DDNet-Token': self.bot.config.get('DDNET_UPLOAD', 'TOKEN')}

        async with self.bot.session.post(url, data=data, headers=headers) as resp:
            status = resp.status
            if status != 200:
                text = await resp.text()
                log.error('Failed to upload %s %s to ddnet.tw: %s (status code: %d)', asset_type, filename, text, status)

            return status

    def is_staff(self, channel: discord.TextChannel, user: discord.Member) -> bool:
        return channel.permissions_for(user).manage_channels

    def is_testing_channel(self, channel: discord.TextChannel, map_channel: bool=False) -> bool:
        testing_channel = isinstance(channel, discord.TextChannel) and channel.category in (self.mt_cat, self.em_cat)
        if map_channel:
            testing_channel = testing_channel and channel not in (self.tinfo_chan, self.submit_chan)

        return testing_channel

    def format_map_details(self, details: str) -> Optional[Tuple[str, str, str]]:
        # Format: `"<name>" by <mapper> [<server>]`
        format_re = r'^\"(.+)\" +by +(.+) +\[(.+)\]$'
        match = re.search(format_re, details)
        if not match:
            return

        name, mapper, server = match.groups()
        mapper = re.split(r', | , | & | and ', mapper)
        server = server.capitalize() if server.capitalize() in SERVER_TYPES else server

        return name, mapper, server

    def get_map_channel(self, name: str) -> Optional[discord.TextChannel]:
        name = name.lower()
        return discord.utils.find(lambda c: name == c.name[1:], self.mt_cat.text_channels) \
            or discord.utils.find(lambda c: name == c.name[2:], self.em_cat.text_channels)

    def validate_map_submission(self, message: discord.Message) -> Optional[str]:
        details = self.format_map_details(message.content)
        filename = message.attachments[0].filename[:-4]
        duplicate_chan = self.get_map_channel(filename)

        if not details:
            return 'Your map submission does not cointain correctly formated details.'
        elif sanitize(details[0], True, False) != filename:
            return 'Name and filename of your map submission do not match.'
        elif details[2] not in SERVER_TYPES:
            return f'The server type of your map submission is not one of `{", ".join(SERVER_TYPES)}`'
        elif duplicate_chan:
            return f'A channel for the map you submitted already exists: {duplicate_chan.mention}'

    async def uploaded_by_author(self, channel: discord.TextChannel, author: discord.Member, map_name: str) -> bool:
        try:
            mentions, filename = channel.topic.split('\n')[1].split(' | ')
        except (IndexError, ValueError):
            return False

        if filename != map_name:
            return False

        if not any(m == author.mention for m in mentions.split(' ')):
            return False

        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        channel = message.channel
        author = message.author

        if channel == self.submit_chan:
            if has_map_file(message):
                error = self.validate_map_submission(message)
                if error:
                    await author.send(error)

                await message.add_reaction('❗' if error else '☑')

            elif not self.is_staff(channel, author):
                # Delete messages that aren't submissions
                await message.delete()

        elif self.is_testing_channel(channel, map_channel=True):
            if has_map_file(message):
                attachment = message.attachments[0]
                filename = attachment.filename

                by_author = await self.uploaded_by_author(channel, author, filename)
                if by_author:
                    buf = BytesIO()
                    await attachment.save(buf)
                    resp = await self.upload_file('map', buf, filename[:-4])
                    await message.add_reaction('🆙' if resp == 200 else '❌')

                    log.info('%s (ID: %d) uploaded map %s (channel ID: %d)', author, author.id, filename, channel.id)

                elif author != self.bot.user:
                    await message.add_reaction('☑')

                await message.pin()

            if message.type is discord.MessageType.pins_add and author == self.bot.user:
                # Delete spammy bot system messages
                await message.delete()

    async def get_message(self, channel: discord.TextChannel, message_id: int) -> discord.Message:
        message = next(m for m in self.bot.cached_messages if m.id == message_id)
        if message is None:
            message = await channel.fetch_message(message_id)

        return message

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        data = payload.data

        if int(data['channel_id']) != self.submit_chan.id:
            return

        if not ('attachments' in data and data['attachments'][0]['filename'].endswith('.map')):
            return

        message = await self.submit_chan.fetch_message(payload.message_id)
        if any(str(r.emoji) == '✅' for r in message.reactions):
            # Ignore already approved submissions
            return

        error = self.validate_map_submission(message)
        if error:
            await message.author.send(error)

        await message.clear_reactions()
        await message.add_reaction('❗' if error else '☑')

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if str(payload.emoji) != '☑':
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not self.is_testing_channel(channel):
            return

        user = channel.guild.get_member(payload.user_id)
        if not self.is_staff(channel, user) or user == self.bot.user:
            return

        message = await channel.fetch_message(payload.message_id)
        if not has_map_file(message):
            return

        attachment = message.attachments[0]
        filename = attachment.filename

        if channel == self.submit_chan:
            # Initial map submissions
            accept = discord.utils.find(lambda r: str(r.emoji) == '☑', message.reactions)
            if not accept:
                return

            name, mapper, server = self.format_map_details(message.content)
            emoji = SERVER_TYPES[server]
            topic = f'**"{name}"** by {human_join([f"**{m}**" for m in mapper])} [{server}]\n' \
                    f'{message.author.mention} | {filename}'

            read_messages = discord.PermissionOverwrite(read_messages=True)
            users = await accept.users().flatten()
            overwrites = {u: read_messages for u in users + [message.author]}
            # Category permissions:
            # - @everyone role: read_messages=False
            # - Tester role:    manage_channels=True, read_messages=True,
            #                   manage_messages=True, manage_roles=True
            # - testing role:   read_messages = True
            # - Bot user:       read_messages=True, manage_messages=True
            overwrites.update(self.mt_cat.overwrites)

            map_chan = await self.mt_cat.create_text_channel(emoji + filename[:-4], overwrites=overwrites, topic=topic)

            await message.clear_reactions()
            await message.add_reaction('✅')

            buf = BytesIO()
            await attachment.save(buf)
            file = discord.File(buf, filename=filename)
            message = await map_chan.send(message.author.mention, file=file)

            # Generate the thumbnail
            await attachment.save(f'{DIR}/maps/{filename}')
            _, stderr = await run_process(f'{DIR}/generate_thumbnail.sh {filename}')
            if stderr:
                log.error('Failed to generate thumbnail of map %s: %s', filename, stderr)
            else:
                thumbnail = discord.File(f'{DIR}/thumbnails/{filename[:-4]}.png')
                await map_chan.send(file=thumbnail)

        # Upload the map to DDNet test servers
        buf = BytesIO()
        await attachment.save(buf)
        resp = await self.upload_file('map', buf, filename[:-4])
        await message.clear_reactions()
        await message.add_reaction('🆙' if resp == 200 else '❌')

        log.info('%s (ID: %d) approved map %s (channel ID: %d)', user, user.id, filename, channel.id)

    @commands.Cog.listener('on_raw_reaction_add')
    async def handle_giving_perms(self, payload: discord.RawReactionActionEvent):
        if str(payload.emoji) != '✅':
            return

        channel = self.bot.get_channel(payload.channel_id)
        user = channel.guild.get_member(payload.user_id)

        # General permissions
        if channel == self.tinfo_chan and self.testing_role not in user.roles:
            await user.add_roles(self.testing_role)

        # Individual channel permissions
        if channel == self.submit_chan:
            message = await channel.fetch_message(payload.message_id)
            map_name = message.attachments[0].filename[:-4]
            map_chan = self.get_map_channel(map_name)
            if map_chan:
                if not map_chan.overwrites_for(user).read_messages:
                    await map_chan.set_permissions(user, read_messages=True)
            else:
                # Remove the reaction to signalize it didn't work
                await message.remove_reaction(payload.emoji, user)

    @commands.Cog.listener('on_raw_reaction_remove')
    async def handle_removing_perms(self, payload: discord.RawReactionActionEvent):
        if str(payload.emoji) != '✅':
            return

        channel = self.bot.get_channel(payload.channel_id)
        user = channel.guild.get_member(payload.user_id)

        # General permissions
        if channel == self.tinfo_chan and self.testing_role in user.roles:
            await user.remove_roles(self.testing_role)

        # Individual channel permissions
        if channel == self.submit_chan:
            message = await channel.fetch_message(payload.message_id)
            map_name = message.attachments[0].filename[:-4]
            map_chan = self.get_map_channel(map_name)
            if map_chan and map_chan.overwrites_for(user).read_messages:
                await map_chan.set_permissions(user, overwrite=None)

    def cog_check(self, ctx: commands.Context) -> bool:
        return self.is_testing_channel(ctx.channel, map_channel=True) and self.is_staff(ctx.channel, ctx.author)

    async def move_map_channel(self, channel: discord.TextChannel, *, emoji: str):
        if channel.name[0] in ('📆', '🔥', '❌'):
            prev_emoji = channel.name[0]
        else:
            prev_emoji = None

        if prev_emoji and prev_emoji == emoji:
            return

        name = channel.name[1:] if prev_emoji else channel.name
        await channel.edit(name=emoji + name, category=self.em_cat)

    @commands.command()
    async def ready(self, ctx: commands.Context):
        """Ready a map"""
        await self.move_map_channel(ctx.channel, emoji='📆')

    @commands.command()
    async def decline(self, ctx: commands.Context):
        """Decline a map"""
        await self.move_map_channel(ctx.channel, emoji='❌')

    @commands.Cog.listener('on_message')
    async def release(self, message: discord.Message):
        if message.channel != self.announce_chan:
            return

        if not message.webhook_id:
            return

        map_url_re = r'\[(?P<name>.+)\]\(<https://ddnet\.tw/maps/\?map=.+?>\)'
        match = re.search(map_url_re, message.content)
        name = sanitize(match.group('name'), channel_name=True)
        map_chan = self.get_map_channel(name)
        if map_chan:
            await self.move_map_channel(map_chan, emoji='🔥')


def setup(bot: commands.Bot):
    bot.add_cog(MapTesting(bot))
