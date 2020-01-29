#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
from collections import namedtuple
from datetime import datetime
from typing import Dict, List, Optional

import discord
from discord.ext import commands

from data.countryflags import COUNTRYFLAGS, FLAG_UNK
from utils.menu import Pages
from utils.text import clean_content, escape

log = logging.getLogger(__name__)


BASE_URL = 'https://ddnet.tw'

class Player:
    __slots__ = ('name', 'clan', 'score', 'country', 'playing', 'url')

    def __init__(self, **kwargs):
        self.name = kwargs.pop('name')
        self.clan = kwargs.pop('clan')
        self.score = kwargs.pop('score')
        self.country = kwargs.pop('country')
        self.playing = kwargs.pop('playing')

        try:
            self.url = BASE_URL + kwargs.pop('url')
        except KeyError:
            self.url = None

    def is_connected(self) -> bool:
        # https://github.com/ddnet/ddnet/blob/38f91d3891eefc392f60f77b1b82ecdb3a47ec62/src/engine/client/serverbrowser.cpp#L348
        return self.name != '(connecting)' or self.clan != '' or self.score != 0 or self.country != -1

    @property
    def flag(self) -> str:
        return COUNTRYFLAGS.get(self.country, FLAG_UNK)

    @property
    def time(self) -> str:
        if self.score == -9999:
            return '--:--'
        else:
            return '{0:02d}:{1:02d}'.format(*divmod(abs(self.score), 60))

    def format(self, time_score: bool=False) -> str:
        if self.url is None:
            line = [f'**{escape(self.name)}**']
        else:
            line = [f'[{escape(self.name)}]({self.url})']

        if self.clan:
            line.append(escape(self.clan))

        if self.playing:
            score = self.time if time_score else self.score
            line = [self.flag, f'`{score}`'] + line

        return ' '.join(line)


class Server:
    __slots__ = ('ip', 'port', 'host', 'name', 'map', 'gametype', 'max_players',
                 'max_clients', '_clients', 'timestamp', 'map_url')

    def __init__(self, **kwargs):
        self.ip = kwargs.pop('ip')
        self.port = kwargs.pop('port')
        self.host = kwargs.pop('host')
        self.name = kwargs.pop('name')
        self.map = kwargs.pop('map')
        self.gametype = kwargs.pop('gametype')
        self.max_players = kwargs.pop('max_players')
        self.max_clients = kwargs.pop('max_clients')
        self._clients = [Player(**p) for p in kwargs.pop('players')]
        self.timestamp = datetime.utcfromtimestamp(kwargs.pop('timestamp'))

        try:
            self.map_url = BASE_URL + kwargs.pop('map_url')
        except KeyError:
            self.map_url = None

    def __contains__(self, item) -> bool:
        return any(p.name == item for p in self.clients)

    @property
    def title(self) -> str:
        return f'{self.name}: {self.map}'

    @property
    def address(self) -> str:
        return f'{self.host}:{self.port}'

    @property
    def color(self) -> Optional[int]:
        # https://github.com/ddnet/ddnet/blob/f1b54d32b909a3c6fc9e1dc6c37475a1d7c21ec4/src/engine/shared/serverbrowser.cpp
        # https://github.com/ddnet/ddnet/blob/f1b54d32b909a3c6fc9e1dc6c37475a1d7c21ec4/src/game/client/components/menus_browser.cpp#L442-L457
        gametype = self.gametype.lower()
        if self.gametype in ('DM', 'TDM', 'CTF'):
            return 0x82FF7F  # Vanilla
        elif 'catch' in gametype:
            return 0xFCFF7F  # Catch
        elif any(t in gametype for t in ('idm', 'itdm', 'ictf')):
            return 0xFF7F7F  # Instagib
        elif 'fng' in gametype:
            return 0xFC7FFF  # FNG
        elif any(t in gametype for t in ('ddracenet', 'ddnet', 'blockz', 'infectionz')):
            return 0x7EBFFD  # DDNet
        elif any(t in gametype for t in ('ddrace', 'mkrace')):
            return 0xBF7FFF  # DDRace
        elif any(t in gametype for t in ('race', 'fastcap')):
            return 0x7FFFE0  # Race

    @property
    def time_score(self) -> bool:
        # https://github.com/ddnet/ddnet/blob/f1b54d32b909a3c6fc9e1dc6c37475a1d7c21ec4/src/game/client/gameclient.cpp#L1008
        gametype = self.gametype.lower()
        return any(t in gametype for t in ('race', 'fastcap', 'ddnet', 'blockz', 'infectionz'))

    @property
    def clients(self) -> List[Player]:
        return [p for p in self._clients if p.is_connected()]

    @property
    def embeds(self) -> List[discord.Embed]:
        embeds = []

        base = discord.Embed(title=self.title, url=self.map_url, timestamp=self.timestamp, color=self.color)
        base.set_footer(text=self.address)

        spectators = sorted([p for p in self.clients if not p.playing], key=lambda p: p.name.lower())
        if spectators:
            name = f'Spectators [{len(spectators)}/{self.max_clients}]'
            value = ', '.join(p.format() for p in spectators)
            base.add_field(name=name, value=value, inline=False)

        # https://github.com/ddnet/ddnet/blob/38f91d3891eefc392f60f77b1b82ecdb3a47ec62/src/game/client/gameclient.cpp#L1381-L1406
        players = sorted(
            [p for p in self.clients if p.playing],
            key=lambda p: (self.time_score and p.score == -9999, -p.score, p.name.lower())
        )
        if players:
            names = (f'Players [{len(players)}/{self.max_players}]', '\u200b')
            for i in range(0, len(players), 16):
                embed = base.copy()
                for j, name in enumerate(names):
                    pslice = players[i + 8 * j:i + 8 * (j + 1)]
                    if pslice:
                        value = '\n'.join(p.format(self.time_score) for p in pslice)
                        embed.insert_field_at(j, name=name, value=value)

                embeds.append(embed)

        return embeds or [base]


