#!/usr/bin/python

import sublime
import sublime_plugin

import bz2
import datetime
import hashlib
import json
import os
import os.path
import re
import select
import shutil
import subprocess
import sys
import threading
import time
import traceback

from subprocess import PIPE


##############################################################
# RemoteCpp Settings
##############################################################

def _get_or_default(setting, default, view=None):
  if view == None:
    view = sublime.active_window().active_view()
  return view.settings().get(setting, default)

def s_ssh():
  return _get_or_default('remote_cpp_ssh', 'ssh')

def s_ssh_port():
  return int(_get_or_default('remote_cpp_ssh_port', 8888))

def s_cwd(view=None):
  return _get_or_default('remote_cpp_cwd', 'cwd', view)

def s_scp():
  return _get_or_default('remote_cpp_scp', 'scp')

def s_build_cmd():
  return _get_or_default('remote_cpp_build_cmd', 'buck build')

def s_find_cmd():
  return _get_or_default('remote_cpp_find_cmd',
      ("find . -maxdepth 5 -not -path '*/\\.*' -type f -print "
          "-not -path '*buck-cache*' -not -path '*buck-out*'"))

def s_grep_cmd():
  return _get_or_default('remote_cpp_grep_cmd', 'grep  -R -n \'{pattern}\' .')

def s_single_file_list():
  return _get_or_default('remote_cpp_single_file_list', True)


##############################################################
# Constants
##############################################################

LS_VIEW_TITLE_PREFIX = 'ListView'
LOG_TYPES = set(('', 'RemoteCppGotoBuildErrorCommand'))
CPP_EXTENSIONS = set([
    '.c',
    '.cpp',
    '.h',
    '.cc',
])
FILE_LIST_PREAMBLE = '''
# Press 'Enter' on the selected remote file to open it.
#
# CWD={cwd}
# Took {millis} millis to list all files.
'''.lstrip()


##############################################################
# RemoteCpp Classes
##############################################################

class CmdListener(object):
  def on_stdout(self, line):
    log('stdout: ' + line)

  def on_stderr(self, line):
    log('stderr: ' + line)

  def on_exit(self, exit_code):
    log('Exit code: {0}'.format(exit_code))


class ListFilesListener(CmdListener):
  def __init__(self, view=None, prefix=''):
    self.last_flush_secs = 0
    self.file_list = []
    self.to_flush = []
    self.prefix = prefix
    if view == None:
      self.listener = None
    else:
      self.listener = AppendToViewListener(view)

  def on_stdout(self, line):
    self.file_list.append(line)

  def on_stderr(self, line):
    if None != self.listener:
      self.listener.on_stderr(line)

  def on_exit(self, exit_code):
    self.normalise_file_list()
    if None != self.listener:
      filtered_files = []
      for path in self.file_list:
        if path.startswith(self.prefix):
          filtered_files.append(path)
      self.listener.on_stdout('\n'.join(filtered_files) + '\n')
      self.listener.on_exit(exit_code)

  def normalise_file_list(self):
    file_list = self.file_list
    new_list = []
    for path in file_list:
      path = normalise_path(path)
      if len(path) == 0:
        continue
      new_list.append(path)
    new_list = sorted(new_list, key=lambda s: s.lower())
    self.file_list = new_list


class AppendToViewListener(CmdListener):
  def __init__(self, view):
    self._view = view
    self._start_secs = time.time()
    self._last_flush = 0
    self._buffer = []

  def on_stdout(self, line):
    self._buffer.append(line)
    self._try_flush_buffer()

  def on_stderr(self, line):
    Commands.append_text(self._view, line)

  def on_exit(self, exit_code):
    self._try_flush_buffer(force=True)
    if exit_code == 0:
      line = ('\n# [{time}] Command finished successfully.'
          ' in {millis} millis.\n').format(
          millis=delta_millis(self._start_secs),
          time=time_str(),
      )
    else:
      line = ('\n\n# [{time}]Command failed with exit code [{code}] '
          'in {millis} millis.\n').format(
          code=exit_code,
          millis=delta_millis(self._start_secs),
          time=time_str(),
      )
    Commands.append_text(self._view, line)

  def _try_flush_buffer(self, force=False):
    now_secs = time.time()
    if (not force) and (now_secs - self._start_secs < 1):
      return
    self._start_secs = now_secs
    text = ''.join(self._buffer)
    self._buffer = []
    Commands.append_text(self._view, text)


