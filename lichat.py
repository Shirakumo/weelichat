# -*- coding: utf-8 -*-
# boilerplate based on cmd_help.py
#
SCRIPT_NAME = 'lichat'
SCRIPT_AUTHOR = 'Georgiy Tugai <georgiy@crossings.link>, Nicolas Hafner <shinmera@tymoon.eu>'
SCRIPT_VERSION = '0.9'
SCRIPT_LICENSE = 'zlib'
SCRIPT_DESC = 'Client for Lichat protocol (https://shirakumo.github.io/lichat)'

import_ok = True

import sys
if sys.version_info[0] < 3 or sys.version_info[1] < 7:
    print('Your Python version ('+str(sys.version_info[0])+'.'+str(sys.version_info[1])+') is too old!')
    print('Please update to 3.7 or later.')
    import_ok = False

try:
    import weechat as w
except ImportError:
    print('This script must be run under WeeChat.')
    print('Get WeeChat now at: http://www.weechat.org/')
    import_ok = False

try:
    from functools import wraps
    from inspect import signature
    from pathlib import Path
    import shlex
    import json
    import urllib.request
    import socket
    import base64
    import re
    import mimetypes
    import time
    import pylichat
    import inspect
    import logging
    import logging.handlers
    from pylichat import Client, ConnectionFailed
    from pylichat.update import *
    from pylichat.symbol import kw, li
    import pylichat.wire
    logger = logging.getLogger('lichat')
except ImportError as message:
    print('Missing package(s) for %s: %s' % (SCRIPT_NAME, message))
    import_ok = False

logtraceback = False
logfilehandler = None
logweehandler = None

class WeechatHandler(logging.Handler):
    def emit(self, record):
        try:
            prefix = ""
            if record.levelno >= logging.ERROR:
                prefix = w.prefix("error")
            elif record.levelno < logging.INFO:
                prefix = w.color("gray")

            if not logtraceback and record.exc_info:
                # Strip traceback info but leave the exception type & string alone
                bk = record.exc_info[2]
                record.exc_info = (record.exc_info[0], record.exc_info[1], None)
                fmt = self.format(record)
                record.exc_info = (record.exc_info[0], record.exc_info[1], bk)
            else:
                fmt = self.format(record)

            w.prnt_date_tags("", 0,
                             f"no_highlight,log5,lichat_log,lichat_log_{record.levelname.lower()}",
                             f"{prefix}{fmt}")
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)

data_save_directory = ''
data_save_types = []
imgur_client_id = ''
imgur_formats = ['video/mp4', 'video/webm', 'video/x-matroska', 'video/quicktime',
                 'video/x-flv', 'video/x-msvideo', 'video/x-ms-wmv', 'video/mpeg',
                 'image/png', 'image/jpeg', 'image/gif', 'image/tiff', 'image/vnd.mozilla.apng']
config_file = None
config = {}
commands = {}
servers = {}

def register_command(name, func, description='', cmdtype='lichat', completion=''):
    commands[name] = {'name': name, 'func': func, 'description': description, 'cmdtype': cmdtype, 'completion': completion}

def call_command(buffer, name, *args):
    return commands[name]['func']('', buffer.buffer, args)

def find_buffer(server, channel):
    server = servers.get(server, None)
    if server != None:
        return server.buffers.get(channel, None)

def weechat_buffer_to_representation(buffer):
    server = w.buffer_get_string(buffer, 'localvar_lichat_server')
    channel = w.buffer_get_string(buffer, 'localvar_lichat_channel')
    return find_buffer(server, channel)
    
def lichat_buffer_input_cb(_data, w_buffer, input_data):
    buffer = weechat_buffer_to_representation(w_buffer)
    buffer.send(Message, text=input_data)
    return w.WEECHAT_RC_OK

def lichat_buffer_close_cb(_data, w_buffer):
    buffer = weechat_buffer_to_representation(w_buffer)
    if buffer.server.is_connected():
        buffer.send(Leave)
    buffer.delete()
    return w.WEECHAT_RC_OK

def lichat_socket_cb(name, fd):
    server = servers[name]
    try:
        for update in server.client.recv():
            server.client.handle(update)
    except pylichat.ConnectionLost:
        logger.info(f"[{name}] connection lost", exc_info=True)
        server.disconnected_error()
    except Exception as e:
        logger.exception(f"[{name}] error in lichat_socket_cb")
    return w.WEECHAT_RC_OK

def input_prompt_cb(data, item, current_window, w_buffer, extra_info):
    buffer = weechat_buffer_to_representation(w_buffer)
    if buffer == None:
        return ''
    
    return f"{w.color(w.config_color(w.config_get('irc.color.input_nick')))}{buffer.server.client.username}"

def reconnect_cb(data, _remaining):
    server = servers.get(data, None)
    if server != None:
        server.reconnect()
    return w.WEECHAT_RC_OK

def timeout_cb(data, _remaining):
    server = servers.get(data, None)
    if server != None:
        server.show(text="Timed out, reconnecting...")
        server.disconnect()
        server.reconnect()
    return w.WEECHAT_RC_OK

def format_alist(list, key_separator=': ', entry_separator='\n'):
    return entry_separator.join([f"{x[0]}{key_separator}{x[1]}" for x in list])

def search_buffer(w_buffer, matcher, gather=True):
    h_line = w.hdata_get('line')
    h_line_data = w.hdata_get('line_data')
    lines = w.hdata_pointer(w.hdata_get('buffer'), w_buffer, 'own_lines')
    line = w.hdata_pointer(w.hdata_get('lines'), lines, 'last_line')

    while line and not matcher(h_line, h_line_data, line):
        line = w.hdata_move(h_line, line, -1)

    if gather:
        line_ptrs = []
        while line and matcher(h_line, h_line_data, line):
            line_ptrs.append(line)
            line = w.hdata_move(h_line, line, -1)
            line_ptrs.reverse()
        return line_ptrs
    return None

def edit_buffer(w_buffer, matcher, new_text):
    line_ptrs = search_buffer(w_buffer, matcher)
    if not line_ptrs: return False

    line_text = new_text.split('\n', len(line_ptrs)-1)
    line_text = [line.replace('\n', ' | ') for line in line_text]
    line_text += [''] * (len(line_ptrs) - len(line_text))

    h_line = w.hdata_get('line')
    h_line_data = w.hdata_get('line_data')
    for line, text in zip(line_ptrs, line_text):
        data = w.hdata_pointer(h_line, line, 'data')
        w.hdata_update(h_line_data, data, {'message': text})
    return True

