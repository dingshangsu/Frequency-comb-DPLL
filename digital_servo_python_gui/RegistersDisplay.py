from PyQt5 import QtGui, Qt, QtCore, QtWidgets
import time, sys
from collections import namedtuple, deque
from enum import Enum, auto
from functools import partial
import RegistersDisplayDefinitions

RegisterInfo = namedtuple('RegisterInfo', ['subsystem', 'display_name', 'addr', 'show', 'formatting_func'])


class EventTypes(Enum):
    read = auto()
    written = auto()
    changed = auto()

RegEventInfo = namedtuple('RegEventInfo', ('field_name', 'event_type'))

class RegisterState():
    def __init__(self, reg_definitions):
        self.mark_timeouts = {  # how long to keep register marked in a different color after each event has happened
            EventTypes.read: 0.1,
            EventTypes.written: 1,
            EventTypes.changed: 1,
        }

        self.unmark_queue = dict() # keys are (field_name, event_type) tuples, and values is the expiration time

        self.reg_definitions = reg_definitions
        # build addr -> field_name lookup table for faster lookup at runtime
        self.name_from_addr = {reg_info.addr: fieldname for (fieldname, reg_info) in self.reg_definitions.items()}

        # start with unknown register values
        self.reg_values = {key:None for key in self.reg_definitions.keys()}

        # empty callbacks for now:
        def this_func_does_nothing(*args, **kwargs):
            pass
        self.mark_reg_callback = this_func_does_nothing
        self.reg_changed_callback = this_func_does_nothing

    def setMarkCallback(self, callback):
        """ This callback will get called with individual register info
        and event info whenever the GUI needs to mark or unmark the event as recent.
        Prototype should look like:
        def callback(field_name, event_type, bMark)
        (use partial if you need to transfer state) """
        self.mark_reg_callback = callback

    def setRegUpdateCallback(self, callback):
        """ This callback will get called with individual register
        name and value whenever the value changes.
        Prototype should look like:
        def callback(field_name, value)
        (use partial if you need to transfer state) """
        self.reg_changed_callback = callback

    def timerColorCoding(self):
        """ TODO: update the color coding status of the registers that have been
        read/written/changed long enough ago. """
        current_time = time.perf_counter()

        list_del = []
        for reg_info, unmark_time in self.unmark_queue.items():
            # check if this register is not yet ready to get unmarked:
            if unmark_time > current_time:
                continue
            # time to unmark this register.
            self.mark_reg_callback(reg_info.field_name, reg_info.event_type, bMark=False)

            # remove item from the queue once we are done looping through
            list_del.append(reg_info)

        # do the deletion after we are done iterating through the deque itself (otherwise it throws an error)
        for key in list_del:
            self.unmark_queue.pop(key)

    def reg_event(self, addr=None, field_names=None, event_type=None, values=None):
        """ call this when one or more registers has had an event (read, written, changed).
        Specify either the address(es) or the field name of the register.
        Specify a list of addresses or names if there are multiple regs
        being reported at the same time.

        event_type must be one of the valid values
        in the EventTypes enum 

        values must be the register(s) new value(s)"""

        # first need to figure out if arguments use addr or field_names:
        if addr is not None:
            # must perform lookups from addresses to names:
            if not isinstance(addr, list):
                field_names_internal = self.name_from_addr[addr]
            else:
                field_names_internal = [self.name_from_addr[x] for x in addr]
        else:
            field_names_internal = field_names

        # now do the actual work:
        map_if_list(partial(self._reg_event_single, event_type), field_names_internal, values)

    def _reg_event_single(self, event_type, field_name, value):
        """ Called from reg_event, only handles one register at a time. """

        self._reg_event_add_to_queue(event_type, field_name)

        # update the GUI only if this value is actually different than it was last time:
        last_value = self.reg_values[field_name]
        if value != last_value:
            self.reg_changed_callback(field_name, value)
            self.reg_values[field_name] = value # save new state

            # add an "updated" event to the queue:
            self._reg_event_add_to_queue(event_type=EventTypes.changed, field_name=field_name)

    def _reg_event_add_to_queue(self, event_type, field_name):
        # mark this register as read/written/updated at current time (change color)
        reg_info = RegEventInfo(field_name, event_type)
        self.mark_reg_callback(field_name, event_type, bMark=True)
        # schedule the expiration of this marking at a later time:
        unmark_time = time.perf_counter()+self.mark_timeouts[event_type]
        self.unmark_queue[reg_info] = unmark_time