class CaptureCmdListener(CmdListener):
  def __init__(self):
    self._out = []
    self._err = []
    self._exit_code = None

  def on_stdout(self, line):
    self._out.append(line)

  def on_stderr(self, line):
    assert not '\n' in line
    self._err.append(line)

  def on_exit(self, exit_code):
    self._exit_code = exit_code

  def out(self):
    return self._out

  def err(self):
    return self._err

  def exit_code(self):
    return int(self._exit_code)


class File(object):
  def __init__(self, cwd, path, row=0, col=0):
    self.cwd = normalise_path(cwd)
    self.path = normalise_path(path)
    self.col = col
    self.row = row

  def remote_path(self):
    return os.path.join(self.cwd, self.path)

  def local_path(self, call_makedirs=True):
    local_path = os.path.join(
        self.local_root(),
        self.path)
    directory = os.path.dirname(local_path)
    if call_makedirs and not os.path.isdir(directory):
      log('Creating directory [{0}]...'.format(directory))
      os.makedirs(directory)
    return local_path

  @staticmethod
  def local_root_for_cwd(cwd):
    local_root = os.path.join(
        plugin_dir(),
        md5(cwd))
    return local_root

  def local_root(self):
    return self.local_root_for_cwd(self.cwd)

  def to_args(self):
    args = {
      'cwd': self.cwd,
      'path': self.path,
    }
    if self.row != 0:
      args['row'] = self.row
    if self.col != 0:
      args['col'] = self.col
    return args


class ThreadPool(object):
  def __init__(self, number_threads):
    self._lock = threading.Lock()
    self._tasks_running = 0
    self._progress_animation = None

  def run(self, callback):
    with self._lock:
      self._tasks_running += 1
      if self._tasks_running == 1:
        self._progress_animation = ProgressAnimation(self.tasks_running).start()
    def callback_wrapper():
      try:
        callback()
      except Exception as e:
        log_exception(
            'Background task failed with exception: [{exception}]'.format(
                exception=e))
      with self._lock:
        self._tasks_running -= 1
    log('tasks running = ' + str(self._tasks_running))
    sublime.set_timeout_async(callback_wrapper, 0)

  def tasks_running(self):
    with self._lock:
      return self._tasks_running

  def close(self):
    if self._progress_animation:
      self._progress_animation.close()
      self._progress_animation = None


class ProgressAnimation(object):
  def __init__(self, tasks_running):
    self._len = 35  # Arbitrary value.
    self._pos = self._len
    self._tasks_running = tasks_running

  def start(self):
    self._pos = self._len
    self._schedule_next_cycle()

  def _schedule_next_cycle(self):
    sublime.set_timeout(self._run_progress_animation, 25)

  def _run_progress_animation(self):
    try:
      if self._tasks_running() > 0:
        self._draw_animation()
        self._pos = (self._pos + 1) % (self._len * 2)
        self._schedule_next_cycle()
      else:
        sublime.status_message('')
    except Exception as e:
      log_exception('Exception running animation: ' + e)
      sublime.status_message('')

  def _draw_animation(self):
    water = ' '
    if self._pos < self._len:
      fish = '<><'
    else:
      fish = '><>'
    pos = abs(self._pos - self._len)
    msg = '(' + water * (pos)
    msg += fish
    msg += water * (self._len - pos) + ')'
    tasks = self._tasks_running()
    if tasks > 1:
      msg += ' x' + str(tasks)
    sublime.status_message(msg)

  def close(self):
    self._tasks_running = lambda : 0


