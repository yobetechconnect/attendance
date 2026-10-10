"""
Face detection service.

Wraps OpenCV's Haar Cascade frontal-face detector. Used both during
face enrollment (accepting/rejecting camera samples) and during live
attendance recognition (locating a face in each frame before it is
handed to the recognizer).
"""

import cv2
import numpy as np

_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
_face_cascade = cv2.CascadeClassifier(_CASCADE_PATH)

FACE_SIZE = (200, 200)  # standardized size fed to the LBPH recognizer


class NoFaceDetected(Exception):
    """Raised when a frame contains no detectable face."""


class MultipleFacesDetected(Exception):
    """Raised when a frame contains more than one candidate face."""


def decode_image(image_bytes: bytes) -> np.ndarray:
    """Decode raw image bytes (e.g. from an uploaded JPEG/PNG) into a BGR array."""
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image data.")
    return img


def detect_faces(image: np.ndarray):
    """Return a list of (x, y, w, h) bounding boxes for faces found in the image."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    faces = _face_cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60)
    )
    return list(faces)


def extract_single_face(image: np.ndarray) -> np.ndarray:
    """
    Detect exactly one face in the image and return it as a preprocessed
    (grayscale, equalized, resized) crop ready for LBPH training/prediction.

    Raises NoFaceDetected if no face is found, or MultipleFacesDetected if
    more than one is found (enrollment/recognition should ask the user to
    ensure only a single, clearly visible face is in frame).
    """
    faces = detect_faces(image)

    if len(faces) == 0:
        raise NoFaceDetected("No face detected in the frame.")
    if len(faces) > 1:
        raise MultipleFacesDetected("More than one face detected in the frame.")

    x, y, w, h = faces[0]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    face_crop = gray[y : y + h, x : x + w]
    face_crop = cv2.equalizeHist(face_crop)
    face_crop = cv2.resize(face_crop, FACE_SIZE, interpolation=cv2.INTER_LINEAR)
    return face_crop
