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
CPP_EXTENSIONS = set([
    '.c',
    '.cpp',
    '.h',
    '.cc',
])
FILE_LIST_PREAMBLE = '''
# Press 'Enter' on the selected remote file to open it.
'''.lstrip()


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
  log('RemoteCpp has loaded successfully! :)')


def plugin_unloaded():
  global STATE
  try:
    STATE.save()
  except:
    log_exception("Critical failure saving RemoteCpp plugin STATE.")


class PluginStateListener(sublime_plugin.EventListener):
  def on_close(self, view):
    # Let's not leak memory in the PluginState.
    STATE.gc()


class SaveFileListener(sublime_plugin.EventListener):
  def on_post_save(self, view):
    file = STATE.file(view.id())
    if file:
      log('Saving file: ' + str())
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


class ListFilesListener(sublime_plugin.EventListener):
  def on_text_command(self, view, command_name, args):
    if not RemoteCppListFilesCommand.owns_view(view):
      return None
    if command_name == 'insert' and args['characters'] == '\n':
      path = get_sel_line(view)
      if self._is_valid_path(path):
        args = File(cwd=s_cwd(), path=path).to_args()
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
        if self._is_valid_path(path):
          args = self._file(view, path).to_args()
          view.run_command(RemoteCppOpenFileCommand.NAME, args)
          return
    log('post: cmd=[{cmd}] args=[{args}]'.format(
        cmd=command_name, args=str(args)), type='on_text_command')
    return

  def _is_valid_path(self, line):
    return line != None and not line.strip().startswith('#')

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

  @staticmethod
  def open_file(view, args_to_create_file):
    view.run_command(RemoteCppOpenFileCommand.NAME, args_to_create_file)

  @staticmethod
  def toggle_header_implementation(view):
    view.run_command(RemoteCppToggleHeaderImplementationCommand.NAME)


class RemoteCppGotoInclude(sublime_plugin.TextCommand):
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
    file = STATE.file(view.id())
    file_list = STATE.list(window.id())
    log(str(file_list))
    if file_list == None:
      log('No file list found so requesting one...')
      def run_in_the_background():
        file_list = RemoteCppListFilesCommand.get_file_list(window)
        if type(file_list) == list:
          Commands.toggle_header_implementation(view)
      THREAD_POOL.run(run_in_the_background)
      return
    else:
      self._toggle(file, file_list)

  def _toggle(self, file, file_list):
    orig_path, orig_extension = os.path.splitext(file.path)
    sibblings = []
    for file_path in file_list:
      path, extension = os.path.splitext(file_path)
      log('path={0} extension={1} orig_path={2} orig_extension{3}'.format(
        path, extension,
        orig_path, orig_extension))
      if orig_extension != extension and orig_path == path:
        sibblings.append(file_path)
      sibbling_count = len(sibblings)
    if sibbling_count == 0:
      log('No sibbling files were found.')
      return
    elif sibbling_count == 1:
      toggle_file = File(cwd=file.cwd, path=sibblings[0])
      Commands.open_file(self.view, toggle_file.to_args())
    else:
      def on_select_callback(selected_index):
        if selected_index == -1:
          return
        toggle_file = File(cwd=file.cwd, path=sibblings[selected_index])
        Commands.open_file(self.view, toggle_file.to_args())
      self.view.window().show_quick_panel(
        items=sibblings,
        on_select=on_select_callback,
        selected_index=0)


class RemoteCppRefreshCache(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_refresh_cache'

  def run(self, edit):
    runnable = lambda : self._run_in_the_background(self.view.window())
    THREAD_POOL.run(runnable)

  def _run_in_the_background(self, window):
    files = []
    roots = set()
    for view in window.views():
      file = STATE.file(view.id())
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
      view = window.open_file(file.local_path())
      STATE.set_file(view.id(), file)


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
    THREAD_POOL.run(lambda: self._run_in_the_background(view))

  @staticmethod
  def _run_in_the_background(view):
    window = view.window()
    try:
      file_list = RemoteCppListFilesCommand.get_file_list(window)
      text = '\n'.join(file_list)
      text = '{preamble}{cwd_prefix}{cwd}\n\n{files}'.format(
          preamble=FILE_LIST_PREAMBLE,
          cwd_prefix=CWD_PREFIX,
          cwd=s_cwd(),
          files=text.lstrip())
    except Exception as e:
      log_exception('Error listing files')
      text = 'Error listing files: [{exception}].'.format(exception=e)
    Commands.append_text(view, text)

  @staticmethod
  def get_file_list(window):
    """ Returns a fresh file list """
    # TODO(ruibm): Check if this works with find in BSD and Linux.
    files = run_cmd((
        s_ssh(), '-p {0}'.format(s_ssh_port()), 'localhost',
        'cd {0}; '.format(s_cwd()) +
        'find . -maxdepth 5 -not -path \'*/\\.*\' -type f -print' +
        ' | sed -e \'s|^./||\''))
    files = sorted(files.strip().split('\n'), key=lambda s: s.lower())
    STATE.set_list(window.id(), files)
    return files

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
# RemoteCpp Functions
##############################################################

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
  file = STATE.file(view.id())
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
# RemoteCpp Classes
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
    view_id = str(view_id)
    if view_id in self.state[self.FILES]:
      return File(**self.state[self.FILES][view_id])
    return None

  def set_file(self, view_id, file):
    self.state[self.FILES][str(view_id)] = file.to_args()

  def list(self, window_id):
    window_id = str(window_id)
    if window_id in self.state[self.LISTS]:
      return self.state[self.LISTS][window_id]
    return None

  def set_list(self, window_id, file_list):
    self.state[self.LISTS][str(window_id)] = file_list

  def gc(self):
    all_views = set()
    all_windows = set()
    for w in sublime.windows():
      all_windows.add(str(w.id()))
      for v in w.views():
        all_views.add(str(v.id()))
    for view_id in tuple(self.state[self.FILES].keys()):
      if not view_id in all_views:
        log('Deleting file for view id [{0}].'.format(view_id))
        del self.state[self.FILES][view_id]
    for window_id in tuple(self.state[self.LISTS].keys()):
      if not window_id in all_windows:
        log('Deleting file_list for window id [{0}].'.format(window_id))
        del self.state[self.LISTS][window_id]

  def load(self):
    path = PluginState._path()
    if not os.path.isfile(path):
      return
    log('Reading PluginState from [{0}]...'.format(path))
    with open(path, 'r') as fp:
      self.state = json.load(fp)
    # self.gc()
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

  @staticmethod
  def _path():
    path = os.path.join(
      sublime.cache_path(),
      File.PLUGIN_DIR,
      PluginState.STATE_FILE)
    return path


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
