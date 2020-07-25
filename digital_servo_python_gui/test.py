from PyQt5 import QtCore, QtGui, QtWidgets, uic
import time
import sys
from datetime import datetime
import os
import inspect
import time
import socket

import numpy as np
import pyqtgraph as pg

import SuperLaserLand_JD_RP
import RP_PLL # for CommsError
import channel_gui
from common import tictoc
import bin_conv

# Set a few global PyQtGraph settings before creating plots:
pg.setConfigOption('leftButtonPan', False)
pg.setConfigOption('background', 'w')
pg.setConfigOption('foreground', 'k')
pg.setConfigOption('antialias', True)

class perChannelSignalEmitter(QtCore.QObject):
    sig_new_adc_data = QtCore.pyqtSignal(np.ndarray, float, float)
    sig_new_iq_data  = QtCore.pyqtSignal(np.ndarray, float)
    sig_new_freq     = QtCore.pyqtSignal(float)
    sig_set_visible  = QtCore.pyqtSignal(bool)

class Test(QtWidgets.QWidget):
    """ Used as a top-level widget when testing """
    sig_new_settings = QtCore.pyqtSignal(dict)
    sig_phase_point  = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        
        self.bDisplayTiming = False
        self.tictoc_last = time.perf_counter()
        self.pfd_target_freq = 10e6
        self.ref_freq = 25e6 # this should be changeable via a signal
        self.num_iq_channels = 4 # set by the hardware...
        self.num_adc_channels = 2 # set by the hardware...
        self.config = dict()
        self.current_tab = 0

        self.perChannelEmitters = list()
        self.pts_settings = list()
        for channel_index in range(self.num_iq_channels):
            self.perChannelEmitters.append(perChannelSignalEmitter())
            self.pts_settings.append({"autorefresh": True, "pts_IQ": 100, "pts_ADC": 100})

        self.sl = SuperLaserLand_JD_RP.SuperLaserLand_JD_RP()
        self.sl.dev.OpenTCPConnection("192.168.2.34")
        self.sl.phaseReadoutDriver.startLogging()
        self.setupADCclock(bExternal=True, f_ref=25e6)
        # self.setupADCclock(bExternal=False, f_ref=200e6)

        self.fastTimer = QtCore.QTimer(self)
        self.fastTimer.timeout.connect(self.fastTimerEvent)

        self.fasterTimer = QtCore.QTimer(self)
        self.fasterTimer.timeout.connect(self.fasterTimerEvent)

        self.slowTimer = QtCore.QTimer(self)
        self.slowTimer.timeout.connect(self.slowTimerEvent)

    def emit_system_settings(self):
        # TODO: make this cleaner... update at the right times, etc
        d = dict()
        d["type"] = "system"
        d["fs"] = self.sl.fs
        d["n_bits_phase"] = self.sl.phaseReadoutDriver.n_bits_phase
        d["LPF_DECIM"] = self.sl.phaseReadoutDriver.LPF_DECIM
        d["n_cycles"] = self.sl.phaseReadoutDriver.n_cycles # number of decimated samples per integration time
        self.sig_new_settings.emit(d)

    def set_num_points(self, channel_id, d):
        """ Called when the GUI wants to change the number of points being read from the ADC and IQ sources """
        self.pts_settings[channel_id-1] = dict(d) # just make a local copy

    def setup_LO(self, d):
        """ Called when the GUI wants to change the LO settings
        d must contain the following fields:
        d["channel_id"]
        d["expected_freq"]
        d["target_if"]
        d["upper_sideband"]
        d["LO_pwr"]
        d["LO_enable"]
        """
        print("setup_LO: %s " % str(d))
        if d["upper_sideband"]:
            out_freq_target = d["expected_freq"] - d["target_if"]
            sign_str = '+'
        else:
            out_freq_target = d["expected_freq"] + d["target_if"]
            sign_str = '-'

        # TODO: input validation vs actual range accessible?
        print("setup_LO(): channel_id: ", d["channel_id"])
        a = self.sl.set_adf4351_freq(out_freq_target, self.ref_freq, self.pfd_target_freq, d["channel_id"], d["LO_pwr"], d["LO_enable"])
        D = 2**a.reg["RF_DIVIDER_SEL"]
        INT = a.reg["INT"]
        R = a.reg["R"]
        LO_actual = self.ref_freq * float(INT / (D*R))
        IF_actual = abs(d["expected_freq"] - LO_actual)

        result = dict(d) # also copies all the fields from d
        result["chosen_LO"] = LO_actual
        result["chosen_IF"] = IF_actual
        result["chosen_LO_text"] = "%.8f MHz = ref freq * %d/%d" % (LO_actual/1e6, INT, D*R)
        if d["upper_sideband"]:
            result["chosen_IF_text"] = "%.8f MHz = input - LO freq" % (IF_actual/1e6)
        else:
            result["chosen_IF_text"] = "%.8f MHz = LO freq - input" % (IF_actual/1e6)

        self.sl.set_ddc_ref_freq(IF_actual, d["channel_id"])

        for field in result:
            prefix = "ch%d_" % d["channel_id"]
            self.config[prefix + field] = result[field]

        result["lpf"] = self.sl.get_ddc_filter()
        result["type"] = "LO"
        self.sig_new_settings.emit(result)

    def get_approximate_input_freq(self, adc_channel_id):
        field_name = "ch%d_" % adc_channel_id + "expected_freq"
        IF_actual = self.config.get(field_name, None)
        if IF_actual is None:
            IF_actual = 40e6 # not critical, just used to lookup the amplitude calibration curve
        return IF_actual

    def slowTimerEvent(self):
        data = self.sl.phaseReadoutDriver.readData()
        if data is None:
            return
        for field in data.dtype.fields:
            with open('out_%s.bin' % field, 'ab') as f:
                f.write(data[field].tobytes())

        ts = data['timestamp']
        for channel in range(1, self.num_iq_channels+1):
            phi = data['phi%d' % channel]
            freq = self.frequencyCounterFromPhaseData(ts, phi)
            self.perChannelEmitters[channel-1].sig_new_freq.emit(freq)

    def frequencyCounterFromPhaseData(self, ts, phi):
        """ TODO: This needs to be improved,
        right now the 'gate time' will vary with the timers' period variation,
        which is not great """
        phi = phi - phi[0]
        phi = phi.astype(np.float) # this limits accuracy to at best ~1e-15 due to computations in double-precision floats
        phi = phi/2**self.sl.phaseReadoutDriver.n_bits_phase
        phi = phi/self.sl.phaseReadoutDriver.n_cycles
        # phi is now in cycles
        ts = ts - ts[0]
        ts = ts.astype(np.float)
        fit = np.polynomial.Polynomial.fit(ts, phi, 1)
        freq = fit.convert().coef[1] # freq is in units of cycles of the IQ waveform/cycles of the ADC clock
        freq_Hz = freq * self.sl.fs
        return freq_Hz

    def fastTimerEvent(self):
        # each ADC channel is shared by two IQ channels
        adc_channel_to_linked_iq_channels = {
            1: (1, 2),
            2: (3, 4)}
        for adc_channel_id in range(self.num_adc_channels):
            iq_channel1, iq_channel2 = adc_channel_to_linked_iq_channels[adc_channel_id+1]
            N1 = self.pts_settings[iq_channel1-1]["pts_ADC"]
            N2 = self.pts_settings[iq_channel2-1]["pts_ADC"]
            N_ADC = max(N1, N2) # we grab the longest trace requested, then emit just what was requested by each
            bAutoRefresh1 = self.pts_settings[iq_channel1-1]["autorefresh"] and (iq_channel1-1 == self.current_tab)
            bAutoRefresh2 = self.pts_settings[iq_channel2-1]["autorefresh"] and (iq_channel2-1 == self.current_tab)
            if not bAutoRefresh1 and not bAutoRefresh2:
                continue
            # fs = 125e6
            # adc_data = 0.1*np.cos(2*np.pi*15e6/125e6*np.linspace(0, N-1, N)) + 0.01*np.random.randn(N)
            (timestamp, adc_data) = self.sl.getADCdata(adc_channel_id+1, N_ADC) # adc numbers are 1-based
            scale_factor_adc_to_input = self.sl.scale_factor_adc_to_input(self.get_approximate_input_freq(adc_channel_id+1))

            # adc_data = self.getIQdata(1, N)
            if adc_data is not None:
                if bAutoRefresh1:
                    self.perChannelEmitters[iq_channel1-1].sig_new_adc_data.emit(adc_data[:N1], self.sl.getADCmaxVoltage(), scale_factor_adc_to_input)
                if bAutoRefresh2:
                    self.perChannelEmitters[iq_channel2-1].sig_new_adc_data.emit(adc_data[:N2], self.sl.getADCmaxVoltage(), scale_factor_adc_to_input)


        # IQ channels are independent otherwise
        for iq_channel_id in range(self.num_iq_channels):
            if not self.pts_settings[iq_channel_id]["autorefresh"] or (iq_channel_id != self.current_tab):
                continue
            N = self.pts_settings[iq_channel_id]["pts_IQ"]
            (timestamp, complex_baseband) = self.sl.getIQdata(iq_channel_id+1, N) # ids are 1-based here too
            # complex_baseband = 0.1*np.exp(1j*np.linspace(0, 2*np.pi, N)) + 0.01*np.random.randn(N) + 0.01*1j*np.random.randn(N)
            scale_factor_adc_to_input = self.sl.scale_factor_adc_to_input(self.get_approximate_input_freq(iq_channel_id+1))
            self.perChannelEmitters[iq_channel_id].sig_new_iq_data.emit(complex_baseband, scale_factor_adc_to_input)

    def fasterTimerEvent(self):
        data = self.sl.phaseReadoutDriver.peakLatestChunk()
        self.sig_phase_point.emit(data)

    def setupADCclock(self, bExternal, f_ref):
        # Valid VCO range is 600 MHz-1600 MHz according to DS181
        if not bExternal:
            if f_ref == 200e6:
                # For 200 MHz clock (internal), these settings should yield 125 MHz ADC clock, 1000 MHz VCO
                f_ref          = 200e6
                CLKFBOUT_MULT  = 5
                CLKOUT0_DIVIDE = 8
            else:
                raise Exception('unsupported fref')
        else: # external clock
            if f_ref == 200e6:
                # For 200 MHz external clock input, these settings should yield 125 MHz ADC clock, 1000 MHz VCO
                f_ext          = 200e6
                CLKFBOUT_MULT  = 5
                CLKOUT0_DIVIDE = 8

            elif f_ref == 10e6:
                # For 10 MHz external clock input, these settings should yield 124 MHz ADC clock, 620 MHz VCO
                f_ext          = 10e6
                CLKFBOUT_MULT  = 62
                CLKOUT0_DIVIDE = 5

            elif f_ref == 25e6:
                # For 25 MHz external clock input, these settings should yield 125 MHz ADC clock, 1250 MHz VCO
                f_ref          = 25e6
                CLKFBOUT_MULT  = 50
                CLKOUT0_DIVIDE = 10
            else:
                raise Exception('unsupported fref')


        self.sl.setADCclockPLL(f_ref, bExternal, CLKFBOUT_MULT, CLKOUT0_DIVIDE)

    def updateTabVisibility(self, tab_index):
        self.current_tab = tab_index
        print("tab_index=", tab_index)
        for k in range(self.num_iq_channels):
            self.perChannelEmitters[k].sig_set_visible.emit(k == tab_index)

