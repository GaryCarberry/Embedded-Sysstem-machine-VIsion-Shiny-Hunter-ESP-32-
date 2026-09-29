import cv2, numpy as np
cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
ret, frame = cap.read()
cap.release()
cv2.imwrite("shiny_test_frame.png", frame)
print(frame.shape)