Packets = namedtuple('Packets', 'rx tx')


class ServerInfo:
    __slots__ = ('host', '_online', 'packets')

    PPS_THRESHOLD = 3000  # we usually get max 2 kpps legit traffic so this should be a safe threshold
    PPS_RATIO_MIN = 500  # ratio is not reliable for low traffic
    PPS_RATIO_THRESHOLD = 2.0  # responding to less than half the traffic indicates junk traffic

    COUNTRYFLAGS = {
        'MAIN': '🇪🇺',
        'GER':  '🇩🇪',
        'GER2': '🇩🇪',
        'RUS':  '🇷🇺',
        'CHL':  '🇨🇱',
        'USA':  '🇺🇸',
        'BRA':  '🇧🇷',
        'ZAF':  '🇿🇦',
        'CHN':  '🇨🇳',
    }

    def __init__(self, **kwargs):
        self.host = kwargs.pop('type')
        self._online = kwargs.pop('online4')

        self.packets = Packets(kwargs.pop('packets_rx', -1), kwargs.pop('packets_tx', -1))

    def is_main(self) -> bool:
        return self.host == 'ddnet.tw'

    def is_online(self) -> bool:
        return self._online

    def is_under_attack(self) -> bool:
        return self.packets.rx > self.PPS_THRESHOLD \
            or self.packets.rx > self.PPS_RATIO_MIN and self.packets.rx / self.packets.tx > self.PPS_RATIO_THRESHOLD

    @property
    def status(self) -> str:
        if not self.is_online():
            return 'down'
        elif self.is_under_attack():
            return 'ddos'  # not necessarily correct but easy to understand
        else:
            return 'up'

    @property
    def country(self) -> str:
        if self.is_main():
            return 'MAIN'

        # monkey patch BRA so that country abbreviations are consistent
        return self.host.split('.')[0].upper().replace('BR', 'BRA')

    @property
    def flag(self) -> str:
        return self.COUNTRYFLAGS.get(self.country, FLAG_UNK)

    def format(self) -> str:
        def humanize_pps(pps: int) -> str:
            if pps < 0:
                return ''
            elif pps < 1000:
                return str(pps)
            else:
                return f'{round(pps / 1000, 2)}k'

        return f'{self.flag} `{self.country:^4}|{self.status:^4}|' \
               f'{humanize_pps(self.packets.rx):>7}|{humanize_pps(self.packets.tx):>7}`'


class ServerStatus:
    __slots__ = ('servers', 'timestamp')

    URL = f'{BASE_URL}/status/'

    def __init__(self, servers: List[Dict], updated: str):
        self.servers = [ServerInfo(**s) for s in servers]
        self.timestamp = datetime.utcfromtimestamp(float(updated))

    @property
    def embed(self) -> discord.Embed:
        header = f'{FLAG_UNK} `srv | +- | ▲ pps | ▼ pps `'
        desc = '\n'.join([header] + [s.format() for s in self.servers])
        return discord.Embed(title='Server Status', description=desc, url=self.URL, timestamp=self.timestamp)


class Teeworlds(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def fetch_servers(self) -> List[Server]:
        url = f'{BASE_URL}/status/index.json'
        async with self.bot.session.get(url) as resp:
            if resp.status != 200:
                log.error('Failed to fetch DDNet server data (status code: %d %s)', resp.status, resp.reason)
                raise RuntimeError('Could not fetch DDNet servers')

            js = await resp.json()

            return [Server(**s) for s in js]

    @commands.command()
    async def find(self, ctx: commands.Context, *, player: clean_content=None):
        """Find a player on a DDNet server"""
        player = player or ctx.author.display_name

        try:
            servers = await self.fetch_servers()
        except RuntimeError as exc:
            return await ctx.send(exc)

        servers = [s for s in servers if player in s]
        if not servers:
            return await ctx.send('Could not find that player')

        server = max(servers, key=lambda s: len(s.clients))

        menu = Pages(server.embeds)
        await menu.start(ctx)

    async def fetch_status(self) -> ServerStatus:
        url = f'{BASE_URL}/status/json/stats.json'
        async with self.bot.session.get(url) as resp:
            if resp.status != 200:
                log.error('Failed to fetch DDNet status data (status code: %d %s)', resp.status, resp.reason)
                raise RuntimeError('Could not fetch DDNet status')

            js = await resp.json()

            return ServerStatus(**js)

    @commands.command()
    async def ddos(self, ctx: commands.Context):
        """Display DDNet server status"""
        try:
            status = await self.fetch_status()
        except RuntimeError as exc:
            await ctx.send(exc)
        else:
            await ctx.send(embed=status.embed)


def setup(bot: commands.Bot):
    bot.add_cog(Teeworlds(bot))
