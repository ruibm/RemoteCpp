#!/usr/bin/python

import sublime
import sublime_plugin

import datetime
import subprocess
import sys
import threading

from subprocess import PIPE


##############################################################
# Scripts Globals
##############################################################

thread_pool = None

##############################################################
# Sublime EventListeners
##############################################################

def plugin_loaded():
  global thread_pool
  thread_pool = ThreadPool(1)
  print("RemoteCpp has loaded successfully! :)")


##############################################################
# Sublime Commands
##############################################################

class Commands(object):
  def __init__(self):
    raise Exception('Not to be instantiated.')

  @staticmethod
  def append_text(view, text):
    view.run_command('remote_cpp_append_text', { 'text': text })


class RemoteCppListFilesCommand(sublime_plugin.TextCommand):
  def run(self, edit):
    view = sublime.active_window().new_file()

    view.set_name('ListFiles [{time}]'.format(time=time_str()))
    view.set_read_only(True)
    view.set_scratch(True)
    def run_in_the_background():
      print("Running in the background!!!")
      try:
        files = run_cmd((
            'ssh', '-p', '8888', 'localhost',
            'find . -maxdepth 3 -type f -printf "%P\n"'))
        files = '\n'.join(sorted(files.split('\n'), key=lambda s: s.lower()))
        Commands.append_text(view, files.lstrip())
      except Exception as e:
        Commands.append_text(view, 'Error listing files: [{exception}].'.format(
          exception=e))
      print("Finished running in the background!!!")
    thread_pool.run(run_in_the_background)

class RemoteCppAppendTextCommand(sublime_plugin.TextCommand):
  def run(self, edit, text='NO_TEXT_PROVIDED'):
    view = self.view
    view.set_read_only(False)
    view.insert(edit, 0, text)
    view.set_read_only(True)


##############################################################
# Static Methods
##############################################################

def run_cmd(cmd_list):
  proc = subprocess.Popen(cmd_list,
      stdin=None,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      bufsize=1)
  out = proc.stdout.read().decode('utf-8')
  proc.wait()
  if proc.returncode != 0:
    raise Exception('Problems running cmd ' + ' '.join(cmd_list))
  # print(out)
  return out

def time_str():
  return datetime.datetime.now().strftime("%Y-%m-%d_%Hh%Mm%Ss")


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
        ProgressAnimation(self.are_tasks_running).start()
    def callback_wrapper():
      try:
        callback()
      except Exception as e:
        print('Background task failed with exception: [{exception}]'.format(
            exception=e))
      with self._lock:
        self._tasks_running -= 1
    print('tasks running = ' + str(self._tasks_running))
    sublime.set_timeout_async(callback_wrapper, 0)

  def are_tasks_running(self):
    with self._lock:
      return self._tasks_running > 0

class ProgressAnimation(object):
  def __init__(self, should_continue_running_callback):
    self._len = 35  # Arbitrary value.
    self._pos = self._len
    self._continue_running = should_continue_running_callback

  def start(self):
    self._pos = self._len
    self._schedule_next_cycle()

  def _schedule_next_cycle(self):
    sublime.set_timeout(self._run_progress_animation, 25)

  def _run_progress_animation(self):
    try:
      if self._continue_running():
        self._draw_animation()
        self._pos = (self._pos + 1) % (self._len * 2)
        self._schedule_next_cycle()
      else:
        sublime.status_message('')
    except Exception as e:
      print('Exception running animation: ' + e)
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
    sublime.status_message(msg)



"[.....><>......]"
"[.....<......]"
"[.....-......]"