class MyScrollArea(QtWidgets.QScrollArea):
    """ Just a normal scrollarea,
    except that we re-implement viewportSizeHint()
    to give a sensible starting value """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def viewportSizeHint(self):
        return QtCore.QSize(1500, 600)

def amplitude_calibration(test_widget):
    """ Measures received amplitude at various frequencies.
    Requires a Siglent SSA-3021X with tracking generator option,
    and prior calibration of said generator using amplitude_calibration.py """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    s.connect(("192.168.2.17", 5025))

    freq_axis = np.linspace(40e6, 2e9, 1000)
    # freq_axis = np.tile(freq_axis, 3)
    # freq_axis = freq_axis[np.array((0, 28, 57))]
    print(freq_axis)
    amplitude = 0*freq_axis
    for index, freq in enumerate(freq_axis):
        print("Setting freq = %f MHz" % (freq/1e6))
        s.sendall((':FREQ:CENT %.3f MHz\n' % (freq/1e6)).encode('ascii'))

        d = dict()
        d["channel_id"] = 1
        d["expected_freq"] = freq
        d["target_if"] = 20e6
        d["upper_sideband"] = False
        test_widget.setup_LO(d)

        time.sleep(200e-3)
        (timestamp, complex_baseband) = test_widget.sl.getIQdata(d["channel_id"], int(1e3))
        mean_amplitude = np.mean(np.abs(complex_baseband))
        # input signal is A*cos(), baseband signal is A*exp(),
        # average power in input signal is thus A**2/2/Z
        impedance = 50
        mean_power_W = mean_amplitude**2/2/impedance
        mean_power_dBm = 10*np.log10(mean_power_W * 1e3)
        print("f=%f MHz, pwr=%f dBm" % (freq/1e6, mean_power_dBm))
        amplitude[index] = mean_power_dBm

    print(amplitude)
    with open("output_amplitude.bin", "wb") as f:
        f.write(amplitude.tobytes())

    with open("output_freq.bin", "wb") as f:
        f.write(freq_axis.tobytes())

