import ns_controller
import time

controller = ns_controller.NSController("COM3")

print("Pressing ABXY together...")

controller.press(["A", "B", "X", "Y"], hold=0.5)

time.sleep(1)

controller.close()

print("Done")