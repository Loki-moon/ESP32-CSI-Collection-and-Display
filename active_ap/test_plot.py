import sys
import time
import socket
import collections
import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui

UDP_IP = "192.168.4.2"
UDP_PORT = 8848

QUEUE_LEN = 50
CSI_LEN = 57 * 2
DISP_FRAME_RATE = 10 # 10 frames per second

node_mac_list = []
corlor_list = []

# lists to hold 'artists' from matplotlib
curve_rssi_list = []
scatter_rssi_list = []
text_rssi_list = []
curve_csi_list = []

def parse_data_line (line, data_len) :
    data = line.split(",") # separate by commas
    assert(data[-1] == "")
    data = [ int(x) for x in data[0:-1] ] # ignore the last one
    assert(len(data) == data_len)

    return data

def add_new_node (pyqt_app, node_id):
    if node_id == 0:
        return
    rssi_que_list.append( collections.deque(np.zeros(QUEUE_LEN)) )
    csi_points_list.append( np.zeros(CSI_LEN) )
    # new rssi curve
    curve_rssi_list.append( pyqt_app.pw1.plot(pen=(node_id, 3)) ) # append SNR curve


    # new csi curve
    curve_csi_list.append( pyqt_app.pw2.plot(pen=(node_id, 3)) ) # append SNR curve


    

def parse_data_packet (pyqt_app, data) :
    data_str = str(data, encoding="ascii")
    lines = data_str.splitlines()
    node_id = -1
    for l_count in range(len(lines)):
        line = lines[l_count]
        print(line)
        items = line.split(",")

        if items[0].find("mac =") >= 0:
            mac_addr = items[0][items[0].find("mac =") + 5:]
            # if a new mac addr
            if not mac_addr in node_mac_list:
                node_mac_list.append(mac_addr)
                node_id = len(node_mac_list) - 1
                add_new_node(pyqt_app, node_id)
            else:
                node_id = node_mac_list.index(mac_addr)

        if items[0] == "rx_ctrl info":
            # the next line should be rx_ctrl info.
            tmp_pos = items[1].find("len = ")
            rx_ctrl_len = int(items[1][tmp_pos+6:]) 
            # parse rx ctrl data
            rx_ctrl_data = parse_data_line(lines[l_count + 1], rx_ctrl_len)

        if items[0] == "RAW" :
            # the next line should be raw csi data.
            tmp_pos = items[1].find("len = ")
            raw_csi_len = int(items[1][tmp_pos+6:])
            # parse csi raw data
            raw_csi_data = parse_data_line(lines[l_count + 1], raw_csi_len)
    # a newline to separate packets
    print()

    return ( rx_ctrl_data, raw_csi_data, node_id)

# scale csi data accoding to SNR
# change to numpy array as well
def cook_csi_data (rx_ctrl_info, raw_csi_data) :
    rssi = rx_ctrl_info[0]  # dbm
    noise_floor = rx_ctrl_info[11] # dbm. The document says unit is 0.25 dbm but it does not make sense.
    # do not know AGC

    # Each channel frequency response of sub-carrier is recorded by two bytes of signed characters. 
    # The first one is imaginary part and the second one is real part.
    raw_csi_data = [ (raw_csi_data[2*i] * 1j + raw_csi_data[2*i + 1]) for i in range(int(len(raw_csi_data) / 2)) ]
    raw_csi_array = np.array(raw_csi_data)    

    ## Note:this part of SNR computation may not be accurate.
    #       The reason is that ESP32 may not provide a accurate noise floor value.
    #       The underlying reason could tha AGC is not calculated explicitly 
    #       so ESP32 doc just consider noise * 0.25 dbm as a estimated value. (described in the official doc)
    #       But here I will jut use the noise value in rx_ctrl info times 1 dbm as the noise floor.
    # scale csi
    snr_db = rssi - noise_floor # dB
    snr_abs = 10**(snr_db / 10.0) # from db back to normal
    csi_sum = np.sum(np.abs(raw_csi_array)**2)
    num_subcarrier = len(raw_csi_array)
    scale = np.sqrt((snr_abs / csi_sum) * num_subcarrier)
    raw_csi_array = raw_csi_array * scale
    print("SNR = {} dB".format(snr_db))
    #

    # TODO: delete pilot subcarriers
    # Note:
    #   check https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/wifi.html
    #   section 'Wi-Fi Channel State Information' 
    #   sub-carrier index : LLTF (-64~-1) + HT-LTF (0~63,-64~-1)
    # In the 40MHz HT transmission, two adjacent 20MHz channels are used. 
    # The channel is divided into 128 sub-carriers. 6 pilot signals are inserted in sub-carriers -53, -25, -11, 11, 25, 53. 
    # Signal is transmitted on sub-carriers -58 to -2 and 2 to 58.
    assert(len(raw_csi_array) == 64 * 3)
    cooked_csi_array = raw_csi_array[64:]
    # rearrange to -58 ~ -2 and 2 ~ 58.
    cooked_csi_array = np.concatenate((cooked_csi_array[-58:-1], cooked_csi_array[2:59]))
    assert(len(cooked_csi_array) == CSI_LEN)
    
    print("RSSI = {} dBm\n".format(rssi))
    return (snr_db, cooked_csi_array)