class PluginState(object):
  STATE_FILE = 'RemoteCpp.PluginState.json.bz2'

  # Latest file_lists per CWD.
  # old: map<window_id, files_list>
  # new: map<cwd, vector<path>>
  LISTS = 'file_lists'

  def __init__(self, state=dict()):
    self.state = state
    if not self.LISTS in self.state:
      self.state[self.LISTS] = {}

  def file(self, local_path):
    if local_path == None:
      return None
    for cwd in all_cwds():
      local_root = File.local_root_for_cwd(cwd)
      if local_path.startswith(local_root):
        # the +1 is to remove the / (path separator character)
        return File(cwd=cwd, path=local_path[len(local_root) + 1:])
    return None

  def list(self, cwd):
    if window_id in self.state[self.LISTS]:
      return self.state[self.LISTS][cwd]
    return None

  def set_list(self, cwd, file_list):
    self.state[self.LISTS][cwd] = file_list

  def gc(self):
    log('RemoteCpp is GC\'ing the PluginState...')
    start_secs = time.time()
    all_local_paths = set()
    cwds = all_cwds()
    for cwd in tuple(self.state[self.LISTS].keys()):
      if not cwd in cwds:
        log('Deleting file list for cwd [{0}].'.format(cwd))
        del self.state[self.LISTS][cwd]
    millis = delta_millis(start_secs)
    log('RemoteCpp finished GC in {millis} millis.'.format(millis=millis))

  def load(self):
    start_secs = time.time()
    path = PluginState._path()
    if not os.path.isfile(path):
      return
    log('Reading RemoteCpp PluginState from [{0}]...'.format(path))
    with bz2.open(path, 'rt') as fp:
      self.state = json.load(fp)
    size_bytes = os.path.getsize(path)
    millis = delta_millis(start_secs)
    log('Successfully loaded {bytes} bytes in {millis} millis.'.format(
        bytes=size_bytes,
        millis=millis))

  def save(self):
    start_secs = time.time()
    raw = json.dumps(self.state, indent=2)
    path = self._path()
    log('Writing RemoteCpp PluginState to [{0}]...'.format(path))
    dir = os.path.dirname(path)
    if not os.path.isdir(dir):
      os.makedirs(dir)
    with bz2.open(path, 'wt') as fp:
      fp.write(raw)
    millis = delta_millis(start_secs)
    size_bytes = os.path.getsize(path)
    log('Successully wrote {bytes} bytes in {millis}.'.format(
        bytes=size_bytes,
        millis=millis))

  @staticmethod
  def _path():
    path = os.path.join(
      plugin_dir(),
      PluginState.STATE_FILE)
    return path


##############################################################
# RemoteCpp Functions
##############################################################

def plugin_dir():
  return os.path.join(sublime.cache_path(), 'RemoteCpp')

def all_cwds():
  cwds = set()
  for window in sublime.windows():
    for view in window.views():
      cwds.add(s_cwd(view))
  return cwds

def normalise_path(path):
  path = path.strip()
  if path.startswith('./'):
    path = path[2:]
  return path

def get_sel_line(view):
  all_regs = view.sel()
  if len(all_regs) > 1:
    log('Only one selection must exist.')
    return None
  lines = view.lines(all_regs[0])
  if len(lines) != 1:
    log('Selection can only affect one line.')
    return None
  line = lines[0]
  text = view.substr(line)
  return text

def is_remote_cpp_file(view):
  file = STATE.file(view.file_name())
  if file == None:
    return False
  _, extension = os.path.splitext(file.path)
  return extension.lower() in CPP_EXTENSIONS

def download_file(file):
  log('Downloading the file [{file}]...'.format(file=file.remote_path()))
  run_cmd((
      'scp',
      '-P', str(s_ssh_port()),
      'localhost:{path}'.format(path=file.remote_path()),
      '{path}'.format(path=file.local_path())
  ))
  log('Done downloading the file into [{file}].'.format(file=file.local_path()))

def create_cmd_ssh_args(cmd_str):
  args = [ s_ssh(), '-p {0}'.format(s_ssh_port()), 'localhost', cmd_str ]
  return args

def ssh_cmd(cmd_str, listener=CmdListener()):
  args = create_cmd_ssh_args(cmd_str)
  run_cmd(args, listener)

