# -*- coding: utf-8 -*-
# boilerplate based on cmd_help.py
#
SCRIPT_NAME = 'lichat'
SCRIPT_AUTHOR = 'Georgiy Tugai <georgiy@crossings.link>'
SCRIPT_VERSION = '0.1'
SCRIPT_LICENSE = ''
SCRIPT_DESC = 'Client for Lichat protocol (https://shirakumo.github.io/lichat-protocol/)'

import_ok = True

try:
    import weechat as w
except ImportError:
    print('This script must be run under WeeChat.')
    print('Get WeeChat now at: http://www.weechat.org/')
    import_ok = False

try:
    from functools import wraps
    import shlex
    import socket
    from pylichat import Client
    from pylichat.update import *
except ImportError as message:
    print('Missing package(s) for %s: %s' % (SCRIPT_NAME, message))
    import_ok = False

my_buffer = None
my_client = None
my_buffers = {}
redir_id_to_buffer = {}

def lichat_buffer_or_ignore(f):
    """
    Only run this function if we're in a lichat buffer, else ignore

    Inspired by slack.py
    """
    @wraps(f)
    def wrapper(data, current_buffer, *args, **kwargs):
        if not (current_buffer == my_buffer or current_buffer in my_buffers.values()):
            return w.WEECHAT_RC_OK
        return f(data, current_buffer, *args, **kwargs)
    return wrapper

def lichat_buffer_input_cb(data, buffer, input_data):
    if buffer == my_buffer:
        w.prnt(buffer, f"{w.prefix('error')}This buffer is not a channel!")
        return w.WEECHAT_RC_OK
    redir_id_to_buffer[my_client.send(Message, channel=data, text=input_data)] = data
    return w.WEECHAT_RC_OK

@lichat_buffer_or_ignore
def join_command_cb(data, current_buffer, args_str):
    args = shlex.split(args_str)
    w.prnt("", f"{args}")
    if len(args) == 2:
        my_client.send(Join, channel=args[1])
        return w.WEECHAT_RC_OK_EAT
    return w.WEECHAT_RC_OK

@lichat_buffer_or_ignore
def part_command_cb(data, current_buffer, args_str):
    args = shlex.split(args_str)
    w.prnt("", f"{args}")
    if len(args) == 2:
        my_client.send(Leave, channel=args[1])
        return w.WEECHAT_RC_OK_EAT
    return w.WEECHAT_RC_OK

def handle_input(client, line):
    pass
        # parts = line.split(' ', 1)
        # command = parts[0]
        # argument = parts[1]
        # if command == '/join':
        #     client.send(Join, channel=argument)
        # elif command == '/leave':
        #     if argument == '': argument = client.channel
        #     client.send(Leave, channel=argument)
        # elif command == '/create':
        #     if argument == '':
        #         client.send(Create)
        #     else:
        #         client.send(Create, channel=argument)
        # else:
        #     w.prnt('Unknown command {0}'.format(command))

def on_misc(client, u):
    if isinstance(u, Failure):
        dst = my_buffer
        if isinstance(u, UpdateFailure):
            dst = my_buffers.get(redir_id_to_buffer.pop(u['update-id'], None), my_buffer)
        w.prnt(dst, f"{w.prefix('error')}ERROR: {u.text}")

def on_message(client, u):
    redir_id_to_buffer.pop(u['id'], None)
    w.prnt("", f"{w.prefix('info')}{redir_id_to_buffer}")
    if u.channel not in my_buffers:
        my_buffers[u.channel] = w.buffer_new(f"lichat.{u.channel}", "lichat_buffer_input_cb", u.channel, "", "")
    w.prnt(my_buffers[u.channel], f"{u['from']}\t{u.text}")

def on_join(client, u):
    if u.channel not in my_buffers:
        my_buffers[u.channel] = w.buffer_new(f"lichat.{u.channel}", "lichat_buffer_input_cb", u.channel, "", "")
    w.prnt(my_buffers[u.channel], f"{w.prefix('join')}{u['from']} has joined {u.channel}")

def on_leave(client, u):
    if u.channel in my_buffers:
        w.prnt(my_buffers[u.channel], f"{w.prefix('quit')}{u['from']} has left {u.channel}")

# class MyClient(Client):
#     def connect_raw(self, host, port):
#         pass

def client_socket_cb(data, fd):
    for update in my_client.recv():
        w.prnt("", f"lichat\t{update.id} {type(update)}")
        my_client.handle(update)
    return w.WEECHAT_RC_OK

# def connect_cb(data, status, gnutls_rc, sock, error, ip_address):
#     if status == w.WEECHAT_HOOK_CONNECT_OK:
#         my_client.socket = socket.socket(fileno=sock)
#         my_client.connect("chat.tymoon.eu", 1111)
#         w.hook_fd(sock,
#                   1,            # flag_read
#                   0,            # flag_write
#                   0,            # flag_exception
#                   'client_socket_cb',
#                   '')
#     else:
#         w.prnt("", f"{w.prefix('error')}\tlichat connect fail: {status} {error}")
#     return w.WEECHAT_RC_OK

def lichat_cb(data, buffer, args_str):
    args = shlex.split(args_str)
    # w.prnt(buffer, f"/lichat\t{args}")
    if len(args) == 0:
        return w.WEECHAT_RC_ERROR

    cmd = args.pop()
    if cmd == "connect":
        global my_client, my_buffer
        my_buffer = w.buffer_new("lichat", "lichat_buffer_input_cb", "", "", "")
        my_client = Client(None)
        my_client.add_handler(Update, on_misc)
        my_client.add_handler(Message, on_message)
        my_client.add_handler(Join, on_join)
        my_client.add_handler(Leave, on_leave)

        if True:
            my_client.connect("chat.tymoon.eu", 1111)
            w.hook_fd(my_client.socket.fileno(),
                      1,            # flag_read
                      0,            # flag_write
                      0,            # flag_exception
                      'client_socket_cb',
                      '')
        # else:
        #     w.hook_connect("",      # proxy
        #                    "chat.tymoon.eu", 1111,
        #                    0,       # ipv6
        #                    0,       # retry
        #                    "",      # local_hostname
        #                    "connect_cb",
        #                    "")
    else:
        w.prnt("", f"{w.prefix('error')}Error with command \"/lichat {args_str}\" (help on command: /help lichat)")
        return w.WEECHAT_RC_ERROR

    return w.WEECHAT_RC_OK

if __name__ == '__main__' and import_ok:
    if w.register(SCRIPT_NAME, SCRIPT_AUTHOR, SCRIPT_VERSION,
                        SCRIPT_LICENSE, SCRIPT_DESC, '', ''):
        extensions.remove('shirakumo-emotes')

        w.hook_command('lichat',                   # command
                             'lichat description', # description
                             'args',               # args
                             'args_description',   # args_description
                             '',                   # completion
                             'lichat_cb', '')
        w.hook_command_run('/join', 'join_command_cb', '')
        w.hook_command_run('/part', 'part_command_cb', '')
        w.prnt("", "lichat.py\tis loaded ok")