class Buffer:
    name = None
    server = None
    buffer = None
    channel = None
    nicklist = None

    def __init__(self, server, channel, name=None):
        if name == None: name = channel
        self.name = name
        self.server = server
        self.channel = channel
        self.buffer = w.buffer_new(self.w_name(),
                                   'lichat_buffer_input_cb', '',
                                   'lichat_buffer_close_cb', '')
        w.buffer_set(self.buffer, 'nicklist', '1')
        w.buffer_set(self.buffer, 'nicklist_case_sensitive', '0')
        w.buffer_set(self.buffer, 'nicklist_display_groups', '0')
        w.buffer_set(self.buffer, 'short_name', name)
        w.buffer_set(self.buffer, 'type', 'formatted')
        w.buffer_set(self.buffer, 'notify', '1')
        w.buffer_set(self.buffer, 'filter', '1')
        w.buffer_set(self.buffer, 'input_multiline', '1')
        w.buffer_set(self.buffer, 'highlight_words', ','.join(server.highlight()))
        w.buffer_set(self.buffer, 'localvar_set_server', server.name)
        w.buffer_set(self.buffer, 'localvar_set_channel', channel)
        w.buffer_set(self.buffer, 'localvar_set_nick', server.client.username)
        if server.client.servername == channel:
            w.buffer_set(self.buffer, 'localvar_set_type', 'server')
        else:
            w.buffer_set(self.buffer, 'localvar_set_type', 'channel')
        w.buffer_set(self.buffer, 'localvar_set_lichat_server', server.name)
        w.buffer_set(self.buffer, 'localvar_set_lichat_channel', channel)
        w.buffer_set(self.buffer, 'localvar_set_lichat_complete_index', '0')
        w.buffer_set(self.buffer, 'localvar_set_lichat_complete_prefix', '')
        server.buffers[channel] = self

    def disconnect(self, show=True):
        if show:
            self.show(text='Disconnected.', kind='network')
        w.nicklist_remove_all(self.buffer)
        self.nicklist = None

    def join(self, user):
        if self.nicklist == None:
            self.nicklist = w.nicklist_add_group(self.buffer, '', 'Users', 'weechat.color.nicklist_group', 1)
        w.nicklist_add_nick(self.buffer, self.nicklist, user, 'bar_fg', '', 'bar_fg', 1)

    def leave(self, user):
        if self.nicklist != None:
            nick = w.nicklist_search_nick(self.buffer, '', user)
            if nick != None:
                w.nicklist_remove_nick(self.buffer, nick)

    def w_name(self):
        return f"lichat.{self.server.name}.{self.name}"

    def info(self, key):
        return self.server.client.channels[self.channel][key]

    def delete(self):
        del self.server.buffers[self.channel]

    def complete_channel(self, args):
        if args.get('channel', None) == None:
            args['channel'] = self.channel
        elif args['channel'].startswith('/'):
            if self.server.is_supported('shirakumo-channel-trees'):
                args['channel'] = self.channel + args['channel']

    def make_instance(self, type, **args):
        if issubclass(type, ChannelUpdate):
            self.complete_channel(args)
        return self.server.client.make_instance(type, **args)
            
    def send(self, type, **args):
        if issubclass(type, ChannelUpdate):
            self.complete_channel(args)
        return self.server.send(type, **args)

    def send_confirm(self, message, type, **args):
        def callback(_client, _previous, update):
            if isinstance(update, Failure):
                self.show(update, kind='error')
            else:
                self.show(update, text=message)
        self.send_cb(callback, type, **args)

    def send_cb(self, cb, type, **args):
        if issubclass(type, ChannelUpdate):
            self.complete_channel(args)
        return self.server.send_cb(cb, type, **args)

    def display(self):
        w.buffer_set(self.buffer, 'display', '1')

    def show(self, update=None, text=None, kind='action', tags=[]):
        time = 0
        prefix_color = ""

        if update is None:
            update = {'from': self.server.client.servername}
        else:
            time = update.unix_clock()
            tags.append(f"lichat_type_{update.__class__.__name__.lower()}")
            if update.get('id'):
                tags.append(f"lichat_id_{str(update['id'])}")
            if update.get('from'):
                tags.append(f"lichat_from_{update['from']}")
                tags.append(f"nick_{update['from'].replace(' ','_')}")

        if text is None:
            if isinstance(update, Update):
                text = update.get('text', f"Update of type {type(update).__name__}")
            else:
                text = f"BUG: Supposed to show non-update {update}"

        if update.get('from') and (kind in ["text", "action"]
                                   or w.config_boolean(w.config_get("irc.look.color_nicks_in_server_messages"))):
            prefix_color = w.color(w.info_get("nick_color_name", update['from']))

        tags = ','.join(tags)
        source = f"{prefix_color}{update['from']}"

        if update.get('bridge'):
            source = f"{source}{w.color('reset')}*"

        if kind == 'text':
            w.prnt_date_tags(self.buffer, time, tags, f"{source}\t{text}")
            w.buffer_set(self.buffer, 'hotlist', '2')
        else:
            w.prnt_date_tags(self.buffer, time, tags, f"{w.prefix(kind)}{source}{w.color('reset')}: {text}")
            w.buffer_set(self.buffer, 'hotlist', '1')
        return self

    def edit(self, update, text=None):
        id = 'lichat_id_'+str(update['id'])
        source = 'lichat_from_'+update['from']
        if text == None: text = update['text']

        def matcher(h_line, h_line_data, line):
            found_id = False
            found_source = False
            data = w.hdata_pointer(h_line, line, 'data')
            for i in range(w.hdata_integer(h_line_data, data, 'tags_count')):
                tag = w.hdata_string(h_line_data, data, f"{i}|tags_array")
                if tag == id: found_id = True
                if tag == source: found_source = True
                if found_id and found_source: return True
            return False
        
        return edit_buffer(self.buffer, matcher, text)

