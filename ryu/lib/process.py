import sys
import os
import logging
import errno
import multiprocessing


LOG = logging.getLogger('ryu.lib.hub')


def get_errno(exc):
    """ Get the error code out of socket.error objects.
    socket.error in <2.5 does not have errno attribute
    socket.error in 3.x does not allow indexing access
    e.args[0] works for all.
    There are cases when args[0] is not errno.
    i.e. http://bugs.python.org/issue6471
    Maybe there are cases when errno is set, but it is not the first argument?
    """

    try:
        if exc.errno is not None:
            return exc.errno
    except AttributeError:
        pass
    try:
        return exc.args[0]
    except IndexError:
        return None


def cpu_count():
    """Returns the number of processors on this machine."""
    if multiprocessing is None:
        return 1
    try:
        return multiprocessing.cpu_count()
    except NotImplementedError:
        pass
    try:
        return os.sysconf("SC_NPROCESSORS_CONF")
    except (AttributeError, ValueError):
        pass
    LOG.error("Could not detect number of processors; assuming 1")
    return 1


_task_id = None


def fork_processes(num_processes, max_restarts=100):
    """Starts multiple worker processes.
    If ``num_processes`` is None or <= 0, we detect the number of cores
    available on this machine and fork that number of child
    processes. If ``num_processes`` is given and > 0, we fork that
    specific number of sub-processes.
    Since we use processes and not threads, there is no shared memory
    between any server code.
    Note that multiple processes are not compatible with the autoreload
    module (or the ``autoreload=True`` option to `tornado.web.Application`
    which defaults to True when ``debug=True``).
    When using multiple processes, no IOLoops can be created or
    referenced until after the call to ``fork_processes``.
    In each child process, ``fork_processes`` returns its *task id*, a
    number between 0 and ``num_processes``.  Processes that exit
    abnormally (due to a signal or non-zero exit status) are restarted
    with the same id (up to ``max_restarts`` times).  In the parent
    process, ``fork_processes`` returns None if all child processes
    have exited normally, but will otherwise only exit by throwing an
    exception.
    """
    global _task_id
    assert _task_id is None
    if num_processes is None or num_processes <= 0:
        num_processes = cpu_count()

    LOG.info("Starting %d processes", num_processes)
    children = {}

    def start_child(i):
        pid = os.fork()
        if pid == 0:
            # child process
            # _reseed_random()
            global _task_id
            _task_id = i
            return i
        else:
            children[pid] = i
            return None

    for i in range(num_processes):
        id = start_child(i)
        if id is not None:
            return id
    num_restarts = 0
    while children:
        try:
            pid, status = os.wait()
        except OSError as e:
            if get_errno(e) == errno.EINTR:
                continue
            raise
        if pid not in children:
            continue
        id = children.pop(pid)
        if os.WIFSIGNALED(status):
            LOG.warning("child %d (pid %d) killed by signal %d, restarting",
                        id, pid, os.WTERMSIG(status))
        elif os.WEXITSTATUS(status) != 0:
            LOG.warning("child %d (pid %d) exited with status %d, restarting",
                        id, pid, os.WEXITSTATUS(status))
        else:
            LOG.info("child %d (pid %d) exited normally", id, pid)
            continue
        num_restarts += 1
        if num_restarts > max_restarts:
            raise RuntimeError("Too many child restarts, giving up")
        new_id = start_child(id)
        if new_id is not None:
            return new_id
    # All child processes exited cleanly, so exit the master process
    # instead of just returning to right after the call to
    # fork_processes (which will probably just start up another IOLoop
    # unless the caller checks the return value).
    sys.exit(0)