def run_cmd(cmd_list, listener=CmdListener()):
  proc = subprocess.Popen(cmd_list,
      stdin=None,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      bufsize=1)
  stdout = proc.stdout
  stderr = proc.stderr
  def read_fd(read_function, on_text_callback):
    text = read_function().decode('utf-8')
    size = 0
    for line in text.splitlines(keepends=True):
      on_text_callback(line)
      size += len(line)
    return size
  while True:
    ret = select.select([ stdout, stderr ], [], [])
    for fd in ret[0]:
      read_bytes = 0
      if fd == stdout:
        read_bytes += read_fd(stdout.readline, listener.on_stdout)
      if fd == stderr:
        read_bytes += read_fd(stderr.readline, listener.on_stderr)
    if read_bytes == 0 and proc.poll() != None:
      # fd.readline() does not return the end of the stream because it does
      # not end with a newline so we use the fd.read() call that guarantees
      # all available characters are read.
      read_fd(stdout.read, listener.on_stdout)
      read_fd(stderr.read, listener.on_stderr)
      listener.on_exit(proc.returncode)
      return

def time_str():
  return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(msg, type=''):
  if type in LOG_TYPES:
    if len(type) > 0:
      msg = '{type}: {msg}'.format(type=type, msg=msg)
    print(msg)

def log_exception(msg):
  print(msg + '\n + ' + traceback.format_exc())

def md5(msg):
  m = hashlib.md5()
  m.update(msg.encode())
  return m.hexdigest()

def show_file_input(view, title, on_done):
  file = STATE.file(view.file_name())
  if file == None:
    path = s_cwd()
    path = path + os.sep
  else:
    path = file.remote_path()
  def on_done_callback(new_file):
    log('The user has chosen: ' + new_file)
    cwd = s_cwd()
    if not new_file.startswith(cwd):
      sublime.error_message('File must be under CWD:\n\n' + cwd)
      return
    path = new_file[len(cwd) + 1:]
    file = File(cwd=cwd, path=path)
    on_done(file)
  view.window().show_input_panel(
      caption=title,
      initial_text=path,
      on_done=on_done_callback,
      on_change=None,
      on_cancel=None,
  )

def delta_millis(start_secs):
  return int(1000 * (time.time() - start_secs))


##############################################################
# Sublime EventListeners
##############################################################

def plugin_loaded():
  global THREAD_POOL, STATE
  THREAD_POOL = ThreadPool(1)
  try:
    STATE.load()
  except:
    log_exception('Critical problem loading the plugin STATE file.')
  sublime.set_timeout_async(STATE.gc, 5000)
  log('RemoteCpp has loaded successfully! :)')


def plugin_unloaded():
  global THREAD_POOL, STATE
  try:
    STATE.save()
  except:
    log_exception("Critical failure saving RemoteCpp plugin STATE.")
  THREAD_POOL.close()


class PluginStateEventListener(sublime_plugin.EventListener):
  def __init__(self):
    self.last_save_secs = time.time()

  def on_new(self, view):
    self._save()

  def on_close(self, view):
    self._save()

  def _save(self):
    now_secs = time.time()
    if now_secs - self.last_save_secs < 20:
      return
    self.last_save_secs = now_secs
    sublime.set_timeout_async(STATE.save, 1000)


class SaveFileEventListener(sublime_plugin.EventListener):
  def on_post_save(self, view):
    file = STATE.file(view.file_name())
    if file:
      log('Saving file: ' + str(file.local_path()))
      runnable = lambda : self._run_in_the_background(file)
      THREAD_POOL.run(runnable)

  def _run_in_the_background(self, file):
    log('Saving file [{0}]...'.format(file.remote_path()))
    run_cmd((
        s_scp(),
        '-P', str(s_ssh_port()),
        '{path}'.format(path=file.local_path()),
        'localhost:{path}'.format(path=file.remote_path()),
    ))
    log('Successsfully saved file [{0}].'.format(file.local_path()))


class ListFilesEventListener(sublime_plugin.EventListener):
  def on_text_command(self, view, command_name, args):
    if RemoteCppListFilesCommand.owns_view(view) and \
        command_name == 'insert' and args['characters'] == '\n':
      path = get_sel_line(view)
      log(path)
      if self._is_valid_path(path):
        args = File(cwd=s_cwd(), path=path).to_args()
        return (RemoteCppOpenFileCommand.NAME, args)
    return None

  def _is_valid_path(self, line):
    if line == None:
      return False
    line = line.strip()
    return len(line) > 0 and not line.startswith('#')


class GotoBuildErrorEventListener(sublime_plugin.EventListener):
  def on_text_command(self, view, command_name, args):
    if RemoteCppBuildCommand.owns_view(view) and \
        command_name == 'insert' and args['characters'] == '\n':
      Commands.goto_build_error(view)
    return None


