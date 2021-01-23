# -*- coding: utf-8 -*-
# boilerplate based on cmd_help.py
#
SCRIPT_NAME = 'lichat'
SCRIPT_AUTHOR = 'Georgiy Tugai <georgiy@crossings.link>'
SCRIPT_VERSION = '0.1'
SCRIPT_LICENSE = ''
SCRIPT_DESC = 'Client for Lichat protocol (https://shirakumo.github.io/lichat)'

import_ok = True

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
    import requests
    import socket
    import base64
    import re
    import mimetypes
    from pylichat import Client
    from pylichat.update import *
    from pylichat.symbol import kw, li
    import pylichat.wire
except ImportError as message:
    print('Missing package(s) for %s: %s' % (SCRIPT_NAME, message))
    import_ok = False

imgur_client_id = ''
imgur_formats = ['video/mp4', 'video/webm', 'video/x-matroska', 'video/quicktime',
                 'video/x-flv', 'video/x-msvideo', 'video/x-ms-wmv', 'video/mpeg',
                 'image/png', 'image/jpeg', 'image/gif', 'image/tiff', 'image/vnd.mozilla.apng']
config_file = None
config = {}
commands = {}
servers = {}

def register_command(name, func, description='', cmdtype='lichat'):
    commands[name] = {'name': name, 'func': func, 'description': description, 'cmdtype': cmdtype}

def call_command(buffer, name, *args):
    return commands[name]['func']('', buffer.buffer, shlex.join(args))

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
    buffer.send(Leave)
    buffer.delete()
    return w.WEECHAT_RC_OK

def lichat_socket_cb(name, fd):
    server = servers[name]
    for update in server.client.recv():
        server.client.handle(update)
    return w.WEECHAT_RC_OK

def input_prompt_cb(data, item, current_window, w_buffer, extra_info):
    buffer = weechat_buffer_to_representation(w_buffer)
    if buffer == None:
        return ''
    
    return f"{w.color(w.config_color(w.config_get('irc.color.input_nick')))}{buffer.server.client.username}"

def edit_buffer(w_buffer, matcher, new_text):
    h_line = w.hdata_get('line')
    h_line_data = w.hdata_get('line_data')
    lines = w.hdata_pointer(w.hdata_get('buffer'), w_buffer, 'own_lines')
    line = w.hdata_pointer(w.hdata_get('lines'), lines, 'last_line')

    while line and not matcher(h_line, h_line_data, line):
        line = w.hdata_move(h_line, line, -1)

    line_ptrs = []
    while line and matcher(h_line, h_line_data, line):
        line_ptrs.append(line)
        line = w.hdata_move(h_line, line, -1)
    line_ptrs.reverse()

    if not line_ptrs: return False

    line_text = new_text.split('\n', len(line_ptrs)-1)
    line_text = [line.replace('\n', ' | ') for line in line_text]
    line_text += [''] * (len(line_ptrs) - len(line_text))

    for line, text in zip(line_ptrs, line_text):
        data = w.hdata_pointer(h_line, line, 'data')
        w.hdata_update(h_line_data, data, {'message': text})
    return True