class Server:
    name = None
    client = None
    buffers = {}
    hook = None
    timeout = None

    def __init__(self, name=None, username=None, password=None, host='chat.tymoon.eu', port=1111, ssl=False):
        client = Client(username, password)
        self.buffers = pylichat.toolkit.CaseInsensitiveDict()
        self.name = name
        self.client = client
        self.host = host
        self.port = port
        self.ssl = ssl
        
        emote_dir = w.info_get('weechat_dir', '')+'/lichat/emotes/'+self.host+'/'
        w.mkdir_parents(emote_dir, 0o755)
        client.reload_emotes(emote_dir)

        def on_connect(client, update):
            for channel in self.config('autojoin', str, '').split('  '):
                self.send(Join, channel=channel)

        def on_disconnect(client, update):
            for channel in self.buffers:
                self.buffers[channel].disconnect()
            if self.timeout != None:
                w.unhook(self.timeout)
                self.timeout = None
            if self.hook != None:
                w.unhook(self.hook)
                self.hook = None
                if self.config('autoreconnect', bool):
                    cooldown = max(1, self.config('autoreconnect_delay', int))
                    self.show(text=f"Reconnecting in {cooldown} seconds...", kind='network')
                    w.hook_timer(cooldown * 1000, 1, 1, 'reconnect_cb', self.name)
        
        def on_misc(client, update):
            if self.timeout != None:
                w.unhook(self.timeout)
                self.timeout = None
            if self.hook != None:
                self.timeout = w.hook_timer(1000*60, 1, 1, 'timeout_cb', self.name)
            
            if isinstance(update, Failure):
                self.show(update, kind='error', tags=['irc_error', 'log3'])

        def on_message(client, update):
            buffer = self.show(update, kind='text', tags=['notify_message', 'irc_privmsg', 'log1'])

        def on_pause(client, update):
            if update.by == 0:
                self.show(update, text=f"has disabled pause mode in {u.channel}", tags=['no_highlight', 'log3'])
            else:
                self.show(update, text=f"has enabled pause mode by {u.by} in {u.channel}", tags=['no_highlight', 'log3'])

        def on_emote(client, update):
            self.client.emotes[update.name].offload(emote_dir)

        def on_data(client, update):
            data = update.__dict__
            data['server'] = name
            if update['from'] != self.client.username:
                if imgur_client_id != '' and update['content-type'] in imgur_formats:
                    w.hook_process('func:upload_file', 0, 'process_upload', json.dumps(data))
                    self.show(update, text=f"Sent file {update['filename']} (Uploading...)")
                elif data_save_directory != '' and (data_save_types == ['all'] or update['content-type'] in data_save_types):
                    data['url'] = f"{data_save_directory}/{time.strftime('%Y.%m.%d-%H-%M-%S')}-{data['filename']}"
                    w.hook_process('func:write_file', 0, 'process_upload', json.dumps(data))
                    self.show(update, text=f"Sent file {update['filename']} (Saving...)")
                else:
                    self.show(update, text=f"Sent file {update['filename']} ({update['content-type']})")
            else:
                self.show(update, text=f"Sent file {update['filename']} ({update['content-type']})")

        def on_channel_info(client, update):
            (_, name) = update.key
            tags = ['no_highlight', 'log3']
            if name == 'topic':
                tags.append('irc_topic')
            text = update.text
            if 256 < len(text):
                text = text[:253]+"..."
            buffer = self.show(update, text=f"{name}: {text}", tags=tags)
            if name == 'topic':
                w.buffer_set(buffer.buffer, 'title', update.text)

        def on_join(client, update):
            buffer = self.show(update, text=f"joined {update.channel}", kind='join', tags=['irc_join', 'no_highlight', 'log4'])
            buffer.join(update['from'])
            if update.channel == self.client.servername:
                buffer.show(text=f"Supported extensions: {', '.join(self.client.extensions)}")

        def on_leave(client, update):
            buffer = self.show(update, text=f"left {update.channel}", kind='quit', tags=['irc_part', 'no_highlight', 'log4'])
            if update['from'] == self.client.username:
                buffer.disconnect(False)
            else:
                buffer.leave(update['from'])

        def on_kick(client, update):
            self.show(update, text=f"has kicked {update.target} from {update.channel}", kind='quit', tags=['irc_kick', 'no_highlight', 'log4'])

        def on_edit(client, update):
            self.buffers[update.channel].edit(update);

        def on_react(client, update):
            ## FIXME: use a separate dedicated reactions line below each message.
            self.show(update, text=f"{update['from']} reacted with {update.emote}", tags=['no_highlight', 'log4'])

        def on_users(client, update):
            buffer = self.buffers[update.channel];
            for user in update.users:
                buffer.join(user)

        client.add_handler(Connect, on_connect)
        client.add_handler(Disconnect, on_disconnect)
        client.add_handler(Update, on_misc)
        client.add_handler(Message, on_message)
        client.add_handler(Join, on_join)
        client.add_handler(Leave, on_leave)
        client.add_handler(Kick, on_kick)
        client.add_handler(Pause, on_pause)
        client.add_handler(Emote, on_emote)
        client.add_handler(Data, on_data)
        client.add_handler(Edit, on_edit)
        client.add_handler(React, on_react)
        client.add_handler(Users, on_users)
        client.add_handler(SetChannelInfo, on_channel_info)
        servers[name] = self

    def config(self, key, type=str, default=None, evaluate=False):
        return cfg('server', self.name+'.'+key, type, default, evaluate)

    def highlight(self):
        parts = self.config('highlight', str, 'username').split(',') + cfg('behaviour', 'highlight', str, '').split(',')
        return [ self.client.username if x == 'username' else x for x in parts ]

    def is_supported(self, extension):
        return self.client.is_supported(extension)

    def is_connected(self):
        return self.hook != None

    def connect(self):
        if self.hook == None:
            self.client.connect(self.host, self.port, ssl=self.ssl)
            self.hook = w.hook_fd(self.client.socket.fileno(), 1, 0, 1, 'lichat_socket_cb', self.name)

    def disconnect(self):
        if self.timeout != None:
            w.unhook(self.timeout)
            self.timeout = None
        if self.hook != None:
            w.unhook(self.hook)
            self.hook = None
            self.client.disconnect()

    def disconnected_error(self):
        logger.debug(f"[{self.name}] disconnected_error")
        self.client.handle(pylichat.update.make_instance(pylichat.update.Disconnect))

    def reconnect(self):
        if self.hook != None: return
        try:
            self.show(text='Reconnecting...')
            self.connect()
        except:
            cooldown = max(1, self.config('autoreconnect_delay', int))
            self.show(text=f"Reconnect failed. Attempting again in {cooldown} seconds.", kind='network')
            w.hook_timer(cooldown * 1000, 1, 1, 'reconnect_cb', self.name)

    def delete(self):
        self.client.disconnect()
        self.buffers.clear()
        del servers[name]

    def send(self, type, **args):
        try:
            return self.client.send(type, **args)
        except pylichat.ConnectionLost:
            logger.info(f"[{self.name}] connection lost", exc_info=True)
            self.disconnected_error()

    def send_cb(self, cb, type, **args):
        try:
            return self.client.send_callback(cb, type, **args)
        except pylichat.ConnectionLost:
            logger.info(f"[{self.name}] connection lost", exc_info=True)
            self.disconnected_error()

    def show(self, update=None, text=None, kind='action', tags=[], buffer=None):
        if buffer == None and isinstance(update, UpdateFailure):
            origin = self.client.origin(update)
            if origin != None and not isinstance(origin, Leave):
                buffer = origin.get('channel', None)
        if buffer == None and update != None:
            buffer = update.get('channel', None)
        if buffer == None:
            buffer = self.client.servername
        if isinstance(buffer, str):
            name = buffer
            buffer = self.buffers.get(name, None)
            if buffer == None:
                buffer = Buffer(self, name)
        return buffer.show(update=update, text=text, kind=kind, tags=tags)

### Commands
def check_signature(f, args, command=None):
    sig = signature(f)
    try:
        sig.bind(*args)
        return True
    except TypeError:
        if command:
            # try to figure out if it was too many, or too few
            try:
                # if this succeeds, there were not enough arguments
                sig.bind_partial(*args)
                w.prnt("", f"{w.prefix('error')}lichat: Too few arguments for command \"{command}\"")
            except TypeError:
                w.prnt("", f"{w.prefix('error')}lichat: Too many arguments for command \"{command}\"")
        return False

def raw_command(name, completion='', description=''):
    def nested(f):
        @wraps(f)
        def wrapper(_data, w_buffer, args_str):
            args = args_str
            if isinstance(args, str):
                args = shlex.split(args)
            args.pop(0)
            if check_signature(f, [w_buffer, *args], command=name):
                f(w_buffer, *args)
            else:
                return w.WEECHAT_RC_ERROR
            return w.WEECHAT_RC_OK
        register_command(name, wrapper, description, cmdtype='raw', completion=completion)
        return wrapper
    return nested

def lichat_command(name, completion='', description=''):
    def nested(f):
        @wraps(f)
        def wrapper(_data, w_buffer, args_str):
            buffer = weechat_buffer_to_representation(w_buffer)
            if buffer is None:
                return w.WEECHAT_RC_OK
            args = args_str
            if isinstance(args, str):
                args = shlex.split(args)
            args.pop(0)
            if check_signature(f, [buffer, *args], command=name):
                f(buffer, *args)
            return w.WEECHAT_RC_OK_EAT
        register_command(name, wrapper, description, cmdtype='lichat', completion=completion)
        return wrapper
    return nested

def handle_failure(buffer):
    def nested(f):
        @wraps(f)
        def wrapper(_client, _prev, update):
            if isinstance(update, Failure):
                buffer.show(update, kind='error')
            else:
                f(update)
        return wrapper
    return nested

def lichat_command_cb(data, w_buffer, args_str):
    args = shlex.split(args_str)
    if len(args) == 0:
        return w.WEECHAT_RC_ERROR

    name = args[0]
    command = commands.get(name, None)
    if command == None:
        w.prnt(w_buffer, f"{w.prefix('error')}Error with command \"/lichat {args_str}\" (help on command: /help lichat)")
        return w.WEECHAT_RC_ERROR

    if command['cmdtype'] == 'lichat' and weechat_buffer_to_representation(w_buffer) is None:
        w.prnt(w_buffer, f"{w.prefix('error')}lichat: command \"lichat {name}\" must be executed on lichat buffer (server, channel or private)")
        return w.WEECHAT_RC_ERROR

    return command['func'](data, w_buffer, args)