class GotoGrepMatchEventListener(sublime_plugin.EventListener):
  def on_text_command(self, view, command_name, args):
    if RemoteCppGotoGrepMatchCommand.is_valid(view) and \
        command_name == 'insert' and args['characters'] == '\n':
      Commands.goto_grep_match(view)
    return None


##############################################################
# Sublime Commands
##############################################################

class Commands(object):
  def __init__(self):
    raise Exception('Not to be instantiated.')

  @staticmethod
  def append_text(view, text, clean_first=False):
    ''' Appends the text and moves scroll to the bottom '''
    view.run_command(RemoteCppAppendTextCommand.NAME, {
        'text': text,
        'clean_first': clean_first,
    })

  @staticmethod
  def open_file(view, args_to_create_file):
    ''' Fails if file does not exist '''
    view.run_command(RemoteCppOpenFileCommand.NAME, args_to_create_file)

  @staticmethod
  def toggle_header_implementation(view):
    view.run_command(RemoteCppToggleHeaderImplementationCommand.NAME)

  @staticmethod
  def goto_build_error(view):
    view.run_command(RemoteCppGotoBuildErrorCommand.NAME)

  @staticmethod
  def goto_grep_match(view):
    view.run_command(RemoteCppGotoGrepMatchCommand.NAME)


class RemoteCppDeleteFileCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_delete_file'

  def is_enabled(self):
    return None != STATE.file(self.view.file_name())

  def is_visible(self):
    return self.is_enabled()

  def run(self, edit):
    file = STATE.file(self.view.file_name())
    title = 'Delete file:\n\n{0}'.format(file.remote_path())
    if sublime.ok_cancel_dialog(title, 'Delete'):
      log("Deleting the file...")
      cmd_str = 'rm -f {remote_path}'.format(remote_path=file.remote_path())
      ssh_cmd(cmd_str)
      self.view.close()

class RemoteCppGcCommand(sublime_plugin.TextCommand):
  def run(self, edit):
    STATE.gc()


class RemoteCppGotoGrepMatchCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_goto_grep_match'
  REGEX = re.compile('^([^:]+):(\d+):.+$')

  @staticmethod
  def is_valid(view):
    if not view.name().startswith(RemoteCppGrepCommand.VIEW_PREFIX):
      return False
    return None != RemoteCppGotoGrepMatchCommand.REGEX.match(get_sel_line(view))

  def is_enabled(self):
    return RemoteCppGotoGrepMatchCommand.is_valid(self.view)

  def is_visible(self):
    return self.is_enabled()

  def run(self, edit):
    line = get_sel_line(self.view)
    match = self.REGEX.match(line)
    path = match.group(1)
    row = int(match.group(2))
    col = 0
    file = File(cwd=s_cwd(), path=path, row=row, col=col)
    Commands.open_file(self.view, file.to_args())


class RemoteCppGrepCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_grep'
  VIEW_PREFIX = 'Grep'

  def run(self, edit):
    log('Grepping file...')
    view = self.view
    window = view.window()
    text = ''
    if len(view.sel()) == 1:
      lines = view.lines(view.sel()[0])
      if len(lines) == 1:
        text = view.substr(view.sel()[0])
    view.window().show_input_panel(
        caption='Remote Grep',
        initial_text=text,
        on_done=lambda t: self._on_done(window, t),
        on_change=None,
        on_cancel=None
    )

  def _on_done(self, window, text):
    log('Grepping for text [{0}]...'.format(text))
    if len(text) == 0:
      return
    view = window.new_file()
    view.set_name('{prefix} [{time}]'.format(
        prefix=self.VIEW_PREFIX,
        time=time_str()))
    view.set_read_only(True)
    view.set_scratch(True)
    view.settings().set("word_wrap", "false")
    Commands.append_text(
        view,
        '# Grepping for [{text}] in [{cwd}]...\n\n'.format(
            cwd=s_cwd(),
            text=text,))
    runnable = lambda: self._run_in_the_background(view, text)
    THREAD_POOL.run(runnable)

  def _run_in_the_background(self, view, text):
    arg_template = "cd {cwd} && " + s_grep_cmd()
    arg_str = arg_template.format(
        cwd=s_cwd(),
        pattern=text,)
    log('Running cmd [{cmd}]...'.format(cmd=arg_str))
    listener = AppendToViewListener(view)
    ssh_cmd(arg_str, listener)


class RemoteCppMoveFileCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_move_file'

  def is_enabled(self):
    return None != STATE.file(self.view.file_name())

  def is_visible(self):
    return self.is_enabled()

  def run(self, edit):
    log('Moving file...')
    orig_file = STATE.file(self.view.file_name())
    callback = lambda dst_file: self._on_move(orig_file, dst_file)
    show_file_input(self.view, 'Move Remote File', callback)

  def _on_move(self, src_file, dst_file):
    runnable = lambda: self._run_in_the_background(src_file, dst_file)
    THREAD_POOL.run(runnable)

  def _run_in_the_background(self, src_file, dst_file):
    try:
      ssh_cmd('mv "{src}" "{dst}"'.format(
          src=src_file.remote_path(),
          dst=dst_file.remote_path()))
    except:
      log_exception('Failed to mv remote files.')
      sublime.error_message(
          'Failed to move files remotely.\n\nSRC={src}\n\nDST={dst}'.format(
                src=src_file.remote_path(),
                dst=dst_file.remote_path()))
      return
    self._rm_local_file(src_file.local_path())
    self._rm_local_file(dst_file.local_path())
    Commands.open_file(self.view, dst_file.to_args())
    log(dir(self.view))
    self.view.close()

  def _rm_local_file(self, path):
    try:
      os.remove(path)
    except:
      log_exception('Problems removing file [{0}].'.format(path))


class RemoteCppGotoBuildErrorCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_goto_build_error'
  REGEX = re.compile('^([^:]+):(\d+):(\d+):.+$')

  def run(self, edit):
    self.log('Going to build error from line...')
    view = self.view
    row = view.rowcol(view.sel()[0].a)[0]
    while row >= 0:
      self.log('Current row is [{0}].'.format(row))
      line = view.line(view.text_point(row, 0))
      text = view.substr(line)
      match = self.REGEX.match(text)
      if match:
        self.log('Found build error in line [{0}] => [{1}]'.format(row, text))
        path = match.group(1)
        row = int(match.group(2))
        col = int(match.group(3))
        self.log('Build error in file [{path}] row=[{row}] col=[{col}].'.format(
            path=path,
            row=row,
            col=col))
        file = File(cwd=s_cwd(), path=path, row=row, col=col)
        Commands.open_file(self.view, file.to_args())
        return
      row -= 1

  def log(self, msg):
    log(msg, type=type(self).__name__)


class RemoteCppNewFileCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_new_file'

  def run(self, edit):
    show_file_input(self.view, 'New Remote File', self._on_done)

  def _on_done(self, file):
    runnable = lambda : self._run_in_the_background(file)
    THREAD_POOL.run(runnable)

  def _run_in_the_background(self, file):
    new_path = file.remote_path()
    cmd = ('if [[ ! -f {remote_path} ]]; then mkdir -p {remote_dir}; fi; '
        ' touch {remote_path};').format(
            remote_path=new_path,
            remote_dir=os.path.dirname(new_path),
    )
    ssh_cmd(cmd)
    Commands.open_file(self.view, file.to_args())


class RemoteCppBuildCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_build'
  VIEW_PREFIX = 'Build'

  def run(self, edit):
    view = self.view.window().new_file()
    view.settings().set("word_wrap", "false")
    view.set_name('{prefix} [{time}]'.format(
        prefix=RemoteCppBuildCommand.VIEW_PREFIX,
        time=time_str()))
    view.set_read_only(True)
    view.set_scratch(True)
    status = '# [{time}] Building with cmd [{cmd}]...\n\n'.format(
        time=time_str(),
        cmd=self._build_cmd())
    Commands.append_text(view, status)
    THREAD_POOL.run(lambda : self._run_in_the_background(view))

  def _build_cmd(self):
    cwd = s_cwd()
    build_cmd = s_build_cmd()
    return "cd {cwd} && {build}".format(
        cwd=cwd,
        build=build_cmd,
    )

  def _run_in_the_background(self, view):
    listener = AppendToViewListener(view)
    ssh_cmd(self._build_cmd(), listener)

  @staticmethod
  def owns_view(view):
    return view.name().startswith(RemoteCppBuildCommand.VIEW_PREFIX)


class RemoteCppGotoIncludeCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_goto_include'
  REGEX = re.compile('^.+["<](.+)[">].*$')

  def is_enabled(self):
    return is_remote_cpp_file(self.view) and None != self._get_sel_path()

  def is_visible(self):
    return self.is_enabled()

  def run(self, edit):
    path = self._get_sel_path()
    file = File(cwd=s_cwd(), path=path)
    Commands.open_file(self.view, file.to_args())

  def _get_sel_path(self):
    line = get_sel_line(self.view)
    if line == None:
      return None
    match = self.REGEX.match(line)
    if match == None:
      return None
    return match.group(1)


class RemoteCppToggleHeaderImplementationCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_toggle_header_implementation'

  def is_enabled(self):
    return is_remote_cpp_file(self.view)

  def is_visible(self):
    return is_remote_cpp_file(self.view)

  def run(self, edit):
    log("Toggling between header and implementation...")
    view = self.view
    window = view.window()
    file = STATE.file(view.file_name())
    file_list = STATE.list(s_cwd())
    if file_list == None:
      log('No file list found so requesting one...')
      def run_in_the_background():
        file_list = RemoteCppListFilesCommand.get_file_list(window)
        if type(file_list) == list:
          log('Successfully downloaded the file list for toggle.')
          Commands.toggle_header_implementation(view)
        else:
          log('Failed to download the file list for toggle.')
      THREAD_POOL.run(run_in_the_background)
      return
    else:
      self._toggle(file, file_list)

  def _toggle(self, file, file_list):
    orig_path, orig_extension = os.path.splitext(file.path)
    sibblings = []
    log('Into the loop...')
    for file_path in file_list:
      path, extension = os.path.splitext(file_path)
      if orig_extension != extension and orig_path == path:
        sibblings.append(file_path)
    sibbling_count = len(sibblings)
    log('Out of the loop...')
    if sibbling_count == 0:
      log('No sibbling files were found.')
      return
    elif sibbling_count == 1:
      log('Only one file found so displaying it.')
      toggle_file = File(cwd=file.cwd, path=sibblings[0])
      Commands.open_file(self.view, toggle_file.to_args())
    else:
      log('Multiple matching files found: [{0}].'.format(', '.join(sibblings)))
      def on_select_callback(selected_index):
        if selected_index == -1:
          return
        toggle_file = File(cwd=file.cwd, path=sibblings[selected_index])
        Commands.open_file(self.view, toggle_file.to_args())
      self.view.window().show_quick_panel(
        items=sibblings,
        on_select=on_select_callback,
        selected_index=0)


class RemoteCppRefreshCacheCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_refresh_cache'

  def run(self, edit):
    window = self.view.window()
    runnable = lambda : self._run_in_the_background(window)
    THREAD_POOL.run(runnable)

  def _run_in_the_background(self, window):
    start_secs = time.time()
    files = []
    roots = set()
    for view in window.views():
      file = STATE.file(view.file_name())
      if file:
        files.append(file)
        roots.add(file.local_root())
    for root in roots:
        log('Deleting local cache directory [{0}]...'.format(root))
        if os.path.exists(root):
          shutil.rmtree(root)
    for file in files:
      log("Refreshing open file [{0}]...".format(file.remote_path()))
      download_file(file)
    millis = delta_millis(start_secs)
    msg = 'Successfully refreshed RemoteCpp cache in {0} millis.'.format(millis)
    log(msg)
    def update_state():
      sublime.status_message(msg)
    sublime.set_timeout(update_state, 500)


