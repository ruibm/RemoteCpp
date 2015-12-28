#!/usr/bin/python

import sublime
import sublime_plugin

import datetime
import hashlib
import json
import os
import os.path
import re
import shutil
import subprocess
import sys
import threading
import traceback

from subprocess import PIPE


##############################################################
# RemoteCpp Settings
##############################################################

# TODO(ruibm): Maybe I should pass in a window/view argument here. This is a
# bit dangerous.
def _get_or_default(setting, default, view=None):
  if not view:
    view = sublime.active_window().active_view()
  return view.settings().get(setting, default)

def s_ssh():
  return _get_or_default('remote_cpp_ssh', 'ssh')

def s_ssh_port():
  return int(_get_or_default('remote_cpp_ssh_port', 8888))

def s_cwd():
  return _get_or_default('remote_cpp_cwd', 'cwd')

def s_scp():
  return _get_or_default('remote_cpp_scp', 'scp')

def s_build_cmd():
  return _get_or_default('build_cmd', 'buck build')


##############################################################
# Constants
##############################################################

LS_VIEW_TITLE_PREFIX = 'ListView'
LOG_TYPES = set(('',))
CWD_PREFIX = '# CWD='


##############################################################
# Plugin Global State
##############################################################

# Initialised in plugin_loaded()
thread_pool = None

# key corresponds to View.id().
# value is an instance of 'class File' defined in this file.
# remote_files = {}

# An instance of PluginState class. Initialised in plugin_loaded().
state = PluginState()


##############################################################
# Sublime EventListeners
##############################################################


def plugin_loaded():
  global thread_pool, state
  thread_pool = ThreadPool(1)
  try:
    state.load()
  except:
    log_exception('Critical problem loading the plugin state file.')
    # TODO(ruibm): Potentially mv the file to $NAME.old
  log('RemoteCpp has loaded successfully! :)')


def plugin_unloaded():
  global state
  try:
    state.save()
  except:
    log_exception("Critical failure saving RemoteCpp plugin state.")


class PluginStateListener(sublime_plugin.EventListener):
  def on_close(self, view):
    # Let's not leak memory in the PluginState.
    state.gc()


class LineSelectorEventListener(sublime_plugin.EventListener):
  # Whether this EventListener is enabled for the current View.
  def is_enabled(self, view):
    return False

  # Whether the current select line is valid.
  def is_valid(self, line):
    raise NotImplementedError()

  # Returns a tuple (cmd, args).
  def create_cmd(self, view, line):
    raise NotImplementedError()

  def on_text_command(self, view, command_name, args):
    if not self.is_enabled(view):
      return None
    if command_name == 'insert' and args['characters'] == '\n':
      for reg in view.sel():
        if reg.empty():
          line = view.line(reg)
          path = view.substr(line).strip()
          if not self.is_valid(path):
            continue
          return self.create_cmd(view, path)
    if command_name == 'drag_select' and 'additive' in args:
      self._sel = self._get_sel(view)
    log('pre: cmd=[{cmd}] args=[{args}]'.format(
        cmd=command_name, args=str(args)), type='on_text_command')
    return None

  def on_post_text_command(self, view, command_name, args):
    if not self.is_enabled(view):
      return
    if command_name == 'drag_select' and 'additive' in args:
      sel = self._get_sel(view)
      diff = sel.difference(self._sel)
      for point in diff:
        path = view.substr(view.line(point))
        if self.is_valid(path):
          cmd, args = self.create_cmd(view, path)
          view.run_command(cmd, args)
          return
    log('post: cmd=[{cmd}] args=[{args}]'.format(
        cmd=command_name, args=str(args)), type='on_text_command')
    return

  def _is_valid(self, line):
    return len(line.strip()) > 0 and not line.strip().startswith('#')

  def _cwd(self, view):
    line = view.substr(view.line(0))
    cwd = line[len(CWD_PREFIX):]
    return cwd

  def _file(self, view, path):
    cwd = self._cwd(view)
    file = File(cwd=cwd, path=path)
    return file

  def _get_sel(self, view):
    sel = set()
    for reg in view.sel():
      if reg.empty():
        sel.add(reg.a)
    return sel


class FollowIncludeEventListener(LineSelectorEventListener):
  REGEX = re.compile('^.+["<](.+)[">].+$')

  def is_enabled(self, view):
    return None != state.file(view.id())

  def is_valid(self, line):
    match = self.REGEX.match(line)
    return match != None

  def create_cmd(self, view, line):
    match = self.REGEX.match(line)
    path = match.group(1)
    args = File(cwd=s.cwd(), path=path).to_args()
    log("Sending this: {0}".format(str((RemoteCppOpenFileCommand.NAME, args))))
    return (RemoteCppOpenFileCommand.NAME, args)