def try_connect(w_buffer, server):
    try:
        server.connect()
    except ConnectionFailed as e:
        if isinstance(e.update, InvalidPassword):
            w.prnt(w_buffer, f"[{server.name}] The password is invalid!")
        elif isinstance(e.update, NoSuchProfile):
            w.prnt(w_buffer, f"[{server.name}] The given username is not registered and does not require a password!")
        elif isinstance(e.update, TooManyConnections):
            w.prnt(w_buffer, f"[{server.name}] The server has too many connections and refused yours.")
        elif isinstance(e.update, TextUpdate):
            w.prnt(w_buffer, f"[{server.name}] Failed to connect: {e.update.text}")
        else:
            w.prnt(w_buffer, f"[{server.name}] Failed to connect: {e}")
    except Exception as e:
        logger.exception(f"[{server.name}] Failed to connect to {server.host}:{server.port} {'with SSL' if server.ssl else ''}")

@raw_command('connect', '%(lichat_server)', 'Connect to a lichat server. If no server name is passed, all servers are connected. If a hostname is passed, a new server connection is created.')
def connect_command_cb(w_buffer, name=None, host=None, port=None, username=None, password=None, ssl=None):
    if name == None:
        for server in servers:
            if not servers[server].is_connected():
                try_connect(w_buffer, servers[server])
    elif host != None:
        if name in servers:
            w.prnt(w_buffer, f"f{w.prefix('error')} A server of that name already exists.")
            return
        if port == None: port = cfg('server_default', 'port', int)
        else: port = int(port)
        if username == None: username = cfg('server_default', 'username')
        if password == None: password = cfg('server_default', 'password')
        if ssl == None: ssl = cfg('server_default', 'ssl', bool)
        if ssl == 'on': ssl = True
        if ssl == 'off': ssl = False
        try_connect(w_buffer, Server(name=name, username=username, password=evaluate_string(password), host=host, port=port, ssl=ssl))
        config_section(config_file, 'server', [
            {'name': f'{name}.host', 'default': host},
            {'name': f'{name}.port', 'default': port, 'min': 1, 'max': 65535},
            {'name': f'{name}.username', 'default': username},
            {'name': f'{name}.password', 'default': password},
            {'name': f'{name}.ssl', 'default': ssl},
            {'name': f'{name}.autoconnect', 'default': True},
            {'name': f'{name}.autojoin', 'default': ''}
        ])
    elif name not in servers:
        w.prnt(w_buffer, f"{w.prefix('error')} No such server {name}")
    else:
        try_connect(w_buffer, servers[name])


@lichat_command('disconnect', '%(lichat_server) %-', 'Disconnect from a lichat server. If no name is given, the server of the current channel is disconnected.')
def disconnect_command_cb(buffer, server=None):
    if server != None:
        server = servers[server]
    else:
        server = buffer.server
    server.disconnect()

@raw_command('help', '%(lichat_command) %-', 'Display help information about lichat commands.')
def help_command_cb(w_buffer, topic=None):
    if topic == None:
        for name in sorted(commands):
            command = commands[name]
            w.prnt(w_buffer, f"{name}\t{command['description']}")
    else:
        command = commands.get(topic, None)
        if command == None:
            w.prnt(w_buffer, f"{w.prefix('error')} No such command {command}")
        else:
            sig = signature(command['func'])
            parameters = sig.parameters.copy()
            parameters.popitem(last=False)
            sig = sig.replace(parameters=parameters.values())
            w.prnt(w_buffer, f"/lichat {command['name']} {sig}")
            w.prnt(w_buffer, f"{command['description']}")

@lichat_command('join', '%(lichat_channel) %-', 'Join an existing channel.')
def join_command_cb(buffer, channel=None):
    @handle_failure(buffer)
    def join_cb(update):
        if update.channel not in buffer.server.buffers:
            Buffer(buffer.server, update.channel)
        buffer.server.buffers[update.channel].display()
    buffer.send_cb(join_cb, Join, channel=channel)

@lichat_command('leave', '%(lichat_channel) %-', 'Leave a channel you\'re in. Defaults to the current channel.')
def leave_command_cb(buffer, channel=None):
    buffer.send(Leave, channel=channel)

@lichat_command('create', '', 'Create a new channel. If no name is given, an anonymous channel is created.')
def create_command_cb(buffer, channel=''):
    buffer.send(Create, channel=channel)

@lichat_command('pull', '%(nicks) %(lichat_channel) %-', 'Pull another user into a channel. If no channel name is given, defaults to the current channel.')
def pull_command_cb(buffer, user, channel=None):
    buffer.send(Pull, channel=channel, target=user)

@lichat_command('kick', '%(nicks) %(lichat_channel) %-', 'Kicks another user from a channel. If no channel name is given, defaults to the current channel.')
def kick_command_cb(buffer, user, channel=None):
    buffer.send(Kick, channel=channel, target=user)

@lichat_command('kickban', '%(nicks) %(lichat_channel) %-', 'Kicks another user from a channel and removes their join permission. If no channel name is given, defaults to the current channel.')
def kickban_command_cb(buffer, user, channel=None):
    call_command(buffer, 'deny', 'join', user, channel)
    call_command(buffer, 'kick', user, channel)

@lichat_command('register', '', 'Register your account with a password. If successful, will save the password to config.')
def register_command_cb(buffer, password):
    @handle_failure(buffer)
    def reg_cb(update):
        if isinstance(update, Register):
            w.config_option_set(config['server'][buffer.server.name+'.password'], password, 0)
        buffer.show(update, text="Profile registered. Password has been saved.")
    buffer.send_cb(reg_cb, Register, password=password)

@lichat_command('set-channel-info', '%(lichat_channel_key) %-', """Set channel information in the current channel.
The key must be a lichat symbol. By default the following symbols are recognised:
  :news
  :topic
  :rules
  :contact
However, a server may support additional symbols.""")
def set_channel_info_command_cb(buffer, key, *value):
    buffer.send(SetChannelInfo, key=pylichat.wire.from_string(key), text=' '.join(value))

@lichat_command('channel-info', '%(lichat_channel_key)|T %(lichat_channel) %-', 'Retrieve channel information. If no channel name is given, defaults to the current channel. If no key is given, all channel info is requested.')
def channel_info_command_cb(buffer, key='T', channel=None):
    key = pylichat.wire.from_string(key)
    buffer.send(ChannelInfo, channel=channel, key=key)

@lichat_command('topic', 'View or set the topic of the current channel.')
def topic_command_cb(buffer, *topic):
    if len(topic) == 0:
        topic = buffer.info(kw('topic'))
        if topic == None:
            topic = "No topic set."
        buffer.show(text=topic)
    else:
        buffer.send(SetChannelInfo, key=kw('topic'), text=' '.join(topic))

@lichat_command('pause', '0 %(lichat_channel) %-', 'Set the pause mode of the channel. If no channel name is given, defaults to the current channel. If no pause time is given, pause-mode is ended.')
def pause_command_cb(buffer, pause="0", channel=None):
    buffer.send(Pause, channel=channel, by=int(pause))

@lichat_command('quiet', '%(nicks) %(lichat_channel) %-', 'Quiets the given user. If no channel name is given, defaults to the current channel.')
def quiet_command_cb(buffer, target, channel=None):
    buffer.send_confirm(f"The user {target} has been quieted. Their messages will no longer be visible.",
                        Quiet, channel=channel, target=target)

@lichat_command('unquiet', '%(nicks) %(lichat_channel) %-', 'Unquiets the given user. If no channel name is given, defaults to the current channel.')
def unquiet_command_cb(buffer, target, channel=None):
    buffer.send_confirm(f"The user {target} has been allowed messaging again.",
                        Unquiet, channel=channel, target=target)

