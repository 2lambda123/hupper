import io
import importlib
import os
import struct
import sys
import subprocess
import threading

from .compat import WIN
from .compat import pickle
from .compat import queue


def resolve_spec(spec):
    modname, funcname = spec.rsplit('.', 1)
    module = importlib.import_module(modname)
    func = getattr(module, funcname)
    return func


if WIN:  # pragma: no cover
    import msvcrt
    from . import winapi

    class ProcessGroup(object):
        def __init__(self):
            self.h_job = winapi.CreateJobObject(None, None)

            info = winapi.JOBOBJECT_BASIC_LIMIT_INFORMATION()
            info.LimitFlags = winapi.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

            extended_info = winapi.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            extended_info.BasicLimitInformation = info

            winapi.SetInformationJobObject(
                self.h_job,
                winapi.JobObjectExtendedLimitInformation,
                extended_info,
            )

        def add_child(self, pid):
            hp = winapi.OpenProcess(winapi.PROCESS_ALL_ACCESS, False, pid)
            try:
                return winapi.AssignProcessToJobObject(self.h_job, hp)
            except OSError as ex:
                if getattr(ex, 'winerror') == 5:
                    # skip ACCESS_DENIED_ERROR on windows < 8 which occurs when
                    # the process is already attached to another job
                    pass
                else:
                    raise

    def snapshot_termios(fd):
        pass

    def restore_termios(fd, state):
        pass

    def get_handle(fd):
        return msvcrt.get_osfhandle(fd)

    def open_handle(handle, mode):
        flags = 0
        if 'w' not in mode and '+' not in mode:
            flags |= os.O_RDONLY
        if 'b' not in mode:
            flags |= os.O_TEXT
        if 'a' in mode:
            flags |= os.O_APPEND
        return msvcrt.open_osfhandle(handle, flags)

else:
    import termios

    class ProcessGroup(object):
        def add_child(self, pid):
            # nothing to do on *nix
            pass

    def snapshot_termios(fd):
        if os.isatty(fd):
            state = termios.tcgetattr(fd)
            return state

    def restore_termios(fd, state):
        if os.isatty(fd) and state:
            termios.tcflush(fd, termios.TCIOFLUSH)
            termios.tcsetattr(fd, termios.TCSANOW, state)

    def get_handle(fd):
        return fd

    def open_handle(handle, mode):
        return handle


def Pipe():
    c2pr_fd, c2pw_fd = os.pipe()
    p2cr_fd, p2cw_fd = os.pipe()

    c1 = Connection(c2pr_fd, p2cw_fd, p2cr_fd, c2pw_fd)
    c2 = Connection(p2cr_fd, c2pw_fd, c2pr_fd, p2cw_fd)
    return c1, c2

