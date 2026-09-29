import cv2
import numpy as np

# ---------- Webcam ----------
CAMERA_INDEX = 0

cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_DSHOW)

if not cap.isOpened():
    raise RuntimeError("Could not open webcam")

for _ in range(10):
    cap.read()

frame = None
hsv = None


def click(event, x, y, flags, param):
    global frame, hsv

    if event == cv2.EVENT_LBUTTONDOWN:
        b, g, r = frame[y, x]
        h, s, v = hsv[y, x]

        print()
        print("-----------------------------")
        print(f"Pixel ({x}, {y})")
        print(f"BGR = ({b}, {g}, {r})")
        print(f"HSV = ({h}, {s}, {v})")
        print("-----------------------------")


cv2.namedWindow("Camera")
cv2.setMouseCallback("Camera", click)

while True:

    ret, frame = cap.read()

    if not ret:
        continue

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    cv2.imshow("Camera", frame)

    key = cv2.waitKey(1) & 0xFF

    if key == ord("s"):
        cv2.imwrite("frame.png", frame)
        print("Saved frame.png")

    elif key == 27:
        break

cap.release()
cv2.destroyAllWindows()