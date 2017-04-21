# Copyright 2007 Benjamin M. Schwartz
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty ofwa
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

import dbus
import gtk
import gtk.gdk
import gobject
import dobject
import logging
import time
import thread
import threading
import locale
import pango
from gettext import gettext
import powerd

suspend = powerd.Suspend()

class WatchModel():
    STATE_PAUSED = 1
    STATE_RUNNING = 2
    
    RUN_EVENT = 1
    PAUSE_EVENT = 2
    RESET_EVENT = 3
    
    _default_basestate = (0.0, STATE_PAUSED)

    def _trans(self, s, pack):
        if pack:
            return dbus.Struct((dbus.Double(s[0]), dbus.Int32(s[1])), signature="di")
        else:
            return (float(s[0]), int(s[1]))

    def __init__(self, handler):
        self._logger = logging.getLogger('stopwatch.WatchModel')
        self._history = dobject.AddOnlySortedSet(handler, translator=self._trans)
        self._history_lock = threading.RLock()

        self._view_listener = None  #This must be done before _update_state
        
        handler2 = handler.copy("basestate")
        
        self._base_state = dobject.HighScore(handler2, WatchModel._default_basestate,
                     float("-inf"), self._trans, dobject.float_translator)
        
        self._state = ()
        self._update_state() #sets the state to the base_state
    
        self._base_state.register_listener(self._basestate_cb)
        self._history.register_listener(self._history_cb)
        
    def get_state(self):
        return self._state
    
    def get_last_update_time(self):
        if len(self._history) > 0:
            lastevent = self._history.last()
            return lastevent[0]
        else:
            return float("-inf")
        
    def reset(self, s, t):
        self._base_state.set_value(s, t)
        self._update_state()
    
    def _basestate_cb(self, v, s):
        self._update_state()
        self._trigger()
    
    def _history_cb(self, diffset):
        self._update_state()
        self._trigger()
    
    def add_event_from_view(self, ev):
        self._history_lock.acquire()
        if ev not in self._history:
            self._history.add(ev)
            self._update_state()
        self._history_lock.release()
        self._trigger()
        #We always trigger when an event is received from the UI.  Otherwise,
        #due to desynchronized clocks, it is possible to click Start/Stop
        # and produce an old event that is irrelevant.  This results in the
        # UI reaching an inconsistent state, with the button toggled off
        # but the clock still running.
        
    def _update_state(self):
        self._logger.debug("_update_state")
        L = len(self._history)
        init = self._base_state.get_value()
        timeval = init[0]
        s = init[1]
        #state machine
        for ev in self._history:
            event_time = ev[0]
            event_type = ev[1]
            if s == WatchModel.STATE_PAUSED:
                if event_type == WatchModel.RUN_EVENT:
                    s = WatchModel.STATE_RUNNING
                    timeval = event_time - timeval
                elif event_type == WatchModel.RESET_EVENT:
                    timeval = 0.0
            elif s == WatchModel.STATE_RUNNING:
                if event_type == WatchModel.RESET_EVENT:
                    timeval = event_time
                elif event_type == WatchModel.PAUSE_EVENT:
                    s = WatchModel.STATE_PAUSED
                    timeval = event_time - timeval

        return self._set_state((timeval, s))

    def is_running(self):
        return self._state[1] == WatchModel.STATE_RUNNING

    def _set_state(self, q):
        self._logger.debug("_set_state")
        if self._state != q:
            self._state = q
            return True
        else:
            return False
    
    def register_view_listener(self, L):
        self._logger.debug("register_view_listener ")
        self._view_listener = L
        self._trigger()

    def _trigger(self):
        if self._view_listener is not None:
            thread.start_new_thread(self._view_listener, (self._state,))