@lichat_command('kill', '%(nicks) %-', 'Kills the connections of a user.')
def kill_command_cb(buffer, target):
    buffer.send_confirm(f"The user {target} has been killed.",
                        Kill, target=target)

@lichat_command('destroy', '%(lichat_channel) %-', 'Destroys a channel immediately.  If no channel name is given, defaults to the current channel.')
def destroy_command_cb(buffer, channel=None):
    buffer.send_confirm(f"The user {target} has been killed.",
                        Destroy, channel=channel)

@lichat_command('ban', '%(nicks) %-', 'Bans the given user from the server by username.')
def ban_command_cb(buffer, target):
    buffer.send_confirm(f"The user {target} has been banned.",
                        Ban, target=target)

@lichat_command('unban', '%(nicks) %-', 'Unbans the given username from the server.')
def unban_command_cb(buffer, target):
    buffer.send_confirm(f"The user {target} has been unbanned.",
                        Unban, target=target)

@lichat_command('ip-ban', '', 'Bans the given IP address from the server. Set bits in the given mask will be ignored when comparing IPs.')
def ip_ban_command_cb(buffer, ip, mask='::'):
    buffer.send_confirm(f"The ip {ip} under {mask} has been banned.",
                        IpBan, ip=ip, mask=mask)

@lichat_command('ip-unban', '', 'Unbans the given IP address from the server. Set bits in the given mask will be ignored when comparing IPs.')
def ip_unban_command_cb(buffer, ip, mask='::'):
    buffer.send_confirm(f"The ip {ip} under {mask} has been unbanned.",
                        IpUnban, ip=ip, mask=mask)

@lichat_command('message', '%(lichat_channel) %-', 'Send a message to the given channel.')
def message_command_cb(buffer, channel, *args):
    buffer.send(Message, channel=channel, message=' '.join(args))

@lichat_command('users', '%(lichat_channel) %-', 'List the users of the given channel. If no channel name is given, defaults to the current channel.')
def users_command_cb(buffer, channel=None):
    @handle_failure(buffer)
    def callback(users):
        buffer.show(text=f"Currently in channel: {' '.join(users.users)}")
    buffer.send_cb(callback, Users, channel=channel)

@lichat_command('channels', '%(lichat_channel) %-', 'List the channels of the current server. If the server supports the channel-trees extension, only channels below the specified channel are returned. If no channel is specified, all top-level channels are returned.')
def channels_command_cb(buffer, channel=''):
    @handle_failure(buffer)
    def callback(channels):
        buffer.show(text=f"Channels: {' '.join(channels.channels)}")
    buffer.send_cb(callback, Channels, channel=channel)

@lichat_command('user-info', '%(nicks) %-', 'Request information on the given user.')
def user_info_command_cb(buffer, target):
    @handle_failure(buffer)
    def callback(info):
        registered = 'registered'
        if not info.registered:
            registered = 'not registered'
        buffer.show(text=f"Info on {target}: {target.connections} connections, {registered}")
        if info.info != None:
            for entry in info.info:
                if entry[0] != ('keyword', 'icon'):
                    buffer.show(text=f"  {entry[0][1]}: entry[1]")
    buffer.send_cb(callback, UserInfo, target=target)

@lichat_command('grant', '%(lichat_update) %(nicks) %(lichat_channel) %-', 'Grant permission for an update to a user. If no user is given, the permission is granted to everyone. If no channel name is given, defaults to the current channel.')
def grant_command_cb(buffer, update, target=None, channel=None):
    type = li(update)
    if target == None:
        buffer.send_confirm(f"All users have been allowed {update}ing",
                            Permissions, channel=channel, permissions=[[type, True]])
    else:
        buffer.send_confirm(f"{target} has been allowed {update}ing",
                            Grant, channel=channel, target=target, update=type)

@lichat_command('deny', '%(lichat_update) %(nicks) %(lichat_channel) %-', 'Deny permission for an update from a user. If no user is given, the permission is denied to everyone but you. If no channel name is given, defaults to the current channel.')
def deny_command_cb(buffer, update, target=None, channel=None):
    type = li(update)
    if target == None:
        buffer.send_confirm(f"All users have been denied from {update}ing",
                            Permissions, channel=channel, permissions=[[type, [li('+'), buffer.server.client.username]]])
    else:
        buffer.send_confrm(f"{target} has been denied from {update}ing",
                           Deny, channel=channel, target=target, update=type)

@lichat_command('send', '%(filename) %(lichat_channel) %-', 'Send a local file or file from an URL as a data upload. If no channel name is given, defaults to the current channel.')
def send_command_cb(buffer, file, channel=None):
    update = buffer.make_instance(Data, channel=channel)
    data = update.__dict__
    data['server'] = buffer.server.name
    data['url'] = file
    if re.match('^(\\w:|/|~)', file):
        w.hook_process('func:read_file', 0, 'process_send', json.dumps(data))
    else:
        w.hook_process('func:download_file', 0, 'process_send', json.dumps(data))
    buffer.show(update, text=f"Sending file...")

@lichat_command('capabilities', '%(lichat_channel) %-', 'Check what capabilities you have. If no channel name is given, defaults to the current channel.')
def capabilities_command_cb(buffer, channel=None):
    @handle_failure(buffer)
    def callback(info):
        buffer.show(text=f"You are permitted the following: {', '.join([x[1] for x in info.permitted])}")
    buffer.send_cb(callback, Capabilities, channel=channel)

@lichat_command('server-info', '%(nicks) %-', 'Check server information on a user.')
def server_info_command_cb(buffer, target):
    @handle_failure(buffer)
    def callback(info):
        attributes = format_alist(info.attributes, entry_separator='\n    ')
        connections = '\n'.join([format_alist(x, entry_separator='\n    ') for x in info.connections])
        buffer.show(text=f"""Server information on {target}:
  Attributes:
    {connections}
  Connections:
    {connections}
""")
    buffer.send_cb(callback, ServerInfo, target=target)

@lichat_command('edit', '1', 'Edit a previous message.')
def edit_command_cb(buffer, line=None, *text):
    if line == None:
        pass # TODO: interactive edit selection
    else:
        text = ''
        if line.isdigit():
            line = int(line)
            text = ' '.join(text)
        else:
            text = line+' '+' '.join(text)
            line = 1
        source = 'lichat_from_'+buffer.server.client.username
        message = 'lichat_type_message'
        seen_ids = ['']
        def matcher(h_line, h_line_data, line):
            found_source = False
            found_message = False
            id = None
            data = w.hdata_pointer(h_line, line, 'data')
            for i in range(w.hdata_integer(h_line_data, data, 'tags_count')):
                tag = w.hdata_string(h_line_data, data, f"{i}|tags_array")
                if tag == source: found_source = True
                if tag == message: found_message = True
                if tag.startswith('lichat_id_'): id = tag[10:]
            ## If this is ours, check.
            if found_source and found_message and id != None:
                ## If this is a new ID, append it to the stack.
                if seen_ids[-1] != id:
                    seen_ids.append(id)
            ## If we've now reached the first message of the ones we want, we're good to go.
            return len(seen_ids)+1 == line
        
        ## Last ID is now ID we want
        search_buffer(buffer.buffer, matcher, gather=False)
        if line < len(seen_ids):
            buffer.send(Edit, id=int(seen_ids[line]), text=text)
        else:
            buffer.show(text=f"Only found {len(seen_ids)-1} messages from you. Don't know how to access message {line-1}.", kind='error')

