import stm_control
import unittest


class TestStm(unittest.TestCase):

    def setUp(self):
        self.stm = stm_control.STM()
        self.stm.open("/dev/cu.usbmodem164952701")

    def test_move_motor(self):
        self.stm.move_motor(-10)

    # def test_get_status(self):
    #     print(self.stm.get_status())

    # def test_iv_curve(self):
    #     print(self.stm.get_iv_curve())
    # def test_scan(self):
    #     self.stm.start_scan()


if __name__ == '__main__':
    unittest.main()