class OneWatchView():
    def __init__(self, mywatch, myname, mymarks, timer):
        self._logger = logging.getLogger('stopwatch.OneWatchView')
        self._watch_model = mywatch
        self._name_model = myname
        self._marks_model = mymarks
        self._timer = timer
        
        self._update_lock = threading.Lock()
        self._state = None
        self._timeval = 0

        self._offset = self._timer.get_offset()
        
        self._name = gtk.Entry()
        self._name_changed_handler = self._name.connect('changed', self._name_cb)
        self._name_lock = threading.Lock()
        self._name_model.register_listener(self._update_name_cb)
        
        check = gtk.Image()
        check.set_from_file('check.svg')
        self._run_button = gtk.ToggleButton(gettext("Start/Stop"))
        self._run_button.set_image(check)
        self._run_button.props.focus_on_click = False        
        self._run_handler = self._run_button.connect('clicked', self._run_cb)
        self._run_button_lock = threading.Lock()

        circle = gtk.Image()
        circle.set_from_file('circle.svg')
        self._reset_button = gtk.Button(gettext("Zero"))
        self._reset_button.set_image(circle)
        self._reset_button.props.focus_on_click = False
        self._reset_button.connect('clicked', self._reset_cb)
        
        x = gtk.Image()
        x.set_from_file('x.svg')
        self._mark_button = gtk.Button(gettext("Mark"))
        self._mark_button.set_image(x)
        self._mark_button.props.focus_on_click = False
        self._mark_button.connect('clicked', self._mark_cb)
        
        timefont = pango.FontDescription()
        timefont.set_family("monospace")
        timefont.set_size(pango.SCALE*14)
        self._time_label = gtk.Label(self._format(0))
        self._time_label.modify_font(timefont)
        self._time_label.set_single_line_mode(True)
        self._time_label.set_selectable(True)
        self._time_label.set_width_chars(10)
        self._time_label.set_alignment(1,0.5) #justify right
        self._time_label.set_padding(6,0)
        eb = gtk.EventBox()
        eb.add(self._time_label)
        eb.modify_bg(gtk.STATE_NORMAL, gtk.gdk.color_parse("white"))
        
        self._should_update = threading.Event()
        self._is_visible = threading.Event()
        self._is_visible.set()
        self._update_lock = threading.Lock()
        self._label_lock = threading.Lock()

        self.box = gtk.HBox()
        self.box.pack_start(self._name, padding=6)
        self.box.pack_start(self._run_button, expand=False)
        self.box.pack_start(self._reset_button, expand=False)
        self.box.pack_start(self._mark_button, expand=False)
        self.box.pack_end(eb, expand=False, padding=6)
        
        markfont = pango.FontDescription()
        markfont.set_family("monospace")
        markfont.set_size(pango.SCALE*10)
        self._marks_label = gtk.Label()
        self._marks_label.modify_font(markfont)
        self._marks_label.set_single_line_mode(True)
        self._marks_label.set_selectable(True)
        self._marks_label.set_alignment(0, 0.5) #justify left
        self._marks_label.set_padding(6,0)
        self._marks_model.register_listener(self._update_marks)
        eb2 = gtk.EventBox()
        eb2.add(self._marks_label)
        eb2.modify_bg(gtk.STATE_NORMAL, gtk.gdk.color_parse("white"))
        
        filler0 = gtk.VBox()
        filler0.pack_start(self.box, expand=False, fill=False)
        filler0.pack_start(eb2, expand=False, fill=False)
        
        filler = gtk.VBox()
        filler.pack_start(filler0, expand=True, fill=False)
        
        self.backbox = gtk.EventBox()
        self.backbox.add(filler)
        self._black = gtk.gdk.color_parse("black")
        self._gray = gtk.gdk.Color(256*192, 256*192, 256*192)
        
        self.display = gtk.EventBox()
        self.display.add(self.backbox)
        #self.display.set_above_child(True)
        self.display.props.can_focus = True
        self.display.connect('focus-in-event', self._got_focus_cb)
        self.display.connect('focus-out-event', self._lost_focus_cb)
        self.display.add_events(gtk.gdk.ALL_EVENTS_MASK)
        self.display.connect('key-press-event', self._keypress_cb)
        #self.display.connect('key-release-event', self._keyrelease_cb)
        
        self._watch_model.register_view_listener(self.update_state)
        
        thread.start_new_thread(self._start_running, ())
        
    def update_state(self, q):
        self._logger.debug("update_state: "+str(q))
        self._update_lock.acquire()
        self._logger.debug("acquired update_lock")
        self._state = q[1]
        self._offset = self._timer.get_offset()
        if self._state == WatchModel.STATE_RUNNING:
            self._timeval = q[0]
            self._set_run_button_active(True)
            self._should_update.set()
        else:
            self._set_run_button_active(False)
            self._should_update.clear()
            self._label_lock.acquire()
            self._timeval = q[0]
            ev = threading.Event()
            gobject.idle_add(self._update_label, self._format(self._timeval), ev)
            ev.wait()
            self._label_lock.release()
        self._update_lock.release()
    
    def _update_name_cb(self, name):
        self._logger.debug("_update_name_cb " + name)
        thread.start_new_thread(self.update_name, (name,))
    
    def update_name(self, name):
        self._logger.debug("update_name " + name)
        self._name_lock.acquire()
        self._name.set_editable(False)
        self._name.handler_block(self._name_changed_handler)
        ev = threading.Event()
        gobject.idle_add(self._set_name, name, ev)
        ev.wait()
        self._name.handler_unblock(self._name_changed_handler)
        self._name.set_editable(True)
        self._name_lock.release()
            
    def _set_name(self, name, ev):
        self._name.set_text(name)
        ev.set()
        return False
        
    def _format(self, t):
        return locale.format('%.2f', max(0,t))
    
    def _update_label(self, string, ev):
        self._time_label.set_text(string)
        ev.set()
        return False
    
    def _start_running(self):
        self._logger.debug("_start_running")
        ev = threading.Event()
        while True:
            self._should_update.wait()
            self._is_visible.wait()
            self._label_lock.acquire()
            if self._should_update.isSet() and self._is_visible.isSet():
                s = self._format(time.time() + self._timer.offset - self._timeval)
                ev.clear()
                gobject.idle_add(self._update_label, s, ev)
                ev.wait()
                time.sleep(0.07)
            self._label_lock.release()
    
    def _run_cb(self, widget):
        t = time.time()
        self._logger.debug("run button pressed: " + str(t))
        if self._run_button.get_active(): #button has _just_ been set active
            action = WatchModel.RUN_EVENT
            suspend.inhibit()
        else:
            action = WatchModel.PAUSE_EVENT
            suspend.uninhibit()
        self._watch_model.add_event_from_view((self._timer.get_offset() + t, action))
        return True
        
    def _set_run_button_active(self, v):
        self._run_button_lock.acquire()
        self._run_button.handler_block(self._run_handler)
        self._run_button.set_active(v)
        self._run_button.handler_unblock(self._run_handler)
        self._run_button_lock.release()
            
    def _reset_cb(self, widget):
        t = time.time()
        self._logger.debug("reset button pressed: " + str(t))
        self._watch_model.add_event_from_view((self._timer.get_offset() + t, WatchModel.RESET_EVENT))
        return True
    
    def _mark_cb(self, widget):
        t = time.time() + self._offset
        self._logger.debug("mark button pressed: " + str(t))
        s = self._state
        tval = self._timeval
        if s == WatchModel.STATE_RUNNING:
            self._marks_model.add(max(0.0, t - tval))
        elif s == WatchModel.STATE_PAUSED:
            self._marks_model.add(tval)
        self._update_marks()
    
    def _update_marks(self, diffset=None):
        L = list(self._marks_model)
        L.sort()
        s = [self._format(num) for num in L]
        p = " ".join(s)
        self._marks_label.set_text(p)
    
    def _name_cb(self, widget):
        self._name_model.set_value(widget.get_text())
        return True
        
    def pause(self):
        self._logger.debug("pause")
        self._is_visible.clear()
    
    def resume(self):
        self._logger.debug("resume")
        self._is_visible.set()
    
    def refresh(self):
        """Make sure display is up-to-date"""
        self._update_name_cb(self._name_model.get_value())
        thread.start_new_thread(self.update_state, (self._watch_model.get_state(),))
        self._update_marks()
    
    def _got_focus_cb(self, widget, event):
        self._logger.debug("got focus")
        self.backbox.modify_bg(gtk.STATE_NORMAL, self._black)
        self._name.modify_bg(gtk.STATE_NORMAL, self._black)
        return True
    
    def _lost_focus_cb(self, widget, event):
        self._logger.debug("lost focus")
        self.backbox.modify_bg(gtk.STATE_NORMAL, self._gray)
        self._name.modify_bg(gtk.STATE_NORMAL, self._gray)
        return True
    
    # KP_End == check gamekey = 65436
    # KP_Page_Down == X gamekey = 65435
    # KP_Home == box gamekey = 65429
    # KP_Page_Up == O gamekey = 65434
    def _keypress_cb(self, widget, event):
        self._logger.debug("key press: " + gtk.gdk.keyval_name(event.keyval)+ " " + str(event.keyval))
        if event.keyval == 65436:
            self._run_button.clicked()
        elif event.keyval == 65434:
            self._reset_button.clicked()
        elif event.keyval == 65435:
            self._mark_button.clicked()
        return False
            