class SaveFileEventListener(sublime_plugin.EventListener):
  def on_post_save(self, view):
    file = state.file(view.id())
    if file:
      log('Saving file: ' + str())
      runnable = lambda : self._run_in_the_background(file)
      thread_pool.run(runnable)

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
    if not RemoteCppListFilesCommand.owns_view(view):
      return None
    if command_name == 'insert' and args['characters'] == '\n':
      for reg in view.sel():
        if reg.empty():
          line = view.line(reg)
          path = view.substr(line).strip()
          if not self._is_valid(path):
            continue
          args = self._file(view, path).to_args()
          return (RemoteCppOpenFileCommand.NAME, args)
    if command_name == 'drag_select' and 'additive' in args:
      self._sel = self._get_sel(view)
    log('pre: cmd=[{cmd}] args=[{args}]'.format(
        cmd=command_name, args=str(args)), type='on_text_command')
    return None

  def on_post_text_command(self, view, command_name, args):
    if not RemoteCppListFilesCommand.owns_view(view):
      return
    if command_name == 'drag_select' and 'additive' in args:
      sel = self._get_sel(view)
      diff = sel.difference(self._sel)
      for point in diff:
        path = view.substr(view.line(point))
        if self._is_valid(path):
          args = self._file(view, path).to_args()
          view.run_command(RemoteCppOpenFileCommand.NAME, args)
          return
    log('post: cmd=[{cmd}] args=[{args}]'.format(
        cmd=command_name, args=str(args)), type='on_text_command')
    return

  def _is_valid(self, line):
    return len(line.strip()) > 0 and not line.strip().startswith('#')

  def _cwd(self, view):
    line = view.substr(view.line(0))
    cwd = line[len(CWD_PREFIX):]
    return cwd

  def _file(self, view, path):
    cwd = self._cwd(view)
    file = File(cwd=cwd, path=path)
    return file

  def _get_sel(self, view):
    sel = set()
    for reg in view.sel():
      if reg.empty():
        sel.add(reg.a)
    return sel



##############################################################
# Sublime Commands
##############################################################

class Commands(object):
  def __init__(self):
    raise Exception('Not to be instantiated.')

  @staticmethod
  def append_text(view, text):
    view.run_command(RemoteCppAppendTextCommand.NAME, { 'text': text })


class HelloRuiCommand(sublime_plugin.TextCommand):
  NAME = 'hello_rui'

  def run(self, edit):
    log('HELLO RUI!!!!!!!!!!')