class Buffer:
    name = None
    server = None
    buffer = None
    channel = None

    def __init__(self, server, channel, name=None):
        if name == None: name = channel
        self.name = name
        self.server = server
        self.channel = channel
        self.buffer = w.buffer_new(self.w_name(),
                                   'lichat_buffer_input_cb', '',
                                   'lichat_buffer_close_cb', '')
        w.buffer_set(self.buffer, 'localvar_set_lichat_server', server.name)
        w.buffer_set(self.buffer, 'localvar_set_lichat_channel', channel)
        server.buffers[channel] = self

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
            self.show(update, text=message, kind='action')
        self.send_cb(callback, type, **args)

    def send_cb(self, cb, type, **args):
        if issubclass(type, ChannelUpdate):
            self.complete_channel(args)
        return self.server.send_cb(cb, type, **args)

    def display(self):
        w.command(self.buffer, f"/buffer {self.w_name()}")

    def show(self, update=None, text=None, kind='text', tags=[]):
        time = 0
        if update == None:
            update = {'from': self.server.client.servername}
        else:
            time = update.unix_clock()
            if update.get('id', None) != None:
                tags.append(f"lichat_id_{str(update['id'])}")
            if update.get('from', None) != None:
                tags.append(f"lichat_from_{update['from']}")
        if text == None:
            if isinstance(update, Join):
                kind = 'join'
                text = f"joined {update.channel}"
            elif isinstance(update, Leave):
                kind = 'quit'
                text = f"left {update.channel}"
            elif isinstance(update, Kick):
                kind = 'quit'
                text = f"has kicked {update.target} from {update.channel}"
            else:
                text = update.get('text', f"Update of type {type(update).__name__}")
        tags = ','.join(tags)
        if kind == 'text':
            w.prnt_date_tags(self.buffer, time, tags, f"{update['from']}\t{text}")
        else:
            w.prnt_date_tags(self.buffer, time, tags, f"{w.prefix(kind)}{update['from']}: {text}")

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
        
        edit_buffer(self.buffer, matcher, text)

class Server:
    name = None
    client = None
    buffers = {}
    hook = None

    def __init__(self, name=None, username=None, password=None, host='chat.tymoon.eu', port=1111, ssl=False):
        client = Client(username, password)
        self.name = name
        self.client = client
        self.host = host
        self.port = port
        self.ssl = ssl
        
        emote_dir = w.info_get('weechat_dir', '')+'/lichat/emotes/'+self.host+'/'
        w.mkdir_parents(emote_dir, 0o755)
        client.reload_emotes(emote_dir)

        def on_connect(client, update):
            for channel in self.config('channels').split('  '):
                self.send(Join, channel=channel)
        
        def on_misc(client, update):
            if isinstance(update, Failure):
                self.show(update, kind='error')

        def display(client, update):
            self.show(update)

        def on_pause(client, update):
            if update.by == 0:
                self.show(update, text=f"has disabled pause mode in {u.channel}", kind='action')
            else:
                self.show(update, text=f"has enabled pause mode by {u.by} in {u.channel}", kind='action')

        def on_emote(client, update):
            self.client.emotes[update.name].offload(emote_dir)

        def on_data(client, update):
            if imgur_client_id != '' and update['from'] != self.client.username and update['content-type'] in imgur_formats:
                data = update.__dict__
                data['server'] = name
                w.hook_process('func:upload_file', 0, 'process_upload', json.dumps(data))
                self.show(update, text=f"Sent file {update['filename']} (Uploading...)", kind='action')
            else:
                self.show(update, text=f"Sent file {update['filename']} ({update['content-type']})", kind='action')

        def on_channel_info(client, update):
            (_, name) = update.key
            self.show(update, text=f"{name}: {text}", kind='action')

        client.add_handler(Connect, on_connect)
        client.add_handler(Update, on_misc)
        client.add_handler(Message, display)
        client.add_handler(Join, display)
        client.add_handler(Leave, display)
        client.add_handler(Kick, display)
        client.add_handler(Pause, on_pause)
        client.add_handler(Emote, on_emote)
        client.add_handler(Data, on_data)
        client.add_handler(SetChannelInfo, on_channel_info)
        servers[name] = self

    def config(self, key, type=str):
        return cfg('server', self.name+'.'+key, type)

    def is_supported(self, extension):
        return self.client.is_supported(extension)

    def is_connected(self):
        return self.hook != None

    def connect(self):
        if self.hook == None:
            self.client.connect(self.host, self.port, ssl=self.ssl)
            self.hook = w.hook_fd(self.client.socket.fileno(), 1, 0, 0, 'lichat_socket_cb', self.name)

    def disconnect(self):
        if self.hook != None:
            self.client.disconnect()
            w.unhook(self.hook)
            self.hook = None

    def delete(self):
        self.client.disconnect()
        self.buffers.clear()
        del servers[name]

    def send(self, type, **args):
        return self.client.send(type, **args)

    def send_cb(self, cb, type, **args):
        return self.client.send_callback(cb, type, **args)

    def show(self, update, text=None, kind='text', buffer=None):
        if buffer == None and isinstance(update, UpdateFailure):
            origin = self.client.origin(update)
            if origin != None and not isinstance(origin, Leave):
                buffer = origin.get('channel', None)
        if buffer == None:
            buffer = update.get('channel', None)
        if buffer == None:
            buffer = self.client.servername
        if isinstance(buffer, str):
            name = buffer
            buffer = self.buffers.get(name, None)
            if buffer == None:
                buffer = Buffer(self, name)
        buffer.show(update, text=text, kind=kind)

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