class RemoteCppOpenFileCommand(sublime_plugin.TextCommand):
    NAME = 'remote_cpp_open_file'

    def run(self, edit, cwd=None, path=None, row=0, col=0):
      if cwd and path:
        file = File(cwd=cwd, path=path, row=row, col=col)
        self._open_remote_file(file)
      else:
        show_file_input(self.view, 'Open Remote File', self._open_remote_file)

    def _open_remote_file(self, file):
      self.log("Opening => " + file.remote_path())
      remote_path = file.remote_path()
      local_path = file.local_path()
      window = self.view.window()
      # Shortcut if the file already exists.
      if os.path.isfile(local_path):
        self._open_file(window, file)
        return
      # Otherwise let's copy the file locally.
      THREAD_POOL.run(lambda : self._run_in_the_background(window, file))

    def _run_in_the_background(self, window, file):
      try:
        download_file(file)
        self._open_file(window, file)
      except:
        msg = 'Failed to open remote file:\n\n{0}'.format(file.remote_path())
        log_exception(msg)
        sublime.error_message(msg)

    def _open_file(self, window, file):
      self.log('rui => ' + str(file.to_args()))
      path_row_col = '{path}:{row}:{col}'.format(
          path=file.local_path(),
          row=file.row,
          col=file.col)
      view = window.open_file(path_row_col, sublime.ENCODED_POSITION)

    def log(self, msg):
      log(msg, type=type(self).__name__)


class RemoteCppListFilesInPathCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_list_files_in_path'

  def is_enabled(self):
    return is_remote_cpp_file(self.view)

  def is_visible(self):
    return is_remote_cpp_file(self.view)

  def run(self, edit):
    file = STATE.file(self.view.file_name())
    prefix = os.path.dirname(file.path)
    self.view.run_command(RemoteCppListFilesCommand.NAME, {'prefix': prefix})


class RemoteCppListFilesCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_list_files'
  VIEW_PREFIX = 'ListFiles'

  # 'prefix' corresponds to the 'path_prefix'.
  def run(self, edit, prefix=''):
    window = self.view.window()
    if s_single_file_list():
      view = self._find_file_list(prefix)
    if view == None:
      view = window.new_file()
    window.focus_view(view)
    view.set_name(self._get_title(prefix))
    view.set_read_only(True)
    view.set_scratch(True)
    view.settings().set("word_wrap", "false")
    THREAD_POOL.run(lambda: self._get_file_list(window, view, prefix))

  def _get_title(self, prefix):
    if len(prefix) == 0:
      return self.VIEW_PREFIX
    else:
      return '{view_prefix} - {path_prefix}'.format(
        view_prefix=self.VIEW_PREFIX,
        path_prefix=prefix,
      )

  def _find_file_list(self, prefix):
    title = self._get_title(prefix)
    for view in self.view.window().views():
      if view.name() == title:
        return view
    return None

  @staticmethod
  def _get_file_list(window, view, prefix):
    capture_listener = CaptureCmdListener()
    if view == None:
      listener = capture_listener
    else:
      if len(prefix) == 0:
        prefix_text = ''
      else:
        prefix_text = ' in path [{prefix}]'.format(prefix=prefix)
      title = '# [{time}] Listing files for CWD=[{cwd}]{prefix}...\n\n'.format(
          cwd=s_cwd(),
          prefix=prefix_text,
          time=time_str())
      Commands.append_text(view, title, clean_first=True)
      append_listener = AppendToViewListener(view)
    cmd_template = 'cd {cwd}; ' + s_find_cmd()
    cmd_str = cmd_template.format(cwd=s_cwd())
    listener = ListFilesListener(view=view, prefix=prefix)
    ssh_cmd(cmd_str, listener)
    STATE.set_list(s_cwd(), listener.file_list)
    return listener.file_list

  @staticmethod
  def get_file_list(window):
    return RemoteCppListFilesCommand._get_file_list(window, None, '')

  @staticmethod
  def owns_view(view):
    return view.name().startswith(RemoteCppListFilesCommand.VIEW_PREFIX)


class RemoteCppAppendTextCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_append_text'

  def run(self, edit, text='NO_TEXT_PROVIDED', clean_first=False):
    view = self.view
    view.set_read_only(False)
    if clean_first and view.size() > 0:
      view.erase(edit, sublime.Region(0, view.size()))
    view.insert(edit, view.size(), text)
    view.set_read_only(True)
    view.show(view.size() + 1)


##############################################################
# Plugin Global State
##############################################################

# Initialised in plugin_loaded()
THREAD_POOL = None

# key corresponds to View.id().
# value is an instance of 'class File' defined in this file.
# remote_files = {}

# An instance of PluginState class. Initialised in plugin_loaded().
STATE = PluginState()
