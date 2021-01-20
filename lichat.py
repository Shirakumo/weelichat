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
    import shlex
    import json
    import requests
    import socket
    import base64
    from pylichat import Client
    from pylichat.update import *
    from pylichat.symbol import kw
    import pylichat.wire
except ImportError as message:
    print('Missing package(s) for %s: %s' % (SCRIPT_NAME, message))
    import_ok = False

imgur_client_id = None
imgur_formats = ['video/mp4' 'video/webm' 'video/x-matroska' 'video/quicktime'
                 'video/x-flv' 'video/x-msvideo' 'video/x-ms-wmv' 'video/mpeg'
                 'image/png' 'image/jpeg' 'image/gif' 'image/tiff' 'image/vnd.mozilla.apng']
config_file = None
config = {}
commands = {}
servers = {}

def register_command(name, func, description='', cmdtype='lichat'):
    commands[name] = {'name': name, 'func': func, 'description': description, 'cmdtype': cmdtype}

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

class Buffer:
    name = None
    server = None
    buffer = None
    channel = None

    def __init__(self, server, channel, name=None):
        if name == None: name = channel
        self.server = server
        self.channel = channel
        self.buffer = w.buffer_new(f"lichat.{server.name}.{name}",
                                   'lichat_buffer_input_cb', '',
                                   'lichat_buffer_close_cb', '')
        w.buffer_set(self.buffer, 'localvar_set_lichat_server', server.name)
        w.buffer_set(self.buffer, 'localvar_set_lichat_channel', channel)
        server.buffers[channel] = self

    def info(self, key):
        return self.server.client.channels[self.channel][key]

    def delete(self):
        del self.server.buffers[self.channel]

    def send(self, type, **args):
        if issubclass(type, ChannelUpdate) and 'channel' not in args:
            args['channel'] = self.channel
        return self.server.send(type, **args)

    def send_cb(self, cb, type, **args):
        if issubclass(type, ChannelUpdate) and 'channel' not in args:
            args['channel'] = self.channel
        return self.server.send_cb(cb, type, **args)

    def show(self, update=None, text=None, kind='text'):
        if update == None:
            update = {'from': self.server.client.servername}
        if text == None:
            text = update.get('text', f"Update of type {type(update).__name__}")
        if kind == 'text':
            w.prnt(self.buffer, f"{update['from']}\t{text}")
        else:
            w.prnt(self.buffer, f"{w.prefix(kind)}{update['from']}: {text}")

    def edit(self, update, text=None):
        ## FIXME: Do edit magic based on update id here
        self.show(update, text)

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

        def on_data(client, update):
            if imgur_client_id != None and u['content-type'] in imgur_formats:
                data = u.__dict__
                data['server'] = name
                w.hook_process('func:upload_file', 0, 'process_upload', json.dumps(data))
                self.show(update, text=f"Sent file {u['filename']} (Uploading...)", kind='action')
            else:
                self.show(update, text=f"Sent file {u['filename']}", kind='action')

        def on_channel_info(client, update):
            (_, name) = update.key
            self.show(update, text=f"{name}: {text}", kind='action')
        
        client.add_handler(Update, on_misc)
        client.add_handler(Message, display)
        client.add_handler(Join, display)
        client.add_handler(Leave, display)
        client.add_handler(Pause, on_pause)
        client.add_handler(Data, on_data)
        client.add_handler(SetChannelInfo, on_channel_info)
        servers[name] = self

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
        def wrapper(data, w_buffer, args_str):
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
        def wrapper(data, w_buffer, args_str):
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
def connect_command_cb(w_buffer, name=None, hostname=None, port=None, username=None, password=None, ssl=None):
    if name == None:
        for server in servers:
            if not server.is_connected():
                servers[server].connect()
    elif hostname != None:
        if name in servers:
            w.prnt(w_buffer, f"f{w.prefix('error')} A server of that name already exists.")
            return
        if port == None: port = cfg('server_default', 'port', int)
        if username == None: username = cfg('server_default', 'username')
        if password == None: password = cfg('server_default', 'password')
        if ssl == None: ssl = cfg('server_default', 'ssl', bool)
        if ssl == 'on': ssl = True
        if ssl == 'off': ssl = False
        Server(name=name, username=username, password=password, host=host, port=port, ssl=ssl).connect()
        config_section(config_file, 'server', [
            {'name': 'host', 'default': hostname},
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

@raw_command('help', 'Display help information about lichat commands.')
def help_command_cb(w_buffer, topic=None):
    if topic == None:
        for name in commands:
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
def join_command_cb(buffer, channel):
    buffer.send(Join, channel=channel)

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

@lichat_command('register', 'Register your account with a password. If successful, will save the password to config.')
def register_command_cb(buffer, password):
    def reg_cb(_client, _prev, update):
        if isinstance(update, Register):
            w.config_option_set(config['server'][buffer.server.name+'.password'], password, 0)
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
    buffer.send(Quiet, channel=channel, target=target)

@lichat_command('unquiet', 'Unquiets the given user. If no channel name is given, defaults to the current channel.')
def unquiet_command_cb(buffer, target, channel=None):
    buffer.send(Unquiet, channel=channel, target=target)

@lichat_command('message', 'Send a message to the given channel.')
def message_command_cb(buffer, channel, *args):
    buffer.send(Message, channel=channel, message=' '.join(args))

@lichat_command('users', 'List the users of the given channel. If no channel name is given, defaults to the current channel.')
def users_command_cb(buffer, channel=None):
    def callback(_client, _prev, users):
        buffer.show(text=f"Currently in channel: {' '.join(users.users)}")
    buffer.send_cb(callback, Users, channel=channel)

@lichat_command('channels', 'List the channels of the current server.')
def channels_command_cb(buffer):
    def callback(_client, _prev, channels):
        buffer.show(text=f"Channels: {' '.join(channels.channels)}")
    buffer.send_cb(callback, Channels)

@lichat_command('user-info', 'Request information on the given user.')
def user_info_command_cb(buffer, target):
    def callback(_client, _prev, info):
        registered = 'registered'
        if not info.registered:
            registered = 'not registered'
        buffer.show(text=f"Info on {target}: {target.connections} connections, {registered}")
    buffer.send_cb(callback, UserInfo, target=target)

## TODO: handle channel-tree channel name prepend if start with slash, allow specifying channel in channels
## TODO: permissions, edit, data

def upload_file(data):
    data = json.loads(data)
    try:
        headers = {'Authorization': f'Client-ID {imgur_client_id}'}
        post = {'type': 'file', 'title': data['filename']}
        files = {}
        if data['content_type'].startswith('image'):
            files['image'] = (data['filename'], base64.b64decode(data['payload']), data['content-type'])
        else:
            files['video'] = (data['filename'], base64.b64decode(data['payload']), data['content-type'])
        r = requests.post(url='https://api.imgur.com/3/image.json', data=post, files=files, headers=headers)
        response = json.loads(r.text)
        if response['success']:
            data['payload'] = response['data']['link']
            return json.dumps(data)
        else:
            return ''
    except Exception:
        return ''

def process_upload(data, command, return_code, out, err):
    if return_code == w.WEECHAT_HOOK_PROCESS_ERROR or out == '':
        w.prnt("", "Failed to upload file.")
    else:
        data = json.loads(out)
        buffer = find_buffer(data['server'], data['channel'])
        if buffer != None:
            buffer.edit(data, text=f"Sent file {u['payload']}")
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
        if name.starts_with(server+'.'):
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
            for channel in w.config_string(sconf['channels']).split('  '):
                instance.send(Join, channel=channel)
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
        extensions.remove('shirakumo-emotes')

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
        w.hook_command_run('/join', 'join_command_cb', '')
        w.hook_command_run('/part', 'leave_command_cb', '')
        w.hook_command_run('/invite', 'pull_command_cb', '')
        w.hook_command_run('/kick', 'kick_command_cb', '')
        w.hook_command_run('/register', 'register_command_cb', '')
        w.hook_command_run('/topic', 'topic_command_cb', '')

        w.bar_item_new('input_prompt', '(extra)input_prompt_cb', '')
        
        w.prnt("", "lichat.py\tis loaded ok")