def raw_command(name, description=''):
    def nested(f):
        @wraps(f)
        def wrapper(_data, w_buffer, args_str):
            args = shlex.split(args_str)
            args.pop(0)
            if check_signature(f, [w_buffer, *args], command=name):
                f(w_buffer, *args)
            else:
                return w.WEECHAT_RC_ERROR
            return w.WEECHAT_RC_OK
        register_command(name, wrapper, description, cmdtype='raw')
        return wrapper
    return nested

def lichat_command(name, description=''):
    def nested(f):
        @wraps(f)
        def wrapper(_data, w_buffer, args_str):
            buffer = weechat_buffer_to_representation(w_buffer)
            if buffer is None:
                return w.WEECHAT_RC_OK
            args = shlex.split(args_str)
            args.pop(0)
            if check_signature(f, [buffer, *args], command=name):
                f(buffer, *args)
            return w.WEECHAT_RC_OK_EAT
        register_command(name, wrapper, description, cmdtype='lichat')
        return wrapper
    return nested

@raw_command('connect', 'Connect to a lichat server. If no server name is passed, all servers are connected. If a hostname is passed, a new server connection is created.')
def connect_command_cb(w_buffer, name=None, host=None, port=None, username=None, password=None, ssl=None):
    if name == None:
        for server in servers:
            if not servers[server].is_connected():
                servers[server].connect()
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
        Server(name=name, username=username, password=password, host=host, port=port, ssl=ssl).connect()
        config_section(config_file, 'server', [
            {'name': 'host', 'default': host},
            {'name': 'port', 'default': port, 'min': 1, 'max': 65535},
            {'name': 'username', 'default': username},
            {'name': 'password', 'default': password},
            {'name': 'channels', 'default': ''},
            {'name': 'connect', 'default': True},
            {'name': 'ssl', 'default': ssl}
        ])
    elif name not in servers:
        w.prnt(w_buffer, f"f{w.prefix('error')} No such server {name}")
    else:
        servers[name].connect()


@lichat_command('disconnect', 'Disconnect from a lichat server. If no name is given, the server of the current channel is disconnected.')
def disconnect_command_cb(buffer, server=None):
    server = buffer.server
    if server != None:
        server = servers[server]
    server.disconnect()

@raw_command('help', 'Display help information about lichat commands.')
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

@lichat_command('join', 'Join an existing channel.')
def join_command_cb(buffer, channel=None):
    def join_cb(_client, _join, update):
        if update.channel not in buffer.server.buffers:
            Buffer(buffer.server, update.channel)
        buffer.server.buffers[update.channel].display()
    buffer.send_cb(join_cb, Join, channel=channel)

@lichat_command('leave', 'Leave a channel you\'re in. Defaults to the current channel.')
def leave_command_cb(buffer, channel=None):
    buffer.send(Leave, channel=channel)

@lichat_command('create', 'Create a new channel. If no name is given, an anonymous channel is created.')
def create_command_cb(buffer, channel=''):
    buffer.send(Create, channel=channel)

@lichat_command('pull', 'Pull another user into a channel. If no channel name is given, defaults to the current channel.')
def pull_command_cb(buffer, user, channel=None):
    buffer.send(Pull, channel=channel, target=user)

