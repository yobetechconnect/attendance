"""
Face recognition service.

Wraps OpenCV's LBPH (Local Binary Patterns Histograms) face recognizer.
Each student's trained face data is stored as its own small model file,
keyed by the student's internal database ID (never by name), under
FACE_DATA_DIR. A single combined model is retrained whenever enrollment
data changes and cached for fast recognition during live sessions.

LBPH prediction returns a *distance* — lower means a better match.
`FACE_RECOGNITION_THRESHOLD` in config.py is the maximum distance
accepted as a positive identification; anything above it is treated
as "not recognized" and must NOT mark attendance.
"""

import json
import os
import threading

import cv2
import numpy as np

from services.face_detection import FACE_SIZE

MODEL_FILENAME = "lbph_model.yml"
LABELS_FILENAME = "labels.json"


# One loaded model per process, reloaded only when the file on disk changes.
# (Loading a trained LBPH model for every camera frame was the main per-frame cost.)
_MODEL_CACHE = {}
_CACHE_LOCK = threading.Lock()


class FaceRecognitionService:
    def __init__(self, face_data_dir: str, threshold: float):
        self.face_data_dir = face_data_dir
        self.threshold = threshold
        self.model_path = os.path.join(face_data_dir, MODEL_FILENAME)
        self.labels_path = os.path.join(face_data_dir, LABELS_FILENAME)
        os.makedirs(face_data_dir, exist_ok=True)

    # ---------------- Enrollment sample storage ----------------

    def student_sample_dir(self, student_id: int) -> str:
        path = os.path.join(self.face_data_dir, f"student_{student_id}")
        os.makedirs(path, exist_ok=True)
        return path

    def save_sample(self, student_id: int, face_crop: np.ndarray, index: int) -> str:
        """Persist one preprocessed face sample (grayscale, FACE_SIZE) to disk."""
        path = os.path.join(self.student_sample_dir(student_id), f"sample_{index:03d}.png")
        cv2.imwrite(path, face_crop)
        return path

    def sample_count(self, student_id: int) -> int:
        directory = self.student_sample_dir(student_id)
        return len([f for f in os.listdir(directory) if f.endswith(".png")])

    def delete_student_samples(self, student_id: int):
        directory = self.student_sample_dir(student_id)
        if os.path.isdir(directory):
            for f in os.listdir(directory):
                os.remove(os.path.join(directory, f))

    # ---------------- Training ----------------

    def train_all(self, student_ids: list[int]):
        """
        Retrain the LBPH model from every active student's saved samples.
        Called after any enrollment change (new student enrolled, samples
        added, or a student's face profile deactivated).
        """
        faces = []
        labels = []
        label_map = {}  # numeric label -> student_id (LBPH needs integer labels)

        for label, student_id in enumerate(student_ids):
            directory = self.student_sample_dir(student_id)
            if not os.path.isdir(directory):
                continue
            sample_files = sorted(f for f in os.listdir(directory) if f.endswith(".png"))
            if not sample_files:
                continue
            label_map[label] = student_id
            for fname in sample_files:
                img = cv2.imread(os.path.join(directory, fname), cv2.IMREAD_GRAYSCALE)
                if img is None:
                    continue
                if img.shape != FACE_SIZE[::-1]:
                    img = cv2.resize(img, FACE_SIZE)
                faces.append(img)
                labels.append(label)

        if not faces:
            raise ValueError("No enrolled face samples available to train on.")

        recognizer = cv2.face.LBPHFaceRecognizer_create()
        recognizer.train(faces, np.array(labels))
        # Write to temp files, then rename: a scan happening during retraining never sees a half-written model.
        tmp_model = self.model_path + ".tmp.yml"
        recognizer.write(tmp_model)
        tmp_labels = self.labels_path + ".tmp"
        with open(tmp_labels, "w", encoding="utf-8") as f:
            json.dump(label_map, f)
        os.replace(tmp_model, self.model_path)
        os.replace(tmp_labels, self.labels_path)

        return len(faces), len(label_map)

    # ---------------- Recognition ----------------

    def _load(self):
        if not (os.path.exists(self.model_path) and os.path.exists(self.labels_path)):
            return None, {}
        stamp = (os.path.getmtime(self.model_path), os.path.getmtime(self.labels_path))
        with _CACHE_LOCK:
            cached = _MODEL_CACHE.get(self.model_path)
            if cached and cached[0] == stamp:
                return cached[1], cached[2]
            recognizer = cv2.face.LBPHFaceRecognizer_create()
            recognizer.read(self.model_path)
            with open(self.labels_path, "r", encoding="utf-8") as f:
                label_map = {int(k): v for k, v in json.load(f).items()}
            _MODEL_CACHE[self.model_path] = (stamp, recognizer, label_map)
            return recognizer, label_map

    def recognize(self, face_crop: np.ndarray):
        """
        Identify a preprocessed face crop.

        Returns (student_id, confidence) if the best match is within the
        configured threshold, otherwise (None, confidence). `confidence`
        here is the raw LBPH distance (lower = better) so it can be logged
        even on a rejection.
        """
        recognizer, label_map = self._load()
        if recognizer is None:
            return None, None

        label, distance = recognizer.predict(face_crop)
        student_id = label_map.get(label)

        if student_id is None or distance > self.threshold:
            return None, distance

        return student_id, distance