@lichat_command('react', '1 %(lichat_emote)', 'React to a previous message. Can use emotes or Unicode emoji.')
def react_command_cb(buffer, line=None, *text):
    if line == None:
        pass # TODO: interactive selection
    else:
        if line.isdigit():
            line = int(line)
            text = ' '.join(text)
        else:
            text = line+(' '+' '.join(text) if 0<len(text) else '')
            line = 1
        found = 0
        message = (None, None)
        def matcher(h_line, h_line_data, line):
            nonlocal found, message
            found_message = False
            id = None
            fr = None
            data = w.hdata_pointer(h_line, line, 'data')
            for i in range(w.hdata_integer(h_line_data, data, 'tags_count')):
                tag = w.hdata_string(h_line_data, data, f"{i}|tags_array")
                if tag == 'lichat_type_message': found_message = True
                if tag.startswith('lichat_id_'): id = tag[10:]
                if tag.startswith('lichat_from_'): fr = tag[12:]
            if found_message and id != None and fr != None:
                found += 1
                message = (id, fr)
            return found == line
        
        search_buffer(buffer.buffer, matcher, gather=False)
        (id, fr) = message
        if id != None:
            data = {'update-id': int(id), 'target': fr, 'emote': text}
            buffer.send(React, **data)
        else:
            buffer.show(text=f"Only found {found} messages. Don't know how to access message {line}.", kind='error')


@lichat_command('query', '%(nicks) %*', 'Join a private channel with a number of other users.')
def query_command_cb(buffer, *targets):
    @handle_failure(buffer)
    def callback(join):
        for target in targets:
            buffer.send(Pull, channel=join.channel, target=target)
    buffer.server.send_cb(callback, Create)

@lichat_command('me', '', 'Send a message in third-person.')
def me_command_cb(buffer, *text):
    buffer.send(Message, text=f"*{' '.join(text)}*")

@lichat_command('set-user-info', '%(lichat_user_key) %-', """Set user information for yourself.
The key must be a lichat symbol. By default the following symbols are recognised:
  :birthday
  :contact
  :location
  :public-key
  :real-name
  :status (away, or something else)
However, a server may support additional symbols.""")
def set_user_info_command_cb(buffer, key, *value):
    @handle_failure(buffer)
    def callback(update):
        buffer.show(text=f"Field {key} updated.")
    buffer.send_cb(callback, SetUserInfo, key=pylichat.wire.from_string(key), text=' '.join(value))

@lichat_command('away', 'on', """Set yourself as away (or present if no argument)""")
def away_command_cb(buffer, value=None):
    if value == None:
        buffer.send(SetUserInfo, key=kw('status'), text='')
    else:
        buffer.send(SetUserInfo, key=kw('status'), text='away')

@lichat_command('status', '', """Update your status""")
def status_command_cb(buffer, *text):
    buffer.send(SetUserInfo, key=kw('status'), text=' '.join(text))

@lichat_command('send-as', '%(nicks) %-', """Send a message as another user in the current channel. Requires the BRIDGE capability.""")
def send_as_command_cb(buffer, user, *text):
    buffer.send(Message, bridge=user, text=' '.join(text))

### Async
def read_file(data):
    data = json.loads(data)
    try:
        with open(data['url'], 'rb') as file:
            data['payload'] = str(base64.b64encode(file.read()), 'utf-8')
        (content_type, _) = mimetypes.guess_type(data['url'], False)
        data['content-type'] = content_type
        data['filename'] = Path(data['url']).stem

        buffer = find_buffer(data['server'], data['channel'])
        if buffer != None:
            buffer.send(Data, **data)
        data['payload'] = True
    except Exception as e:
        data['text'] = f"Internal error: {e}"
    return json.dumps(data)

def download_file(data):
    data = json.loads(data)
    try:
        r = urllib.request.urlopen(data['url'])
        data['payload'] = str(base64.b64encode(r.read()), 'utf-8')
        data['content-type'] = r.headers.get('content-type').split(';')[0]
        match = re.compile('filename="([^"]+)"').search(r.headers.get('content-disposition') or '')
        if match != None:
            data['filename'] = match.group(1)
        else:
            data['filename'] = data['url'].rsplit('/', 1)[1]

        buffer = find_buffer(data['server'], data['channel'])
        if buffer != None:
            buffer.send(Data, **data)
        data['payload'] = True
    except urllib.error.HTTPError as e:
        data['text'] = f"URL unreachable: {e}"
    except Exception as e:
        data['text'] = f"Internal error: {e}"
    return json.dumps(data)

def process_send(_data, _command, return_code, out, err):
    if return_code == w.WEECHAT_HOOK_PROCESS_ERROR or out == '':
        w.prnt("", "Failed to download file.")
    else:
        data = json.loads(out)
        buffer = find_buffer(data['server'], data['channel'])
        if buffer != None:
            if data.get('payload', None) == None:
                buffer.edit(make_instance(Failure, **data))
    return w.WEECHAT_RC_OK

def write_file(data):
    data = json.loads(data)
    try:
        with open(data['url'], 'wb') as file:
            file.write(base64.b64decode(data['payload']))
        data['payload'] = ''
        data['text'] = f"Sent file file://{data['url']}"
    except Exception as e:
        data['text'] = f"Internal error: {e}"
    return json.dumps(data)

def upload_file(data):
    data = json.loads(data)
    try:
        # FIXME: try to remove dependency on requests, because
        # requests -> simplejson -> decimal, which provokes the
        # MPD_SETMINALLOC issue.
        import requests
        headers = {'Authorization': f'Client-ID {imgur_client_id}'}
        post = {'type': 'file', 'title': data['filename']}
        files = {}
        if data['content-type'].startswith('image'):
            files['image'] = (data['filename'], base64.b64decode(data['payload']), data['content-type'])
        else:
            files['video'] = (data['filename'], base64.b64decode(data['payload']), data['content-type'])
        r = requests.post(url='https://api.imgur.com/3/image.json', data=post, files=files, headers=headers)
        response = json.loads(r.text)
        if response['success']:
            data['payload'] = response['data']['link']
            data['text'] = f"Sent file {response['data']['link']}"
        else:
            data['payload'] = ''
            data['text'] = f"Imgur failed: {response['data']['error']}"
    except Exception as e:
        data['text'] = f"Internal error: {e}"
    return json.dumps(data)

def process_upload(_data, _command, return_code, out, err):
    if return_code == w.WEECHAT_HOOK_PROCESS_ERROR or out == '':
        w.prnt("", "Failed to upload file.")
    else:
        try:
            data = json.loads(out)
            update = make_instance(Message, **data)
            buffer = find_buffer(data['server'], data['channel'])
            if buffer != None:
                buffer.edit(update)
        except Exception as e:
            w.prnt("", f"Failed to upload file: couldn't parse:\n{out}")
    return w.WEECHAT_RC_OK

### Completion
def channel_completion_cb(_data, item, w_buffer, completion):
    buffer = weechat_buffer_to_representation(w_buffer)
    if buffer == None: return w.WEECHAT_RC_OK

    for channel in buffer.server.buffers:
        w.hook_completion_list_add(completion, channel, 0, w.WEECHAT_LIST_POS_SORT)
    return w.WEECHAT_RC_OK

def server_completion_cb(_data, item, w_buffer, completion):
    for server in servers:
        w.hook_completion_list_add(completion, server, 0, w.WEECHAT_LIST_POS_SORT)
    return w.WEECHAT_RC_OK

def update_completion_cb(_data, item, w_buffer, completion):
    for name, obj in inspect.getmembers(pylichat.update):
        if hasattr(obj, '__symbol__'):
            w.hook_completion_list_add(completion, obj.__symbol__[1], 0, w.WEECHAT_LIST_POS_SORT)
    return w.WEECHAT_RC_OK