@lichat_command('kick', 'Kicks another user from a channel. If no channel name is given, defaults to the current channel.')
def kick_command_cb(buffer, user, channel=None):
    buffer.send(Kick, channel=channel, target=user)

@lichat_command('kickban', 'Kicks another user from a channel and removes their join permission. If no channel name is given, defaults to the current channel.')
def kickban_command_cb(buffer, user, channel=None):
    call_command(buffer, 'deny', 'join', user, channel)
    call_command(buffer, 'kick', user, channel)

@lichat_command('register', 'Register your account with a password. If successful, will save the password to config.')
def register_command_cb(buffer, password):
    def reg_cb(_client, _prev, update):
        if isinstance(update, Register):
            w.config_option_set(config['server'][buffer.server.name+'.password'], password, 0)
        buffer.show(update, text="Profile registered. Password has been saved.")
    buffer.send_cb(reg_cb, Register, password=password)

@lichat_command('set-channel-info', """Set channel information. If no channel name is given, defaults to the current channel.
The key must be a lichat symbol. By default the following symbols are recognised:
  :news
  :topic
  :rules
  :contact
However, a server may support additional symbols.""")
def set_channel_info_command_cb(buffer, key, value, channel=None):
    buffer.send(SetChannelInfo, channel=channel, key=wire.from_string(key), text=value)

@lichat_command('channel-info', 'Retrieve channel information. If no channel name is given, defaults to the current channel. If no key is given, all channel info is requested.')
def channel_info_command_cb(buffer, key=True, channel=None):
    if key != True:
        key = wire.from_string(key)
    buffer.send(ChannelInfo, channel=channel, key=key)

@lichat_command('topic', 'View or set the topic of the current channel.')
def topic_command_cb(buffer, value=None):
    if value == None:
        buffer.show(text=buffer.info(kw('topic')))
    else:
        buffer.send(SetChannelInfo, key=kw('topic'), text=value)

@lichat_command('pause', 'Set the pause mode of the channel. If no channel name is given, defaults to the current channel. If no pause time is given, pause-mode is ended.')
def pause_command_cb(buffer, pause="0", channel=None):
    buffer.send(Pause, channel=channel, by=int(pause))

@lichat_command('quiet', 'Quiets the given user. If no channel name is given, defaults to the current channel.')
def quiet_command_cb(buffer, target, channel=None):
    buffer.send_confirm(f"The user {target} has been quieted. Their messages will no longer be visible.",
                        Quiet, channel=channel, target=target)

@lichat_command('unquiet', 'Unquiets the given user. If no channel name is given, defaults to the current channel.')
def unquiet_command_cb(buffer, target, channel=None):
    buffer.send_confirm(f"The user {target} has been allowed messaging again.",
                        Unquiet, channel=channel, target=target)

@lichat_command('ban', 'Bans the given user from the server by username.')
def ban_command_cb(buffer, target):
    buffer.send_confirm(f"The user {target} has been banned.",
                        Ban, target=target)

@lichat_command('unban', 'Unbans the given username from the server.')
def unban_command_cb(buffer, target):
    buffer.send_confirm(f"The user {target} has been unbanned.",
                        Unban, target=target)

@lichat_command('ip-ban', 'Bans the given IP address from the server. Set bits in the given mask will be ignored when comparing IPs.')
def ip_ban_command_cb(buffer, ip, mask='::'):
    buffer.send_confirm(f"The ip {ip} under {mask} has been banned.",
                        IpBan, ip=ip, mask=mask)

@lichat_command('ip-unban', 'Unbans the given IP address from the server. Set bits in the given mask will be ignored when comparing IPs.')
def ip_unban_command_cb(buffer, ip, mask='::'):
    buffer.send_confirm(f"The ip {ip} under {mask} has been unbanned.",
                        IpUnban, ip=ip, mask=mask)

@lichat_command('message', 'Send a message to the given channel.')
def message_command_cb(buffer, channel, *args):
    buffer.send(Message, channel=channel, message=' '.join(args))

