"""调试压电陶瓷：通过 DAC 来回拉电压，让管子振动。

Teensy 脚 7 = DAC X 的片选（CS），脚 9 = DAC Y 的片选。
压电应接到板子上 X / Y 的模拟输出，不是直接焊在 7、9 数字脚上。
X 约 ±5 V，中点 32768 = 0 V。
"""

import time

import stm_control

PORT = "/dev/cu.usbmodem164952701"

# "x" / "y" / "both" / "firmware"
# firmware 会发 TEST：先 Z 再 X 再 Y，各 500 次 1 kHz 方波
AXIS = "both"

# 方波高低码值和频率。0 和 50000 大约是 -5 V 和 +2.6 V，能摸到振动。
DAC_LO = 10000
DAC_HI = 55000
FREQ_HZ = 5
SECONDS = 3
MID = 32768


def square_wave(stm, setters, seconds, freq_hz):
    half = 0.5 / max(freq_hz, 0.1)
    n = int(seconds * freq_hz)
    print(f"square wave {freq_hz} Hz, {n} cycles, {seconds}s")
    for _ in range(n):
        for set_dac in setters:
            set_dac(DAC_HI)
        time.sleep(half)
        for set_dac in setters:
            set_dac(DAC_LO)
        time.sleep(half)
    for set_dac in setters:
        set_dac(MID)


def main():
    stm = stm_control.STM()
    stm.open(PORT)
    print(f"opened {PORT}")
    print("pin 7 -> DAC X, pin 9 -> DAC Y")

    if AXIS == "firmware":
        print("firmware TEST: Z, then X, then Y, ~1 kHz")
        stm.send_cmd("TEST")
        time.sleep(5)
    else:
        setters = []
        if AXIS in ("x", "both"):
            setters.append(stm.set_dacx)
            print("drive DAC X (pin 7)")
        if AXIS in ("y", "both"):
            setters.append(stm.set_dacy)
            print("drive DAC Y (pin 9)")
        square_wave(stm, setters, SECONDS, FREQ_HZ)

    print("back to midscale 32768")
    stm.close()


if __name__ == "__main__":
    main()