def update_esp32_data(pyqt_app):
    # recv UDP packet
    data, addr = sock.recvfrom(2048) # buffer size is 2048 bytes

    # parse data packet to get lists of data
    (rx_ctrl_data, raw_csi_data, node_id) = parse_data_packet(pyqt_app, data)
    # only process HT(802.11 n) and 40 MHz frames
    # sig-mod and channel bandwidth fields
    if rx_ctrl_data[2] != 1 or rx_ctrl_data[4] != 1 :
        return -1
    # node_id not assigned, error
    assert(node_id >= 0)
    print("Got a HT 40MHz packet ...")

    # prepare csi data
    (rssi, csi_data) = cook_csi_data(rx_ctrl_data, raw_csi_data)

    # update RSSI
    print("node id = ", node_id)
    rssi_que_list[node_id].popleft()
    rssi_que_list[node_id].append( rssi )
    # update CSI
    csi_points_list[node_id] = 10 * np.log10(np.abs(csi_data)**2)

    return node_id


class App(QtGui.QMainWindow):
    def __init__(self, parent=None):
        super(App, self).__init__(parent)

        #### Create Gui Elements ###########
        self.mainbox = pg.LayoutWidget()
        self.setCentralWidget(self.mainbox)

        self.disp_time = np.array([ (x - QUEUE_LEN + 1)/ DISP_FRAME_RATE for x in range(QUEUE_LEN)])

        self.pw1 = pg.PlotWidget(name="Plot1")
        curve_rssi_list.append( self.pw1.plot(pen=(0, 3)) ) # append SNR curve
        self.mainbox.addWidget(self.pw1, row=0, col=0)
        self.pw1.setLabel('left', 'SNR', units='dB')
        self.pw1.setLabel('bottom', 'Time ', units=None)
        self.pw1.setYRange(0, 50)

        self.pw2 = pg.PlotWidget(name="Plot2")
        curve_csi_list.append( self.pw2.plot(pen=(0, 3)) ) # append CSI curve
        self.mainbox.addWidget(self.pw2, row=0, col=1)
        self.pw2.setLabel('left', 'CSI', units='dB')
        self.pw2.setLabel('bottom', 'subcarriers [-58, -2] and [2, 58] ', units=None)
        self.pw2.setXRange(0, CSI_LEN)
        self.pw2.setYRange(0, 50)

        self.info_panel = pg.GraphicsLayoutWidget()
        self.mainbox.addWidget(self.info_panel, row=0, col=2)

        self.label = QtGui.QLabel()
        self.mainbox.addWidget(self.label, row=1, col=0)

        self.view = self.info_panel.addViewBox()
        self.view.setAspectLocked(True)
        self.view.setRange(QtCore.QRectF(0,0, 100, 100))

        #  image plot
        self.img = pg.ImageItem(border='w')
        self.view.addItem(self.img)


        #### Set Data  #####################

        self.x = np.linspace(0,50., num=100)
        self.X,self.Y = np.meshgrid(self.x,self.x)

        self.counter = 0
        self.fps = 0.
        self.lastupdate = time.time()

        #### Start  #####################
        self._update()

    def _update(self):

        node_id = update_esp32_data(self)
        if node_id >= 0:
            curve_rssi_list[node_id].setData(x=self.disp_time, y=rssi_que_list[node_id], pen=(node_id, 3))
            curve_csi_list[node_id].setData(y=csi_points_list[node_id], pen=(node_id, 3))

        self.data = np.sin(self.X/3.+self.counter/9.)*np.cos(self.Y/3.+self.counter/9.)

        self.img.setImage(self.data)

        now = time.time()
        dt = (now-self.lastupdate)
        if dt <= 0:
            dt = 0.000000000001
        fps2 = 1.0 / dt
        self.lastupdate = now
        self.fps = self.fps * 0.9 + fps2 * 0.1
        tx = 'Mean Frame Rate:  {fps:.3f} FPS'.format(fps=self.fps )
        self.label.setText(tx)
        QtCore.QTimer.singleShot(1, self._update)
        self.counter += 1


if __name__ == '__main__':
    # a queue to hold SNR values
    rssi_que_list = [collections.deque(np.zeros(QUEUE_LEN))]
    csi_points_list = [np.zeros(CSI_LEN)]

    # create a recv socket for packets from ESP32 soft-ap
    sock = socket.socket(socket.AF_INET, # Internet
                        socket.SOCK_DGRAM) # UDP
    sock.bind((UDP_IP, UDP_PORT))

    app = QtGui.QApplication(sys.argv)
    thisapp = App()
    thisapp.show()
    sys.exit(app.exec_())