@lichat_command('users', 'List the users of the given channel. If no channel name is given, defaults to the current channel.')
def users_command_cb(buffer, channel=None):
    def callback(_client, _prev, users):
        buffer.show(text=f"Currently in channel: {' '.join(users.users)}")
    buffer.send_cb(callback, Users, channel=channel)

@lichat_command('channels', 'List the channels of the current server. If the server supports the channel-trees extension, only channels below the specified channel are retuurned. If no channel is specified, all top-level channels are returned.')
def channels_command_cb(buffer, channel=''):
    def callback(_client, _prev, channels):
        buffer.show(text=f"Channels: {' '.join(channels.channels)}")
    buffer.send_cb(callback, Channels, channel=channel)

@lichat_command('user-info', 'Request information on the given user.')
def user_info_command_cb(buffer, target):
    def callback(_client, _prev, info):
        registered = 'registered'
        if not info.registered:
            registered = 'not registered'
        buffer.show(text=f"Info on {target}: {target.connections} connections, {registered}")
    buffer.send_cb(callback, UserInfo, target=target)

@lichat_command('grant', 'Grant permission for an update to a user. If no user is given, the permission is granted to everyone. If no channel name is given, defaults to the current channel.')
def grant_command_cb(buffer, update, target=None, channel=None):
    type = li(update)
    if target == None:
        buffer.send_confirm(f"All users have been allowed {update}ing",
                            Permissions, channel=channel, permissions=[[type, True]])
    else:
        buffer.send_confirm(f"{target} has been allowed {update}ing",
                            Grant, channel=channel, target=target, update=type)

@lichat_command('deny', 'Deny permission for an update from a user. If no user is given, the permission is denied to everyone but you. If no channel name is given, defaults to the current channel.')
def deny_command_cb(buffer, update, target=None, channel=None):
    type = li(update)
    if target == None:
        buffer.send_confirm(f"All users have been denied from {update}ing",
                            Permissions, channel=channel, permissions=[[type, [li('+'), buffer.server.client.username]]])
    else:
        buffer.send_confrm(f"{target} has been denied from {update}ing",
                           Deny, channel=channel, target=target, update=type)

@lichat_command('send', 'Send a local file or file from an URL as a data upload. If no channel name is given, defaults to the current channel.')
def send_command_cb(buffer, file, channel=None):
    update = buffer.make_instance(Data, channel=channel)
    data = update.__dict__
    data['server'] = buffer.server.name
    data['url'] = file
    if re.match('^(\\w:|/|~)', file):
        w.hook_process('func:read_file', 0, 'process_send', json.dumps(data))
    else:
        w.hook_process('func:download_file', 0, 'process_send', json.dumps(data))
    buffer.show(update, text=f"Sending file...", kind='action')

## TODO: capabilities and server-info
## TODO: save files to disk
## TODO: making edits
## TODO: nicklist
## TODO: autocompletion
## TODO: properly handle disconnections initiated by the server.

def read_file(data):
    data = json.loads(data)
    try:
        with open(data['url'], 'rb') as file:
            data['payload'] = str(base64.b64encode(file.read()))
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
        r = requests.get(data['url'], allow_redirects=True)
        if r.status_code == 200:
            data['payload'] = str(base64.b64encode(r.content))
            data['content-type'] = r.headers.get('content-type')
            match = re.compile('filename="([^"]+)"').search(r.headers.get('content-disposition') or '')
            if match != None:
                data['filename'] = match.group(1)
            else:
                data['filename'] = data['url'].rsplit('/', 1)[1]
            
            buffer = find_buffer(data['server'], data['channel'])
            if buffer != None:
                buffer.send(Data, **data)
            data['payload'] = True
        else:
            data['text'] = f"URL unreachable: error {r.status_code}"
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

def upload_file(data):
    data = json.loads(data)
    try:
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
        data = json.loads(out)
        update = make_instance(Message, **data)
        buffer = find_buffer(data['server'], data['channel'])
        if buffer != None:
            buffer.edit(update)
    return w.WEECHAT_RC_OK

