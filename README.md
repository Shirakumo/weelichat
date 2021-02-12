# About
This is a Weechat script to connect to [Lichat](https://shirakumo.github.io/lichat) servers. It has support for most Lichat extensions, including data sending and receiving.

Make sure you have [pylichat](https://github.com/shirakumo/py-lichat) available in your Python3 path. You can either clone it, or install it via [pip](https://pypi.org/project/pylichat/).

The script has help for all of its commands under the `/lichat` prefix, and adds hooks to replace most standard IRC commands while in a Lichat buffer.

If you want to chat with us about the development, join `lichat://chat.tymoon.eu/lichatters` or `irc://irc.freenode.net/#shirakumo`.

## Easy Setup
On the command line:
```bash
pip install pylichat && curl -o ~/.weechat/python/lichat.py https://raw.githubusercontent.com/shirakumo/weelichat/lichat.py
```
Then in Weechat:
```
/script load lichat.py
/script autoload lichat.py
/lichat connect
/lichat help
```