class Connection(object):
    """
    A connection to a bi-directional pipe.

    """
    def __init__(self, r_fd, w_fd, remote_r_fd, remote_w_fd):
        self.r_fd = r_fd
        self.w_fd = w_fd
        self.remote_r_fd = remote_r_fd
        self.remote_w_fd = remote_w_fd

    def __getstate__(self):
        return {
            'r_handle': get_handle(self.r_fd),
            'w_handle': get_handle(self.w_fd),
            'remote_r_handle': get_handle(self.remote_r_fd),
            'remote_w_handle': get_handle(self.remote_w_fd),
        }

    def __setstate__(self, state):
        self.r_fd = open_handle(state['r_handle'], 'rb')
        self.w_fd = open_handle(state['w_handle'], 'wb')
        self.remote_r_fd = open_handle(state['remote_r_handle'], 'rb')
        self.remote_w_fd = open_handle(state['remote_w_handle'], 'wb')

    def activate(self):
        close_fd(self.remote_r_fd, raises=False)
        close_fd(self.remote_w_fd, raises=False)

        self.r = os.fdopen(self.r_fd, 'rb')
        self.w = os.fdopen(self.w_fd, 'wb')

        self.send_lock = threading.Lock()
        self.reader_queue = queue.Queue()

        self.reader_thread = threading.Thread(target=self._read_loop)
        self.reader_thread.daemon = True
        self.reader_thread.start()

    def close(self):
        self.r.close()
        self.w.close()

    def _recv_packet(self):
        buf = io.BytesIO()
        chunk = self.r.read(8)
        if not chunk:
            return
        size = remaining = struct.unpack('Q', chunk)[0]
        while remaining > 0:
            chunk = self.r.read(remaining)
            n = len(chunk)
            if n == 0:
                if remaining == size:
                    raise EOFError
                else:
                    raise IOError('got end of file during message')
            buf.write(chunk)
            remaining -= n
        return pickle.loads(buf.getvalue())

    def _read_loop(self):
        try:
            while True:
                packet = self._recv_packet()
                if packet is None:
                    break
                self.reader_queue.put(packet)
        except EOFError:
            pass
        self.reader_queue.put(None)

    def send(self, value):
        data = pickle.dumps(value)
        with self.send_lock:
            self.w.write(struct.pack('Q', len(data)))
            self.w.write(data)
            self.w.flush()
        return len(data) + 8

    def recv(self, timeout=None):
        packet = self.reader_queue.get(block=True, timeout=timeout)
        return packet


def set_inheritable(fd):
    # py34 and above sets CLOEXEC automatically on file descriptors
    # and we want to prevent that from happening
    if hasattr(os, 'get_inheritable') and not os.get_inheritable(fd):
        os.set_inheritable(fd, True)


def close_fd(fd, raises=True):
    if fd is not None:
        try:
            os.close(fd)
        except Exception:  # pragma: nocover
            if raises:
                raise


def args_from_interpreter_flags():
    """
    Return a list of command-line arguments reproducing the current
    settings in sys.flags and sys.warnoptions.

    """
    flag_opt_map = {
        'debug': 'd',
        'dont_write_bytecode': 'B',
        'no_user_site': 's',
        'no_site': 'S',
        'ignore_environment': 'E',
        'verbose': 'v',
        'bytes_warning': 'b',
        'quiet': 'q',
        'optimize': 'O',
    }
    args = []
    for flag, opt in flag_opt_map.items():
        v = getattr(sys.flags, flag, 0)
        if v > 0:
            args.append('-' + opt * v)
    for opt in sys.warnoptions:
        args.append('-W' + opt)
    return args


def get_command_line(**kwds):
    prog = 'from hupper.ipc import spawn_main; spawn_main(%s)'
    prog %= ', '.join('%s=%r' % item for item in kwds.items())
    opts = args_from_interpreter_flags()
    return [sys.executable] + opts + ['-c', prog]


def get_preparation_data():
    data = {}
    data['sys.argv'] = sys.argv
    return data


def prepare(data):
    if 'sys.argv' in data:
        sys.argv = data['sys.argv']


def spawn(spec, kwargs, pass_fds=()):
    """
    Invoke a python function in a subprocess.

    """
    r, w = os.pipe()
    for fd in [r] + list(pass_fds):
        set_inheritable(fd)

    preparation_data = get_preparation_data()

    r_handle = get_handle(r)
    args = get_command_line(pipe_handle=r_handle)
    process = subprocess.Popen(args, close_fds=False)

    to_child = os.fdopen(w, 'wb')
    to_child.write(pickle.dumps([preparation_data, spec, kwargs]))
    to_child.close()

    return process


def spawn_main(pipe_handle):
    fd = open_handle(pipe_handle, 'rb')
    from_parent = os.fdopen(fd, 'rb')
    preparation_data, spec, kwargs = pickle.load(from_parent)
    from_parent.close()

    prepare(preparation_data)

    modname, funcname = spec.rsplit('.', 1)
    module = importlib.import_module(modname)
    func = getattr(module, funcname)

    func(**kwargs)
    sys.exit(0)
