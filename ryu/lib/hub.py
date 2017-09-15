# Copyright (C) 2013 Nippon Telegraph and Telephone Corporation.
# Copyright (C) 2013 YAMAMOTO Takashi <yamamoto at valinux co jp>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import absolute_import, print_function

import logging
import os

import netaddr

from .process import fork_processes, get_errno

# We don't bother to use cfg.py because monkey patch needs to be
# called very early. Instead, we use an environment variable to
# select the type of hub.
HUB_TYPE = os.getenv('RYU_HUB_TYPE', 'gevent')
if HUB_TYPE.lower() not in ("eventlet", "gevent"):
    raise OSError("unknown coroutine library %s." % HUB_TYPE)

LOG = logging.getLogger('ryu.lib.hub')


if HUB_TYPE == 'eventlet':
    print("using eventlet.")
    import eventlet
    # HACK:
    # sleep() is the workaround for the following issue.
    # https://github.com/eventlet/eventlet/issues/401
    eventlet.sleep()
    import eventlet.event
    import eventlet.queue
    import eventlet.semaphore
    import eventlet.timeout
    import eventlet.wsgi
    from eventlet import websocket
    import greenlet
    import ssl
    import socket
    import traceback

    from eventlet.wsgi import ALREADY_HANDLED

    getcurrent = eventlet.getcurrent
    patch = eventlet.monkey_patch
    sleep = eventlet.sleep
    listen = eventlet.listen
    connect = eventlet.connect

    def spawn(*args, **kwargs):
        raise_error = kwargs.pop('raise_error', False)

        def _launch(func, *args, **kwargs):
            # Mimic gevent's default raise_error=False behaviour
            # by not propagating an exception to the joiner.
            try:
                return func(*args, **kwargs)
            except TaskExit:
                pass
            except BaseException as e:
                if raise_error:
                    raise e
                # Log uncaught exception.
                # Note: this is an intentional divergence from gevent
                # behaviour; gevent silently ignores such exceptions.
                LOG.error('hub: uncaught exception: %s',
                          traceback.format_exc())

        return eventlet.spawn(_launch, *args, **kwargs)

    def spawn_after(seconds, *args, **kwargs):
        raise_error = kwargs.pop('raise_error', False)

        def _launch(func, *args, **kwargs):
            # Mimic gevent's default raise_error=False behaviour
            # by not propagating an exception to the joiner.
            try:
                return func(*args, **kwargs)
            except TaskExit:
                pass
            except BaseException as e:
                if raise_error:
                    raise e
                # Log uncaught exception.
                # Note: this is an intentional divergence from gevent
                # behaviour; gevent silently ignores such exceptions.
                LOG.error('hub: uncaught exception: %s',
                          traceback.format_exc())

        return eventlet.spawn_after(seconds, _launch, *args, **kwargs)

    def kill(thread):
        thread.kill()

    def joinall(threads):
        for t in threads:
            # This try-except is necessary when killing an inactive
            # greenthread.
            try:
                t.wait()
            except TaskExit:
                pass

    Queue = eventlet.queue.LightQueue
    QueueEmpty = eventlet.queue.Empty
    Semaphore = eventlet.semaphore.Semaphore
    BoundedSemaphore = eventlet.semaphore.BoundedSemaphore
    TaskExit = greenlet.GreenletExit

    class StreamServer(object):
        def __init__(self, listen_info, handle=None, backlog=None,
                     spawn='default', **ssl_args):
            assert backlog is None
            assert spawn == 'default'

            if netaddr.valid_ipv6(listen_info[0]):
                self.server = eventlet.listen(listen_info,
                                              family=socket.AF_INET6)
            elif os.path.isdir(os.path.dirname(listen_info[0])):
                # Case for Unix domain socket
                self.server = eventlet.listen(listen_info[0],
                                              family=socket.AF_UNIX)
            else:
                self.server = eventlet.listen(listen_info)

            if ssl_args:
                def wrap_and_handle(sock, addr):
                    ssl_args.setdefault('server_side', True)
                    handle(ssl.wrap_socket(sock, **ssl_args), addr)

                self.handle = wrap_and_handle
            else:
                self.handle = handle

        def serve_forever(self):
            while True:
                sock, addr = self.server.accept()
                spawn(self.handle, sock, addr)

    class LoggingWrapper(object):
        def write(self, message):
            LOG.info(message.rstrip('\n'))

    class WSGIServer(StreamServer):
        def serve_forever(self):
            self.logger = LoggingWrapper()
            eventlet.wsgi.server(self.server, self.handle, self.logger)

    WebSocketWSGI = websocket.WebSocketWSGI

    Timeout = eventlet.timeout.Timeout

    class Event(object):
        def __init__(self):
            self._ev = eventlet.event.Event()
            self._cond = False

        def _wait(self, timeout=None):
            while not self._cond:
                self._ev.wait()

        def _broadcast(self):
            self._ev.send()
            # Since eventlet Event doesn't allow multiple send() operations
            # on an event, re-create the underlying event.
            # Note: _ev.reset() is obsolete.
            self._ev = eventlet.event.Event()

        def is_set(self):
            return self._cond

        def set(self):
            self._cond = True
            self._broadcast()

        def clear(self):
            self._cond = False

        def wait(self, timeout=None):
            if timeout is None:
                self._wait()
            else:
                try:
                    with Timeout(timeout):
                        self._wait()
                except Timeout:
                    pass

            return self._cond