def channel_key_completion_cb(_data, item, w_buffer, completion):
    for k in [':topic',':rules',':news',':contact']:
        w.hook_completion_list_add(completion, k, 0, w.WEECHAT_LIST_POS_SORT)
    return w.WEECHAT_RC_OK

def emote_completion_cb(_data, item, w_buffer, completion):
    buffer = weechat_buffer_to_representation(w_buffer)
    if buffer == None: return w.WEECHAT_RC_OK
    
    for emote in buffer.server.client.emotes:
        w.hook_completion_list_add(completion, emote, 0, w.WEECHAT_LIST_POS_SORT)
    return w.WEECHAT_RC_OK

def last_emote(text, emotes):
    match = re.match(r'.*:([^:]+):$', text)
    if match != None and match.group(1).lower() in emotes:
        return match.group(1)

def input_complete_cb(_data, w_buffer, command):
    buffer = weechat_buffer_to_representation(w_buffer)
    if buffer == None: return w.WEECHAT_RC_OK

    ## Reinvent completion engine...
    text = w.buffer_get_string(w_buffer, 'input')
    index = int(w.buffer_get_string(w_buffer, 'localvar_lichat_complete_index'))
    prefix = w.buffer_get_string(w_buffer, 'localvar_lichat_complete_prefix')
    emotes = buffer.server.client.emotes.keys()
    try:
        ## If we aren't ending with a full emote, or the emote is
        ## from a different prefix than we're used to, reset.
        last = last_emote(text, emotes)
        if last == None or not prefix.startswith(text[:-(len(last)+1)]):
            index = 0
            prefix = text

        ## Now find all emotes that would match.
        last_colon = prefix.rindex(':')+1
        find = prefix[last_colon:].lower()
        matches = []
        for emote in emotes:
            if emote.startswith(find):
                matches.append(emote)

        ## If there are any matches, select the next one, and update our cache.
        if 0 < len(matches):
            matches.sort()
            match = matches[index]
            if command == 'input complete_next':
                index = (index+1) % len(matches)
            else:
                index = (index-1) % len(matches)
            w.buffer_set(w_buffer, 'localvar_set_lichat_complete_index', str(index))
            w.buffer_set(w_buffer, 'localvar_set_lichat_complete_prefix', prefix)
            w.buffer_set(w_buffer, 'input', f"{prefix[:last_colon]}{match}:")
    except:
        pass
    return w.WEECHAT_RC_OK

### Config
def evaluate_string(string):
    return w.string_eval_expression(string, {}, {}, {})

def cfg(section, option, type=str, default=None, evaluate=False):
    cfg = config[section].get(option, None)
    if cfg == None: return default
    if type == str:
        string = w.config_string(cfg)
        if evaluate: return evaluate_string(string)
        else: return string
    elif type == bool: return w.config_boolean(cfg)
    elif type == int: return w.config_integer(cfg)

def server_options(server):
    found = {}
    cfg = config['server']
    for name in cfg:
        if name.startswith(server+'.'):
            found[name[len(server)+1:]] = cfg[name]
    return found

def servers_options():
    found = {}
    cfg = config['server']
    for name in cfg:
        parts = name.split('.', 1)
        if len(parts) > 1:
            found.setdefault(parts[0], {})[parts[1]] = cfg[name]
    return found

def config_create_option_cb(section_name, file, section, option, value):
    config[section_name][option] = w.config_search_option(file, section, option)
    return w.WEECHAT_CONFIG_OPTION_SET_OK_SAME_VALUE

def config_delete_option_cb(section_name, file, section, option):
    cfg = config[section_name]
    for key in list(cfg):
        if cfg[key] == option:
            del cfg[key]
    return w.WEECHAT_CONFIG_OPTION_UNSET_OK_REMOVED

def config_updated(full=False):
    logger.debug(f"config_updated({full=})")
    global imgur_client_id, data_save_directory, data_save_types, logtraceback, logfilehandler
    data_save_directory = cfg('behaviour', 'data_save_directory')
    data_save_types = cfg('behaviour', 'data_save_types').split(',')
    imgur_client_id = cfg('behaviour', 'imgur_client_id')
    logweehandler.setLevel(cfg('behaviour', 'loglevel', str, 'WARNING'))
    logtraceback = cfg('behaviour', 'logtraceback', bool, False)
    if cfg('behaviour', 'logfile', bool, False):
        if logfilehandler is None:
            logfilehandler = logging.handlers.RotatingFileHandler(w.string_eval_path_home("%h/lichat.log", '', '', ''),
                                                                  maxBytes=4000000, backupCount=8,
                                                                  encoding='utf-8')
            logfilehandler.setFormatter(logging.Formatter("%(asctime)s\t%(name)s\t%(levelname)s\t%(message)s",
                                                          datefmt='%a, %d %b %Y %H:%M:%S %z'))
            logging.root.addHandler(logfilehandler)
    else:
        if logfilehandler is not None:
            logging.root.removeHandler(logfilehandler)
            logfilehandler = None

    if not full:
        return

    for server, sconf in servers_options().items():
        server = w.config_string(sconf['name']) or server
        if server not in servers:
            Server(name=server,
                   username=w.config_string(sconf['username']),
                   password=evaluate_string(w.config_string(sconf['password'])),
                   host=w.config_string(sconf['host']),
                   port=w.config_integer(sconf['port']),
                   ssl=w.config_boolean(sconf['ssl']))

def config_option_change_cb(option_name, option):
    logger.debug(f"config_option_change_cb({option_name}) -> {w.config_string(option) or w.config_integer(option)}")
    config_updated(full=False)
    if option_name.startswith('server.'):
        server = option_name.split('.', maxsplit=2)[1]
        servername = w.config_string(f"lichat.server.{server}.name") or server
        if servername in servers:
            w.prnt("", f"{w.prefix('error')}Note that changes to lichat server options currently require reloading the script")
    return w.WEECHAT_RC_OK

def config_section(file, section_name, options, read_cb=''):
    def value_type(value):
        if isinstance(value, bool): # NB: bool is a subclass of int
            return 'boolean'
        if isinstance(value, str):
            return 'string'
        if isinstance(value, int):
            return 'integer'

    section = None
    if section_name in config:
        section = config[section_name]['__section__']
    else:
        section = w.config_new_section(file, section_name, 1, 1,
                                       read_cb, '',
                                       '', '', # write
                                       '', '', # write_default
                                       'config_create_option_cb', section_name,
                                       'config_delete_option_cb', section_name)
        config[section_name] = {'__section__': section}
    
    for option in options:
        name = option['name']
        optype = option.get('optype', value_type(option['default']))
        description = option.get('description', f'({optype})')
        min = option.get('min', 0)
        max = option.get('max', 65535)
        default = str(option['default'])
        config[section_name][name] = w.config_new_option(file, section, name,
                                                         optype, description,
                                                         option.get('enum', ''), min, max,
                                                         default, default, 0,
                                                         '', '', # check_value
                                                         'config_option_change_cb', f"{section_name}.{name}",
                                                         '', '') # delete
    return section


