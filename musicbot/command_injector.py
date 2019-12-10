from discord.ext.commands import Command, Group, command, group
import traceback
import inspect
from typing import Optional, Union, Iterable, AnyStr, Set
from .lib.event_emitter import AsyncEventEmitter, on
from .utils import DependencyResolver, isiterable

class CommandGenerator:
    def __init__(self, cmd, *, group = False):
        """
        @TheerapakG: Passing Command object directly is heavily discouraged
        because creating new cmd based on old kwargs uses undocumented feature
        """
        if isinstance(cmd, Command):
            self.group = group or isinstance(cmd, Group)
            self.cmd_func = cmd.callback
            if hasattr(cmd, '__original_kwargs__'):
                self.cmd_kwargs = cmd.__original_kwargs__.copy()
            else:
                self.cmd_kwargs = dict()
        else:
            self.group = group
            self.cmd_func = cmd
            self.cmd_kwargs = dict()

        self.childs = set()

    def __repr__(self):
        return 'CommandGenerator(func = {}, group = {})'.format(self.cmd_func, self.group)

    def add_child(self, cmd_obj: Command):
        self.childs.add(cmd_obj)
        return self

    def make_command(self, **kwargs):
        cmd_kwargs = self.cmd_kwargs
        cmd_kwargs.update(kwargs)
        if self.group:
            new = group(**cmd_kwargs)(self.cmd_func)
        else:
            new = command(**cmd_kwargs)(self.cmd_func)
        self.childs.add(new)
        return new    

class _MarkInject:
    def __init__(self, name, after: Optional[Set], injectfunction, ejectfunction, cmd: CommandGenerator, *, child = None):
        self.name = name
        self.after = after if after else set()
        self.inject = injectfunction
        self.eject = ejectfunction
        self.cmd = cmd
        self.child = child

    def __repr__(self):
        return '_MarkInject(name = {}, cmd = {})'.format(self.name, repr(self.cmd))

class InjectableMixin(AsyncEventEmitter):
    @on('pre_init')
    async def pre_init(self, bot):
        self.bot = bot
        self.log = self.bot.log
        self.injects = dict()
        self.injectdeps = DependencyResolver()

    @on('init')
    async def init(self):
        for item in dir(self):
            if hasattr(type(self), item) and isinstance(getattr(type(self), item), property):
                continue
            iteminst = getattr(self, item)
            if isinstance(iteminst, _MarkInject):
                self.injects[iteminst.name] = iteminst
                while iteminst.child:
                    iteminst.child.after.add(iteminst.name)
                    iteminst = iteminst.child
                    self.injects[iteminst.name] = iteminst

        for item in self.injects.values():
            self.injectdeps.add_item(item.name, item.after)

        satisfied, unsatisfied = self.injectdeps.get_state()

        if unsatisfied:
            self.log.warning('These following injections does not have dependencies required and will not be loaded: {}'.format(', '.join(unsatisfied)))
            for name in unsatisfied:
                self.injectdeps.remove_item(name)
                del self.injects[name]
        
        for name in satisfied:
            item = self.injects[name]
            self.bot.log.debug('injecting with {}'.format(item.inject))
            try:
                item.inject(self.bot, self)
            except:
                self.bot.log.error(traceback.format_exc())

    @on('uninit')
    async def uninit(self):
        unloadlist = self.injectdeps.get_dependents_multiple(self.injects.keys())
        
        for name in unloadlist:
            item = self.injects[name]
            self.bot.log.debug('ejecting with {}'.format(item.eject))
            try:
                item.eject(self.bot)
            except:
                self.bot.log.error(traceback.format_exc())