# End class RegisterState

def map_if_list(func, *args):
    if isinstance(args[0], list):
        return map(func, *args)
    else:
        # scalar case:
        return func(*args)

class RegistersDisplayWidget(Qt.QWidget):
    def __init__(self, parent, reg_definitions):
        super().__init__(parent)
        # uic.loadUi("RegistersDisplayWidget.ui", self) # no need for this at the moment since the UI is super simple
        self.initUI(reg_definitions)

    def initUI(self, reg_definitions):
        self.view = Qt.QTreeView(self)
        self.view.setSelectionBehavior(Qt.QAbstractItemView.SelectRows)
        self.model = Qt.QStandardItemModel()
        self.model.setHorizontalHeaderLabels(['Register', 'Addr', 'Value', 'R', 'W'])
        self.view.setModel(self.model)
        self.view.setUniformRowHeights(True)
        self.view.setAlternatingRowColors(True)
        self.view.setColumnWidth(3, 10)
        self.view.setColumnWidth(4, 10)

        # create brushes for various background colors:
        self.brushes = {}
        self.brushes['red'] = Qt.QBrush(Qt.QColor(255, 0, 0))
        self.brushes['yellow'] = Qt.QBrush(Qt.QColor(255, 255, 0))
        self.brushes['green'] = Qt.QBrush(Qt.QColor(0, 165, 114))

        self._populate_model(reg_definitions)

        hbox = Qt.QHBoxLayout(self)
        hbox.setContentsMargins(0, 0, 0, 0)
        hbox.addWidget(self.view) # TODO: multiple TV next to each other? How to handle the split model/items ?
        self.setLayout(hbox)

        self.setWindowTitle('Registers')

    def _get_item_parent_from_subsystem(self, subsystem):
        """ This either creates an data item if subsystem is not represented yet
        in the model, or just finds the pre-existing element.
        It maintains a dict of the subsystems in order to do this task. """

        try:
            return_value = self.subsystems[subsystem]
        except KeyError:
            # create that node
            accumulated_name = ''
            all_names = subsystem.split('/')
            names_to = '/'.join(all_names[:-1])
            lower_level_name = all_names[-1]

            child = Qt.QStandardItem(lower_level_name)
            self.subsystems[subsystem] = child

            parent = self._get_item_parent_from_subsystem(names_to)
            parent.appendRow(child)

            index = self.model.indexFromItem(child)
            self.view.expand(index)

            return_value = child

        return return_value

    def _populate_model(self, reg_definitions):
        self.reg_definitions = reg_definitions

        self.subsystems = dict()
        self.subsystems[''] = self.model # root item is the model itself

        # first determine hierarchy by looking at the subsystem fields
        # nested subsystems can be specified by separating names by "/",
        # example: "pll/demodulator/oscillator"
        # ['subsystem', 'display_name', 'addr', 'show', 'formatting_func']
        for (field_name, reg_info) in self.reg_definitions.items():
            child1 = Qt.QStandardItem(field_name)
            child2 = Qt.QStandardItem(field_name)
            child3 = Qt.QStandardItem('0')
            child4 = Qt.QStandardItem('')
            child5 = Qt.QStandardItem('')
            child4.setBackground(self.brushes['green'])
            child5.setBackground(self.brushes['red'])
            parent = self._get_item_parent_from_subsystem(reg_info.subsystem) # this creates the subsystem item if it doesn't exist
            parent.appendRow([child1, child2, child3, child4, child5])


            # # span container columns (what does that mean??)
            # view.setFirstColumnSpanned(i, view.rootIndex(), True)


################################################################
## Main code, for testing the widget with no other container
################################################################
def main():
    # Qt4:
    # app = QtGui.QApplication(sys.argv) # Qt4
    app = QtWidgets.QApplication(sys.argv) # Qt5

    reg_definitions = RegistersDisplayDefinitions.reg_definitions
    GUI = RegistersDisplayWidget(None, reg_definitions)
    # GUI.show()
    GUI.showMaximized()
    app.exec_()
    del GUI
    
    
if __name__ == '__main__':
    main()



print("TODO: CLICKING ON A REGISTER OPENS A SUMMARY WINDOW: VALUE, and all other parameters (copy-pasteable)")