def config_server_read_cb(data, file, section, name, value):
    """Called when an option is read from file in the lichat.server
    section -- even if that option hasn't been declared yet. Thus, we
    can dynamically declare options.
    """
    parts = name.split('.', maxsplit=1)
    option = w.config_search_option(file, section, name)
    if option == '' and len(parts) > 1:
        g = w.config_get(f"lichat.server.{parts[0]}.host")
        if g == '':
            config_section(config_file, 'server', [
                {'name': f'{parts[0]}.name', 'default': f'{parts[0]}'},
                {'name': f'{parts[0]}.host', 'default': ''},
                {'name': f'{parts[0]}.port', 'default': 1111, 'min': 1, 'max': 65535},
                {'name': f'{parts[0]}.username', 'default': w.config_string(w.config_get('irc.server_default.username'))},
                {'name': f'{parts[0]}.password', 'default': ''},
                {'name': f'{parts[0]}.ssl', 'default': False},
                {'name': f'{parts[0]}.autojoin', 'default': 'lichatters'},
                {'name': f'{parts[0]}.autoconnect', 'default': False},
                {'name': f'{parts[0]}.autoreconnect', 'default': True},
                {'name': f'{parts[0]}.autoreconnect_delay', 'min': 1, 'default': 60},
                {'name': f'{parts[0]}.highlight', 'default': 'username'}
            ])
            option = w.config_search_option(file, section, name)

    return w.config_option_set(option, value, 1)

def shutdown_cb():
    logger.info("Unloading script")
    for name, server in servers.items():
        if server.is_connected():
            logger.info(f"[{server.name}] Disconnecting")
            try:
                server.disconnect()
            except:
                logger.exception(f"[server.name] Error while disconnecting")

    return w.WEECHAT_RC_OK

### Setup
if __name__ == '__main__' and import_ok:
    if w.register(SCRIPT_NAME, SCRIPT_AUTHOR, SCRIPT_VERSION,
                        SCRIPT_LICENSE, SCRIPT_DESC, 'shutdown_cb', ''):
        logweehandler = WeechatHandler(level=logging.WARNING)
        logging.basicConfig(handlers=[logweehandler], force=True, level=logging.DEBUG)
        
        config_file = w.config_new('lichat', '', '')
        config_section(config_file, 'behaviour', [
            {'name': 'data_save_directory', 'default': w.info_get('weechat_dir', '')+'/lichat/downloads/',
             'description': f"Where to save uploaded files to."},
            {'name': 'data_save_types', 'default': 'all',
             'description': f"Which file types to save locally. Should be a comma-separated list of mime-types. Setting to 'all' will save files of any type."},
            {'name': 'imgur_client_id', 'default': '',
             'description': f"An imgur.com client ID token. If set, will upload compatible data files to imgur and replace with a link instead of saving the file locally."},
            {'name': 'highlight', 'default': '',
             'description': f"A comma-separated list of words to highlight in any Lichat buffer."},
            # can also be CRITICAL but that would hide too much...
            {'name': 'loglevel', 'default': 'WARNING', 'enum': 'ERROR|WARNING|INFO|DEBUG',
             'optype': 'integer',
             'description': f"weelichat log level"},
            {'name': 'logtraceback', 'default': False, 'description': "Include exception traceback in error messages"},
            {'name': 'logfile', 'default': False, 'description': 'also log all messages (DEBUG) to file (%h/lichat.log)'}
        ])
        config_section(config_file, 'server_default', [
            {'name': 'name', 'default': '',
             'description': f"The name of the server network."},
            {'name': 'host', 'default': '',
             'description': f"The default hostname to use."},
            {'name': 'port', 'default': 1111, 'min': 1, 'max': 65535,
             'description': f"The default port to use. The official Lichat default port is 1111."},
            {'name': 'username', 'default': w.config_string(w.config_get('irc.server_default.username')),
             'description': f"The username to connect with. Leaving this empty will make the server choose a name for you."},
            {'name': 'password', 'default': '',
             'description': f"The password to connect with, in case the username is registered."},
            {'name': 'ssl', 'default': False,
             'description': f"Whether to connect with SSL. The server must support this. The default port for SSL is 1112."},
            {'name': 'autojoin', 'default': '',
             'description': f"Which channels to join by default. Should be a double-space-separated list."},
            {'name': 'autoconnect', 'default': True,
             'description': f"Whether to automatically connect to this server when the lichat script is loaded."},
            {'name': 'autoreconnect', 'default': True,
             'description': f"Whether to automatically reconnect when the client disconnects for some reason."},
            {'name': 'autoreconnect_delay', 'min': 1, 'default': 60,
             'description': f"How long to wait between reconnection attempts, in seconds."},
            {'name': 'highlight', 'default': 'username',
             'description': f"A comma-separated list of words to highlight in any buffer for this server. The special word 'username' will be replaced with the username used for this server."}
        ])
        config_section(config_file, 'server', [
            {'name': 'tynet.name', 'default': 'TyNET'},
            {'name': 'tynet.host', 'default': 'chat.tymoon.eu'},
            {'name': 'tynet.port', 'default': 1111, 'min': 1, 'max': 65535},
            {'name': 'tynet.username', 'default': w.config_string(w.config_get('irc.server_default.username'))},
            {'name': 'tynet.password', 'default': ''},
            {'name': 'tynet.ssl', 'default': False},
            {'name': 'tynet.autojoin', 'default': 'lichatters'},
            {'name': 'tynet.autoconnect', 'default': False},
            {'name': 'tynet.autoreconnect', 'default': True},
            {'name': 'tynet.autoreconnect_delay', 'min': 1, 'default': 60},
            {'name': 'tynet.highlight', 'default': 'username'}
        ], read_cb='config_server_read_cb')
        w.config_reload(config_file)
        config_updated(full=True)
        
        w.hook_command('lichat', 'Prefix for lichat related commands',
                       '<command> [<command options>]',
                       'Commands:\n  '+'\n  '.join(commands.keys())+'\nUse /lichat help for more information.',
                       ' || '.join([c['name']+' '+c['completion'] for c in commands.values() if c['completion'] != '']),
                       'lichat_command_cb', '')
        w.hook_command_run('/ban', 'ban_command_cb', '')
        w.hook_command_run('/disconnect', 'disconnect_command_cb', '')
        w.hook_command_run('/invite', 'pull_command_cb', '')
        w.hook_command_run('/join', 'join_command_cb', '')
        w.hook_command_run('/kick', 'kick_command_cb', '')
        w.hook_command_run('/kickban', 'kickban_command_cb', '')
        w.hook_command_run('/kill', 'kill_command_cb', '')
        w.hook_command_run('/me', 'me_command_cb', '')
        w.hook_command_run('/msg', 'message_command_cb', '')
        w.hook_command_run('/part', 'leave_command_cb', '')
        w.hook_command_run('/query', 'query_command_cb', '')
        w.hook_command_run('/register', 'register_command_cb', '')
        w.hook_command_run('/topic', 'topic_command_cb', '')
        w.hook_command_run('/unban', 'unban_command_cb', '')
        w.hook_command_run('/users', 'users_command_cb', '')
        w.hook_command_run('/whois', 'user_info_command_cb', '')

        w.bar_item_new('input_prompt', '(extra)input_prompt_cb', '')

        w.hook_completion('lichat_channel', 'complete Lichat channel names', 'channel_completion_cb', '')
        w.hook_completion('lichat_server', 'complete Lichat server names', 'server_completion_cb', '')
        w.hook_completion('lichat_update', 'complete Lichat update types', 'update_completion_cb', '')
        w.hook_completion('lichat_channel_key', 'complete Lichat channel info keys', 'channel_key_completion_cb', '')
        w.hook_completion('lichat_emote', 'complete :emotes: for Lichat', 'emote_completion_cb', '')
        w.hook_command_run('/input complete_*', 'input_complete_cb', '')
        
        logger.info("Loaded script")

        for server, sconf in servers_options().items():
            server = w.config_string(sconf['name']) or server
            instance = servers[server]
            if w.config_boolean(sconf['autoconnect']) and not instance.is_connected():
                try_connect('', instance)

## TODO: buffer sending to avoid getting throttled by the server.
## TODO: lag estimation
## TODO: config seems to get overridden / not properly saved sometimes?