def ensure_inject(potentially_injected, *, group = False) -> _MarkInject:
    if not isinstance(potentially_injected, _MarkInject):
        if isinstance(potentially_injected, Command):
            return _MarkInject(
                potentially_injected.name,
                None,
                lambda *args, **kwargs: None,
                lambda *args, **kwargs: None,
                CommandGenerator(potentially_injected, group = group).add_child(potentially_injected)
            )
        elif inspect.iscoroutinefunction(potentially_injected):
            return _MarkInject(
                potentially_injected.__name__ if hasattr(potentially_injected, '__name__') else repr(potentially_injected),
                None,
                lambda *args, **kwargs: None,
                lambda *args, **kwargs: None,
                CommandGenerator(potentially_injected, group = group)
            )
        else:
            raise ValueError("unknown type to ensure inject: {}".format(type(potentially_injected)))
    return potentially_injected

def try_append_payload(name, injected: _MarkInject, inject, eject, after:Optional[Union[AnyStr,Iterable[AnyStr]]] = None):
    if not after:
        after = set()
    elif isinstance(after, str):
        after = set([after])
    elif isiterable(after):
        after = set(after)
    return _MarkInject(
        name,
        after,
        inject,
        eject,
        injected.cmd,
        child = injected
    )

def inject_as_subcommand(groupcommand, *, inject_name = None, after:Optional[Union[AnyStr,Iterable[AnyStr]]] = None, **kwargs):
    def do_inject(subcommand):
        subcommand = ensure_inject(subcommand)
        subcmd = subcommand.cmd.make_command(**kwargs)
        def inject(bot, cog):
            bot.log.debug('Invoking inject_as_subcommand injecting {} to {}'.format(subcommand.cmd, groupcommand))
            subcmd.cog = cog
            cmd = bot.get_command(groupcommand)
            cmd.add_command(subcmd)
            bot.alias.fix_chained_command_alias(subcmd, 'injected')

        def eject(bot):
            bot.log.debug('Invoking inject_as_subcommand ejecting {} from {}'.format(subcommand.cmd, groupcommand))
            cmd = bot.get_command(groupcommand)
            cmd.remove_command(subcmd)

        return try_append_payload(
            inject_name if inject_name else 'inject_{}_{}'.format(subcmd.name, groupcommand),
            subcommand, 
            inject, 
            eject,
            after
        )
    return do_inject

def inject_as_group(command):
    return ensure_inject(command, group = True)

def inject_as_cog_subcommand(groupcommand, *, inject_name = None, after:Optional[Union[AnyStr,Iterable[AnyStr]]] = None, **kwargs):
    def do_inject(subcommand):
        subcommand = ensure_inject(subcommand)
        subcmd = subcommand.cmd.make_command(**kwargs)
        def inject(bot, cog):
            bot.log.debug('Invoking inject_as_cog_subcommand injecting {} to {}'.format(subcommand.cmd, groupcommand))
            subcmd.cog = cog
            cmd = cog.get_command(groupcommand)
            cmd.add_command(subcmd)
            bot.alias.fix_chained_command_alias(subcmd, 'injected')

        def eject(bot):
            bot.log.debug('Invoking inject_as_cog_subcommand ejecting {} from {}'.format(subcommand.cmd, groupcommand))
            cmd = bot.get_command(groupcommand)
            cmd.remove_command(subcmd)

        return try_append_payload(
            inject_name if inject_name else 'inject_{}_{}'.format(subcmd.name, groupcommand),
            subcommand, 
            inject, 
            eject,
            after
        )
    return do_inject

def inject_as_main_command(names:Union[AnyStr,Iterable[AnyStr]], *, inject_name = None, after:Optional[Union[AnyStr,Iterable[AnyStr]]] = None, **kwargs):
    if isinstance(names, str):
        names = (names, )

    def do_inject(command):
        command = ensure_inject(command)
        def inject(bot, cog):
            bot.log.debug('Invoking inject_as_main_command injecting {} as {}'.format(command.cmd, names))
            for name in names:
                cmd = command.cmd.make_command(name = name, **kwargs)
                cmd.cog = cog
                bot.add_command(cmd)

        def eject(bot):
            bot.log.debug('Invoking inject_as_main_command ejecting {}'.format(names))
            for name in names:
                bot.remove_command(name)

        return try_append_payload(
            inject_name if inject_name else 'inject_{}'.format('_'.join(names)),
            command, 
            inject, 
            eject,
            after
        )
    return do_inject