elif HUB_TYPE == 'gevent':
    print("using gevent.")
    import sys
    import warnings

    import greenlet
    import gevent
    from gevent import socket
    from gevent import sleep, getcurrent, kill, joinall, spawn, Timeout
    from gevent.monkey import patch_all as patch
    from gevent.lock import Semaphore, BoundedSemaphore
    from gevent.queue import Queue, Empty as QueueEmpty
    from gevent.server import StreamServer
    from gevent.pywsgi import WSGIServer
    from gevent.event import Event


    def connect(addr, family=socket.AF_INET, bind=None):
        sock = socket.socket(family, socket.SOCK_STREAM)
        if bind is not None:
            sock.bind(bind)
        sock.connect(addr)
        return sock


    def listen(addr, family=socket.AF_INET, backlog=50, reuse_addr=True, reuse_port=None):
        sock = socket.socket(family, socket.SOCK_STREAM)
        if reuse_addr and sys.platform[:3] != 'win':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family in (socket.AF_INET, socket.AF_INET6) and addr[1] == 0:
            if reuse_port:
                warnings.warn(
                    '''listen on random port (0) with SO_REUSEPORT is dangerous.
                    Double check your intent.
                    Example problem: https://github.com/eventlet/eventlet/issues/411''',
                    UserWarning, stacklevel=3)
        elif reuse_port is None:
            reuse_port = True
        if reuse_port and hasattr(socket, 'SO_REUSEPORT'):
            # NOTE(zhengwei): linux kernel >= 3.9
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            # OSError is enough on Python 3+
            except (OSError, socket.error) as ex:
                if get_errno(ex) in (22, 92):
                    # A famous platform defines unsupported socket option.
                    # https://github.com/eventlet/eventlet/issues/380
                    # https://github.com/eventlet/eventlet/issues/418
                    warnings.warn(
                        '''socket.SO_REUSEPORT is defined but not supported.
                        On Windows: known bug, wontfix.
                        On other systems: please comment in the issue linked below.
                        More information: https://github.com/eventlet/eventlet/issues/380''',
                        UserWarning, stacklevel=3)

        sock.bind(addr)
        sock.listen(backlog)
        return sock

    spawn_after = gevent.spawn_later
    TaskExit = greenlet.GreenletExit


    class LoggingWrapper(object):
        def write(self, message):
            LOG.info(message.rstrip('\n'))

    from ._gevent_websocket import WebSocketWSGI

    class _AlreadyHandled(object):

        def __iter__(self):
            return self

        def next(self):
            raise StopIteration

    __next__ = next

    ALREADY_HANDLED = _AlreadyHandled()