class GUIView():
    NUM_WATCHES = 9

    def __init__(self, tubebox, timer):
        self.timer = timer
        self._views = []
        self._names = []
        self._watches = []
        self._markers = []
        for i in xrange(GUIView.NUM_WATCHES):
            name_handler = dobject.UnorderedHandler("name"+str(i), tubebox)
            name_model = dobject.Latest(name_handler, gettext("Stopwatch") + " " + locale.str(i+1), time_handler=timer, translator=dobject.string_translator)
            self._names.append(name_model)
            watch_handler = dobject.UnorderedHandler("watch"+str(i), tubebox)
            watch_model = WatchModel(watch_handler)
            self._watches.append(watch_model)
            marks_handler = dobject.UnorderedHandler("marks"+str(i), tubebox)
            marks_model = dobject.AddOnlySet(marks_handler, translator = dobject.float_translator)
            self._markers.append(marks_model)
            watch_view = OneWatchView(watch_model, name_model, marks_model, timer)
            self._views.append(watch_view)
            
        self.display = gtk.VBox()
        for x in self._views:
            self.display.pack_start(x.display, expand=True, fill=True)
        
        self._pause_lock = threading.Lock()
    
    def get_names(self):
        return [n.get_value() for n in self._names]
    
    def set_names(self, namestate):
        for i in xrange(GUIView.NUM_WATCHES):
            self._names[i].set_value(namestate[i])
    
    def get_state(self):
        return [(w.get_state(), w.get_last_update_time()) for w in self._watches]
        
    def set_state(self,states):
        for i in xrange(GUIView.NUM_WATCHES):
            self._watches[i].reset(states[i][0], states[i][1])
            if self._watches[i].is_running():
                suspend.inhibit()
    
    def get_marks(self):
        return [list(m) for m in self._markers]
    
    def set_marks(self, marks):
        for i in xrange(GUIView.NUM_WATCHES):
            self._markers[i].update(marks[i])
    
    def get_all(self):
        return (self.timer.get_offset(), self.get_names(), self.get_state(), self.get_marks())
    
    def set_all(self, q):
        self.timer.set_offset(q[0])
        self.set_names(q[1])
        self.set_state(q[2])
        self.set_marks(q[3])
        for v in self._views:
            v.refresh()
    
    def pause(self):
        self._pause_lock.acquire()
        for w in self._views:
            w.pause()
        self._pause_lock.release()
    
    def resume(self):
        self._pause_lock.acquire()
        for w in self._views:
            w.resume()
        self._pause_lock.release()
