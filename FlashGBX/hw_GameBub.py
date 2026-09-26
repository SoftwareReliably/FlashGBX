# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/lesserkuma)

# pylint: disable=wildcard-import, unused-wildcard-import
from .i18n import __
from .LK_Device import *
from .Logging import logger


class GbxDevice(LK_Device):
    device_name = "Game Bub"
    max_buffer_read = 1024
    max_buffer_write = 512
    DEVICE_LABEL_LONG = "Game Bub"
    DEVICE_LABEL_SHORT = "Game Bub"
    FWUPDATE_ACTION = None
    DEVICE_SUPPORT_MESSAGE = "For help with your Game Bub, please see the user guide:\nhttps://docs.gamebub.net/"

    def __init__(self):
        pass

    def _write(self, data, wait=False):
        if not isinstance(data, bytearray):
            data = bytearray([data])

        # Avoid sending exact 64-byte USB packet multiples in one write.
        if len(data) > 1 and len(data) % 64 == 0:
            super()._write(data[:-1], wait=False)
            return super()._write(data[-1:], wait=wait)

        return super()._write(data, wait=wait)

    def Initialize(self, flashcarts, port=None, max_baud=2000000):
        if self.IsConnected():
            self.device.close()
        conn_msg = []
        ports = []
        if port is not None:
            ports = [port]
        else:
            comports = serial.tools.list_ports.comports()
            for i in range(len(comports)):
                if comports[i].vid == 0x1209 and comports[i].pid == 0xB010:
                    ports.append(comports[i].device)
            if len(ports) == 0:
                return False

        for i in range(len(ports)):
            if self.TryConnect(ports[i], max_baud):
                self.baudrate = max_baud
                try:
                    dev = serial.Serial(ports[i], self.baudrate, timeout=0.1, exclusive=True)
                except (SerialException, OSError) as e:
                    dprint(f"Couldn’t reopen port {ports[i]:s}:", e)
                    continue
                self.device = dev
            else:
                continue

            if self.fw is None or self.fw == {}:
                continue

            dprint(f"Found a {self.device_name}")
            dprint("Firmware information:", self.fw)

            if self.device is None or not self.IsConnected():
                self.device = None
                if self.fw is not None:
                    conn_msg.append(
                        [
                            0,
                            __(
                                "Couldn’t communicate with the {device_name} on port {port}. Please disconnect and reconnect the device, then try again.",
                                device_name=self.device_name,
                                port=ports[i],
                            ),
                        ],
                    )
                continue

            self.port = ports[i]
            self.device.timeout = self.device_timeout

            # Load Flash Cartridge Handlers
            self.UpdateFlashCarts(flashcarts)

            # Stop after first found device
            break

        return conn_msg

    def LoadFirmwareVersion(self):
        dprint("Querying firmware version")
        try:
            self.device.timeout = 0.075
            self.device.reset_input_buffer()
            self.device.reset_output_buffer()

            self._write(self.DEVICE_CMD["QUERY_FW_INFO"])
            size = self._read(1)
            if size != 8:
                return False
            data = self._read(size)
            info = data[:8]
            keys = ["cfw_id", "fw_ver", "pcb_ver", "fw_ts"]
            values = struct.unpack(">cHBI", bytearray(info))
            self.fw = dict(zip(keys, values))
            self.fw["cfw_id"] = self.fw["cfw_id"].decode("ascii")
            self.fw["fw_dt"] = (
                datetime.datetime.fromtimestamp(self.fw["fw_ts"]).astimezone().replace(microsecond=0).isoformat()
            )
            self.fw["ofw_ver"] = None
            self.fw["pcb_name"] = ""
            self.fw["cart_power_ctrl"] = False
            self.fw["bootloader_reset"] = False
            if self.fw["cfw_id"] in ["L", "E"] and self.fw["fw_ver"] >= 12:
                size = self._read(1)
                name = self._read(size)
                if len(name) > 0:
                    try:
                        self.fw["pcb_name"] = name.decode("UTF-8").replace("\x00", "").strip()
                    except:
                        self.fw["pcb_name"] = "Unnamed Device"
                    self.device_name = self.fw["pcb_name"]

                # Cartridge Power Control support, Switch Power support, and Switch Mode support
                temp = self._read(1)
                self.fw["cart_power_ctrl"] = temp & 1 == 1
                self.fw["cart_presence_switch"] = (temp >> 1) & 1 == 1
                self.fw["cart_mode_switch"] = (temp >> 2) & 1 == 1

                # Reset to bootloader support
                self.fw["bootloader_reset"] = self._read(1) == 1

            return True

        except Exception as e:
            dprint("Disconnecting due to an error", e, sep="\n")
            try:
                if self.device.isOpen():
                    self.device.reset_input_buffer()
                    self.device.reset_output_buffer()
                    self.device.close()
                self.device = None
            except Exception:
                logger.exception("Failed to close Game Bub after an initialization error")
            return False

    def ChangeBaudRate(self, _):
        dprint("Baudrate change is not supported.")

    def GetFirmwareVersion(self, more=False):
        s = "{:s}{:d}".format(self.fw["cfw_id"], self.fw["fw_ver"])
        if more:
            s += " ({:s})".format(self.fw["fw_dt"])
        return s

    def GetFullNameExtended(self, more=False):
        if more:
            return __(
                "{device_name} – Firmware {fw_version} ({timestamp}) on {port}",
                device_name=self.GetFullName(),
                fw_version=self.GetFirmwareVersion(),
                timestamp=self.fw["fw_dt"],
                port=self.GetPort(),
            )
        return __(
            "{device_name} – Firmware {fw_version} ({port})",
            device_name=self.GetFullName(),
            fw_version=self.GetFirmwareVersion(),
            port=self.GetPort(),
        )

    def CanSetVoltageBySwitch(self):
        return False

    def CanSetVoltageByCode(self):
        return False

    def CanSetVoltageByAutoswitch(self):
        return True

    def CanPowerCycleCart(self):
        return True

    def GetSupprtedModes(self):
        return ["DMG", "AGB"]

    def IsSupported3dMemory(self):
        return True

    def IsClkConnected(self):
        return True

    def SupportsFirmwareUpdates(self):
        return False

    def FirmwareUpdateAvailable(self):
        return False

    def GetFirmwareUpdaterClass(self):
        return None

    def ResetLEDs(self):
        pass

    def SupportsBootloaderReset(self):
        return False

    def BootloaderReset(self):
        return False

    def SupportsAudioAsWe(self):
        return True

    def GetFullName(self):
        return self.device_name