def lichat_cb(data, w_buffer, args_str):
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

    return command['func'](data, w_buffer, shlex.join(args))

def cfg(section, option, type=str):
    cfg = config[section][option]
    if type == str: return w.config_string(cfg)
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

def config_reload_cb(_data, file):
    w.config_reload(file)
    global imgur_client_id
    imgur_client_id = cfg('behaviour', 'imgur_client_id')
    for server, sconf in servers_options().items():
        if server not in servers:
            Server(name=server,
                   username=w.config_string(sconf['username']),
                   password=w.config_string(sconf['password']),
                   host=w.config_string(sconf['host']),
                   port=w.config_integer(sconf['port']),
                   ssl=w.config_boolean(sconf['ssl']))
        instance = servers[server]
        if w.config_boolean(sconf['connect']) and not instance.is_connected():
            instance.connect()
    return w.WEECHAT_RC_OK

def config_section(file, section_name, options):
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
        section = w.config_new_section(file, section_name, 1, 1, '', '', '', '', '', '', 'config_create_option_cb', section_name, 'config_delete_option_cb', section_name)
        config[section_name] = {'__section__': section}
    
    for option in options:
        name = option['name']
        optype = option.get('optype', value_type(option['default']))
        description = option.get('description', f'({optype})')
        min = option.get('min', 0)
        max = option.get('max', 0)
        default = str(option['default'])
        config[section_name][name] = w.config_new_option(file, section, name,
                                                         optype, description,
                                                         '', min, max,
                                                         default, default, 0,
                                                         '', '', '', '', '', '')
    return section

if __name__ == '__main__' and import_ok:
    if w.register(SCRIPT_NAME, SCRIPT_AUTHOR, SCRIPT_VERSION,
                        SCRIPT_LICENSE, SCRIPT_DESC, '', ''):
        
        config_file = w.config_new('lichat', 'config_reload_cb', '')
        config_section(config_file, 'behaviour', [
            {'name': 'imgur_client_id', 'default': ''}
        ])
        config_section(config_file, 'server_default', [
            {'name': 'host', 'default': ''},
            {'name': 'port', 'default': 1111, 'min': 1, 'max': 65535},
            {'name': 'username', 'default': w.config_string(w.config_get('irc.server_default.username'))},
            {'name': 'password', 'default': ''},
            {'name': 'channels', 'default': ''},
            {'name': 'connect', 'default': True},
            {'name': 'ssl', 'default': False}
        ])
        config_section(config_file, 'server', [
            {'name': 'tynet.host', 'default': 'chat.tymoon.eu'},
            {'name': 'tynet.port', 'default': 1111, 'min': 1, 'max': 65535},
            {'name': 'tynet.username', 'default': w.config_string(w.config_get('irc.server_default.username'))},
            {'name': 'tynet.password', 'default': ''},
            {'name': 'tynet.channels', 'default': 'lichatters'},
            {'name': 'tynet.connect', 'default': False},
            {'name': 'tynet.ssl', 'default': False}
        ])
        config_reload_cb('', config_file)
        
        w.hook_command('lichat',                   # command
                       'lichat description',       # description
                       'args',                     # args
                       'args_description',         # args_description
                       '',                         # completion
                       'lichat_cb', '')
        w.hook_command_run('/disconnect', 'disconnect_command_cb', '')
        w.hook_command_run('/join', 'join_command_cb', '')
        w.hook_command_run('/part', 'leave_command_cb', '')
        w.hook_command_run('/invite', 'pull_command_cb', '')
        w.hook_command_run('/kick', 'kick_command_cb', '')
        w.hook_command_run('/kickban', 'kickban_command_cb', '')
        w.hook_command_run('/register', 'register_command_cb', '')
        w.hook_command_run('/topic', 'topic_command_cb', '')

        w.bar_item_new('input_prompt', '(extra)input_prompt_cb', '')
        
        w.prnt("", "lichat.py\tis loaded ok")