class RemoteCppRefreshCache(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_refresh_cache'

  def run(self, edit):
    runnable = lambda : self._run_in_the_background(self.view.window())
    thread_pool.run(runnable)

  def _run_in_the_background(self, window):
    files = []
    roots = set()
    for view in window.views():
      file = state.file(view.id())
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
    log('Finished deleting the cache successfully.')


class RemoteCppOpenFileCommand(sublime_plugin.TextCommand):
    NAME = 'remote_cpp_open_file'

    def run(self, edit, **args):
      file = File(**args)
      log("Opening => " + file.remote_path())
      remote_path = file.remote_path()
      local_path = file.local_path()
      window = sublime.active_window()
      # Shortcut if the file already exists.
      if os.path.isfile(local_path):
        self._open_file(window, file)
        return
      # Otherwise let's copy the file locally.
      runnable = lambda : self._run_in_the_background(
          window,
          file)
      thread_pool.run(runnable)

    def _run_in_the_background(self, window, file):
      download_file(file)
      self._open_file(window, file)

    def _open_file(self, window, file):
      view = window.open_file(file.local_path())
      state.set_file(view.id(), file)


class RemoteCppListFilesCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_list_files'
  WINDOW_PREFIX = 'ListFiles'

  def run(self, edit):
    view = sublime.active_window().new_file()
    view.set_name('{prefix} [{time}]'.format(
        prefix=RemoteCppListFilesCommand.WINDOW_PREFIX,
        time=time_str()))
    view.set_read_only(True)
    view.set_scratch(True)
    def run_in_the_background():
      log("Running in the background!!!")
      try:
        # TODO(ruibm): Check if this works with find in BSD and Linux.
        files = run_cmd((
            s_ssh(), '-p {0}'.format(s_ssh_port()), 'localhost',
            'cd {0}; '.format(s_cwd()) +
            'find . -maxdepth 5 -not -path \'*/\\.*\' -type f -print' +
            ' | sed -e \'s|^./||\''))
        files = sorted(files.strip().split('\n'), key=lambda s: s.lower())
        state.set_list(view.window().id(), files)
        files = '\n'.join(files)
        files = '{cwd_prefix}{cwd}\n\n{files}'.format(
            cwd_prefix=CWD_PREFIX,
            cwd=s_cwd(),
            files=files.lstrip())
        Commands.append_text(view, files)
      except Exception as e:
        msg = 'Error listing files: [{exception}].'.format(exception=e)
        log_exception(msg)
        Commands.append_text(view, msg)
      log("Finished running in the background!!!")
    thread_pool.run(run_in_the_background)

  @staticmethod
  def owns_view(view):
    return view.name().startswith(RemoteCppListFilesCommand.WINDOW_PREFIX)


class RemoteCppAppendTextCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_append_text'

  def run(self, edit, text='NO_TEXT_PROVIDED'):
    view = self.view
    view.set_read_only(False)
    view.insert(edit, 0, text)
    view.set_read_only(True)


##############################################################
# Static Methods
##############################################################

def download_file(file):
  log('Downloading the file [{file}]...'.format(file=file.remote_path()))
  run_cmd((
      'scp',
      '-P', str(s_ssh_port()),
      'localhost:{path}'.format(path=file.remote_path()),
      '{path}'.format(path=file.local_path())
  ))
  log('Done downloading the file into [{file}].'.format(file=file.local_path()))

def run_cmd(cmd_list):
  proc = subprocess.Popen(cmd_list,
      stdin=None,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      bufsize=1)
  out = proc.stdout.read().decode('utf-8')
  proc.wait()
  if proc.returncode != 0:
    raise Exception('Problems running cmd [{cmd}]'.format(
        cmd=' '.join(cmd_list)
    ))
  return out

def time_str():
  return datetime.datetime.now().strftime("%Y-%m-%d_%Hh%Mm%Ss")

def log(msg, type=''):
  if type in LOG_TYPES:
    print(msg)

def log_exception(msg):
  print(msg + '\n + ' + traceback.format_exc())

def md5(msg):
  m = hashlib.md5()
  m.update(msg.encode())
  return m.hexdigest()


##############################################################
# Classes
##############################################################

class File(object):
  PLUGIN_DIR = 'RemoteCpp'

  def __init__(self, cwd, path):
    self.cwd = cwd
    self.path = path

  def remote_path(self):
    return os.path.join(self.cwd, self.path)

  def local_path(self, call_makedirs=True):
    local_path = os.path.join(
        sublime.cache_path(),
        self.PLUGIN_DIR,
        md5(self.cwd),
        self.path)
    directory = os.path.dirname(local_path)
    if call_makedirs and not os.path.isdir(directory):
      log('Creating directory [{0}]...'.format(directory))
      os.makedirs(directory)
    return local_path

  def local_root(self):
    local_root = os.path.join(
        sublime.cache_path(),
        self.PLUGIN_DIR,
        md5(self.cwd))
    return local_root

  def to_args(self):
    return {
      'cwd': self.cwd,
      'path': self.path,
    }


class ThreadPool(object):
  def __init__(self, number_threads):
    self._lock = threading.Lock()
    self._tasks_running = 0

  def run(self, callback):
    with self._lock:
      self._tasks_running += 1
      if self._tasks_running == 1:
        ProgressAnimation(self.tasks_running).start()
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


class PluginState(object):
  STATE_FILE = 'RemoteCpp.PluginState.json'

  # List of open remotes files => map<view_id, File.to_args()>
  FILES = 'remote_files'
  # File lists => map<window_id, files_list>
  LISTS = 'file_list'

  def __init__(self, state=dict()):
    self.state = state
    if not self.FILES in self.state:
      self.state[self.FILES] = {}
    if not self.LISTS in self.state:
      self.state[self.LISTS] = {}

  def file(self, view_id):
    view_id = int(view_id)
    if view_id in self.state[self.FILES]:
      return File(**self.state[self.FILES][view_id])
    return None

  def set_file(self, view_id, file):
    self.state[self.FILES][int(view_id)] = file.to_args()

  def list(self, window_id):
    window_id = int(window_id)
    if window_id in self.state[self.LISTS]:
      return self.state[self.LISTS][window_id]
    return None

  def set_list(self, window_id, file_list):
    self.state[self.LISTS][int(window_id)] = file_list

  @staticmethod
  def _path():
    path = os.path.join(
      sublime.cache_path(),
      File.PLUGIN_DIR,
      PluginState.STATE_FILE)
    return path

  def gc(self):
    all_views = set()
    all_windows = set()
    for w in sublime.windows():
      all_windows.add(w.id())
      for v in w.views():
        all_views.add(v.id())
    for view_id in tuple(self.state[self.FILES].keys()):
      if not view_id in all_views:
        del self.state[self.FILES][view_id]
    for window_id in tuple(self.state[self.LISTS].keys()):
      if not window_id in all_windows:
        del self.state[self.LISTS][window_id]

  def load(self):
    path = PluginState._path()
    if not os.path.isfile(path):
      return
    log('Reading PluginState from [{0}]...'.format(path))
    with open(path, 'r') as fp:
      self.state = json.load(fp)
    self.gc()
    log('Reloaded successfully the PluginState.')

  def save(self):
    raw = json.dumps(self.state, indent=2)
    path = self._path()
    log('Writing PluginState to [{0}]...'.format(path))
    dir = os.path.dirname(path)
    if not os.path.isdir(dir):
      os.makedirs(dir)
    with open(path, 'w') as fp:
      fp.write(raw)
    log('Successully wrote {0} bytes of PluginState.'.format(len(raw)))

