#!/usr/bin/python

import sublime
import sublime_plugin

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
  return _get_or_default('remote_cpp_build_cmd', 'buck build')

def s_find_filters():
  return _get_or_default('remote_cpp_find_filters',
      "-not -path '*buck-cache*' -not -path '*buck-out*'")

def s_grep_cmd():
  return _get_or_default('remote_cpp_grep_cmd', 'grep  -R -n \'{pattern}\' .')

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
  global THREAD_POOL, STATE
  try:
    STATE.save()
  except:
    log_exception("Critical failure saving RemoteCpp plugin STATE.")
  THREAD_POOL.close()


class PluginStateListener(sublime_plugin.EventListener):
  def on_close(self, view):
    # Let's not leak memory in the PluginState.
    # STATE.gc()  # NOT A GOOD IDEA AFTER ALL
    pass


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
    if RemoteCppListFilesCommand.owns_view(view) and \
        command_name == 'insert' and args['characters'] == '\n':
      path = get_sel_line(view)
      if self._is_valid_path(path):
        args = File(cwd=s_cwd(), path=path).to_args()
        return (RemoteCppOpenFileCommand.NAME, args)
    log('pre: cmd=[{cmd}] args=[{args}]'.format(
        cmd=command_name, args=str(args)), type='on_text_command')
    return None

  def _is_valid_path(self, line):
    return line != None and not line.strip().startswith('#')


class GotoBuildErrorListener(sublime_plugin.EventListener):
  def on_text_command(self, view, command_name, args):
    if RemoteCppBuildCommand.owns_view(view) and \
        command_name == 'insert' and args['characters'] == '\n':
      Commands.goto_build_error(view)
    return None


class GotoGrepMatchListener(sublime_plugin.EventListener):
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
  def append_text(view, text):
    ''' Appends the text and moves scroll to the bottom '''
    view.run_command(RemoteCppAppendTextCommand.NAME, { 'text': text })

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


class RemoteCppGotoGrepMatchCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_goto_grep_match'
  REGEX = re.compile('^(.+):(\d+):.+$')

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
        '[{time}] Grepping for [{text}] in [{cwd}]...\n\n'.format(
            cwd=s_cwd(),
            time=time_str(),
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
    ssh_cmd_async(arg_str, listener)


class RemoteCppMoveFileCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_move_file'

  def is_enabled(self):
    return None != STATE.file(self.view.id())

  def is_visible(self):
    return self.is_enabled()

  def run(self, edit):
    log('Moving file...')
    orig_file = STATE.file(self.view.id())
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
  REGEX = re.compile('^(.+):(\d+):(\d+):.+$')

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
    status = '[{time}] Building with cmd [{cmd}]...\n\n'.format(
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
    ssh_cmd_async(self._build_cmd(), listener)

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
    file = STATE.file(view.id())
    file_list = STATE.list(window.id())
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
      STATE.set_file(view.id(), file)

    def log(self, msg):
      log(msg, type=type(self).__name__)


class RemoteCppListFilesCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_list_files'
  VIEW_PREFIX = 'ListFiles'

  def run(self, edit):
    view = sublime.active_window().new_file()
    view.set_name('{prefix} [{time}]'.format(
        prefix=RemoteCppListFilesCommand.VIEW_PREFIX,
        time=time_str()))
    view.set_read_only(True)
    view.set_scratch(True)
    view.settings().set("word_wrap", "false")
    THREAD_POOL.run(lambda: self._run_in_the_background(view))

  @staticmethod
  def _run_in_the_background(view):
    window = view.window()
    try:
      start_secs = time.time()
      file_list = RemoteCppListFilesCommand.get_file_list(window)
      text = '\n'.join(file_list)
      end_secs = time.time()
      delta_millis = int((end_secs - start_secs) * 1000)
      preamble = FILE_LIST_PREAMBLE.format(
        cwd=s_cwd(),
        millis=delta_millis)
      text = '{preamble}\n\n{files}'.format(
          preamble=preamble,
          files=text.lstrip())
    except Exception as e:
      log_exception('Error listing files')
      text = 'Error listing files: [{exception}].'.format(exception=e)
    Commands.append_text(view, text)

  @staticmethod
  def get_file_list(window):
    """ Returns a fresh file list """
    # TODO(ruibm): Check if this works with find in BSD and Linux.
    log('rui => ' + s_find_filters())
    cmd_str = ('cd {cwd}; '
        'find . -maxdepth 5 -not -path \'*/\\.*\' {filters} -type f -print'
        ' | sed -e \'s|^./||\'').format(
        cwd=s_cwd(),
        filters=s_find_filters(),
    )
    files = run_cmd((
        s_ssh(), '-p {0}'.format(s_ssh_port()), 'localhost',
        cmd_str))
    files = sorted(files.strip().split('\n'), key=lambda s: s.lower())
    STATE.set_list(window.id(), files)
    return files

  @staticmethod
  def owns_view(view):
    return view.name().startswith(RemoteCppListFilesCommand.VIEW_PREFIX)


class RemoteCppAppendTextCommand(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_append_text'

  def run(self, edit, text='NO_TEXT_PROVIDED'):
    view = self.view
    view.set_read_only(False)
    view.insert(edit, view.size(), text)
    view.set_read_only(True)
    view.show(view.size() + 1)


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


class AppendToViewListener(CmdListener):
  def __init__(self, view):
    self._view = view
    self._start_secs = time.time()

  def on_stdout(self, line):
    Commands.append_text(self._view, line)

  def on_stderr(self, line):
    Commands.append_text(self._view, line)

  def on_exit(self, exit_code):
    if exit_code == 0:
      line = ('\n\n[{time}] Command finished successfully'
          ' in {millis} millis.\n').format(
          time=time_str(),
          millis=self._delta_millis(),
      )
    else:
      line = ('\n\n[{time}] Command failed with exit code [{code}] '
          'in {millis} millis.\n').format(
          code=exit_code,
          time=time_str(),
          millis=self._delta_millis(),
      )
    Commands.append_text(self._view, line)

  def _delta_millis(self):
    return int(1000 * (time.time() - self._start_secs))


class CaptureCmdListener(CmdListener):
  def __init__(self):
    self._out = []
    self._err = []
    self._exit_code = None

  def on_stdout(self, line):
    self._out.append(line)

  def on_stderr(self, line):
    self._err.append(line)

  def on_exit(self, exit_code):
    self._exit_code = exit_code

  def stdout(self):
    return ''.join(self._out)

  def stderr(self):
    return ''.join(self._err)

  def exit_code(self):
    return self._exit_code


class File(object):
  PLUGIN_DIR = 'RemoteCpp'

  def __init__(self, cwd, path, row=0, col=0):
    self.cwd = cwd
    self.path = path
    self.col = col
    self.row = row

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

def create_cmd_ssh_args(cmd_str):
  args = [ s_ssh(), '-p {0}'.format(s_ssh_port()), 'localhost', cmd_str ]
  return args

def ssh_cmd(cmd_str):
  args = create_cmd_ssh_args(cmd_str)
  return run_cmd(args)

def run_cmd(cmd_list):
  listener = CaptureCmdListener()
  run_cmd_async(cmd_list, listener)
  if listener.exit_code() != 0:
    raise Exception('Problems running cmd [{cmd}]'.format(
        cmd=' '.join(cmd_list)
    ))
  return listener.stdout()

def ssh_cmd_async(cmd_str, listener=CmdListener()):
  args = create_cmd_ssh_args(cmd_str)
  run_cmd_async(args, listener)

def run_cmd_async(cmd_list, listener=CmdListener()):
  proc = subprocess.Popen(cmd_list,
      stdin=None,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      bufsize=1)
  stdout = proc.stdout
  stderr = proc.stderr
  def read_fd(read_function, on_text_callback):
    text = read_function().decode('utf-8')
    on_text_callback(text)
    return len(text)
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
  return datetime.datetime.now().strftime("%Y-%m-%d_%Hh%Mm%Ss")

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
  file = STATE.file(view.id())
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
