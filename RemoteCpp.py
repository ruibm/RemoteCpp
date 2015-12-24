#!/usr/bin/python

import sublime
import sublime_plugin

import datetime
import os
import os.path
import shutil
import subprocess
import sys
import threading

from subprocess import PIPE

##############################################################
# Constants
##############################################################

LS_VIEW_TITLE_PREFIX = 'ListView'
LOG_TYPES = set(('',))


##############################################################
# Plugin Global State
##############################################################

# Initialised in plugin_loaded()
thread_pool = None

# key corresponds to View.id().
# value is a dictionary of properties.
file_state = {}


##############################################################
# Sublime EventListeners
##############################################################

def plugin_loaded():
  global thread_pool
  thread_pool = ThreadPool(1)
  log('RemoteCpp has loaded successfully! :)')


class SaveFileEventListener(sublime_plugin.EventListener):
  def on_post_save(self, view):
    if view.id() in file_state:
      state = file_state[view.id()]
      log('Saving file: ' + str())
      runnable = lambda : self._run_in_the_background(**state)
      thread_pool.run(runnable)

  def _run_in_the_background(self, remote_path, local_path):
    log('Saving file [{0}]...'.format(local_path))
    run_cmd((
        'scp',
        '-P', '8888',
        '{path}'.format(path=local_path),
        'localhost:{path}'.format(path=remote_path),
    ))
    log('Successsfully saved file [{0}].'.format(remote_path))


class ListFilesEventListener(sublime_plugin.EventListener):
  def on_text_command(self, view, command_name, args):
    if not RemoteCppListFilesCommand.owns_view(view):
      return None
    if command_name == 'insert' and args['characters'] == '\n':
      return self._create_cmd(view)
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
        cmd, args = self._create_cmd(view)
        view.run_command(RemoteCppOpenFileCommand.NAME, {'path': path})
        return
      log('Would open file: ' + str(args))
    log('post: cmd=[{cmd}] args=[{args}]'.format(
        cmd=command_name, args=str(args)), type='on_text_command')
    return

  def _get_sel(self, view):
    sel = set()
    for reg in view.sel():
      if reg.empty():
        sel.add(reg.a)
    return sel

  def _create_cmd(self, view):
    for reg in view.sel():
      if reg.empty():
        line = view.line(reg)
        path = view.substr(line).strip()
        return (RemoteCppOpenFileCommand.NAME, {'path': path})
    return None


##############################################################
# Sublime Commands
##############################################################

class Commands(object):
  def __init__(self):
    raise Exception('Not to be instantiated.')

  @staticmethod
  def append_text(view, text):
    view.run_command(RemoteCppAppendTextCommand.NAME, { 'text': text })


class RemoteCppRefreshCache(sublime_plugin.TextCommand):
  NAME = 'remote_cpp_refresh_cache'

  def run(self, edit):
    runnable = lambda : self._run_in_the_background(self.view.window())
    thread_pool.run(runnable)

  def _run_in_the_background(self, window):
    directory = get_local_path()
    log('Deleting local cache directory [{0}]...'.format(directory))
    shutil.rmtree(directory)
    os.makedirs(directory)
    for view in window.views():
      if view.id() in file_state:
        state = file_state[view.id()]
        log("Refreshing open file [{0}]...".format(state['local_path']))
        os.makedirs(os.path.dirname(state['local_path']))
        download_file(
          remote_path=state['remote_path'],
          local_path=state['local_path'])
    log('Finished deleting the cache successfully.')


class RemoteCppOpenFileCommand(sublime_plugin.TextCommand):
    NAME = 'remote_cpp_open_file'

    def run(self, edit, path):
      log("Opening => " + path)
      remote_path = path
      local_path = get_local_path(remote_path)
      window = sublime.active_window()
      # Shortcut if the file already exists.
      if os.path.isfile(local_path):
        self._open_file(window, remote_path, local_path)
        return
      # Otherwise let's copy the file locally.
      runnable = lambda : self._run_in_the_background(
          window,
          remote_path,
          local_path)
      thread_pool.run(runnable)

    def _run_in_the_background(self, window, remote_path, local_path):
      download_file(remote_path=remote_path, local_path=local_path)
      self._open_file(window, remote_path, local_path)

    def _open_file(self, window, remote_path, local_path):
      view = window.open_file(local_path)
      file_state[view.id()] = {
          'remote_path': remote_path,
          'local_path': local_path
      }


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
        files = run_cmd((
            'ssh', '-p', '8888', 'localhost',
            'find . -maxdepth 5 -not -path \'*/\\.*\' -type f -printf "%P\n"'))
        files = '\n'.join(sorted(files.split('\n'), key=lambda s: s.lower()))
        Commands.append_text(view, files.lstrip())
      except Exception as e:
        Commands.append_text(view, 'Error listing files: [{exception}].'.format(
          exception=e))
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

def download_file(remote_path, local_path):
  log('Downloading the file [{file}]...'.format(file=remote_path))
  run_cmd((
      'scp',
      '-P', '8888',
      'localhost:{path}'.format(path=remote_path),
      '{path}'.format(path=local_path)
  ))
  log('Done downloading the file into [{file}].'.format(file=local_path))

def get_local_path(remote_path=''):
  local_path = os.path.join(sublime.cache_path(), 'RemoteCpp', remote_path)
  directory = os.path.dirname(local_path)
  if not os.path.isdir(directory):
    log('Creating directory [{0}]...'.format(directory))
    os.makedirs(directory)
  return local_path

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
  # print(out)
  return out

def time_str():
  return datetime.datetime.now().strftime("%Y-%m-%d_%Hh%Mm%Ss")

def log(msg, type=''):
  if type in LOG_TYPES:
    print(msg)


##############################################################
# Classes
##############################################################

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
        log('Background task failed with exception: [{exception}]'.format(
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
      log('Exception running animation: ' + e)
      sublime.status_message('')

  # def _draw_animation(self):
  #   msg = list('[' + ' ' * self._len + ']')
  #   msg[abs(self._pos - self._len)] = '='
  #   msg = ''.join(msg)
  #   sublime.status_message(msg)

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