def main():
    # for testing when ran without a parent GUI
    app = QtWidgets.QApplication(sys.argv)
    test_widget = Test()
    tab_widget = QtGui.QTabWidget()
    tab_widget.resize(1200, 300)
    vbox = QtWidgets.QVBoxLayout()

    GUIs = list()
    for channel_id in range(4):
        GUI = channel_gui.ChannelGUI(channel_id+1) # 1-based IDs used here

        # Connect signals to slots
        GUI.sig_set_num_points.connect(test_widget.set_num_points)
        GUI.sig_setup_LO.connect(test_widget.setup_LO)
        test_widget.perChannelEmitters[channel_id].sig_new_adc_data.connect(GUI.newADCdata)
        test_widget.perChannelEmitters[channel_id].sig_new_iq_data.connect(GUI.newIQdata)
        test_widget.perChannelEmitters[channel_id].sig_new_freq.connect(GUI.newFreqData)
        test_widget.perChannelEmitters[channel_id].sig_set_visible.connect(GUI.setVisibility)
        test_widget.sig_new_settings.connect(GUI.newSettings)
        test_widget.sig_phase_point.connect(GUI.newPhasePoint)

        # Trigger a few updates now that the signals have been connected
        GUI.probingSettingsChanged()

        # vbox.addWidget(GUI)
        tab_widget.addTab(GUI, "Channel %d" % (channel_id+1))
        GUIs.append(GUI)

    if len(sys.argv) > 1 and sys.argv[1] == '-amplitude_calibration':
        print("Running amplitude calibration...")
        amplitude_calibration(test_widget)
        print("Amplitude calibration complete! Data saved to disk.")
    
    # tab_widget.setLayout(vbox)
    # main_widget.show()
    tab_widget.currentChanged.connect(test_widget.updateTabVisibility)
    tab_widget.show()

    # scrollarea = MyScrollArea() # need a scrollarea since 4 channel is too big...
    # scrollarea.setWidget(main_widget)
    # scrollarea.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
    # scrollarea.show()

    test_widget.updateTabVisibility(0)
    test_widget.emit_system_settings()
    test_widget.fasterTimer.start(5)
    test_widget.fastTimer.start(20)
    test_widget.slowTimer.start(1000)
    
    app.exec_()

if __name__ == '__main__':
    main()