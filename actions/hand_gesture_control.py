"""
Hand Gesture Control for IRA

Uses MediaPipe Hands + OpenCV to detect hand gestures via webcam
and maps them to PC control actions.

Gestures:
  - Palm (all fingers open)  â†’ Play/Pause media
  - Fist                    â†’ Mute / unmute
  - Swipe Left              â†’ Previous track / tab left
  - Swipe Right             â†’ Next track / tab right
  - Swipe Up                â†’ Volume up
  - Swipe Down              â†’ Volume down
  - Pinch (index+thumb)     â†’ Left click
  - Peace (V sign)           â†’ Toggle gesture control on/off
  - Thumbs Up               â†’ Brightness up
  - Thumbs Down             â†’ Brightness down
  - Two hands pinch         â†’ Zoom in/out
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
import pyautogui

# Suppress MediaPipe GPU delegate logs
import os as _os
_os.environ["GLOG_minloglevel"] = "2"

import mediapipe as mp

pyautogui.FAILSAFE = False


# â”€â”€ Gesture definitions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class Gesture(Enum):
    NONE        = "none"
    PALM        = "palm"         # all fingers open
    FIST        = "fist"         # all fingers closed
    SWIPE_LEFT  = "swipe_left"
    SWIPE_RIGHT = "swipe_right"
    SWIPE_UP    = "swipe_up"
    SWIPE_DOWN  = "swipe_down"
    PINCH       = "pinch"        # index + thumb touching
    PEACE       = "peace"        # V sign
    THUMBS_UP   = "thumbs_up"
    THUMBS_DOWN = "thumbs_down"
    OK          = "ok"           # OK sign


@dataclass
class GestureAction:
    gesture: Gesture
    action: str
    description: str


GESTURE_MAP: list[GestureAction] = [
    GestureAction(Gesture.PALM,        "media_playpause",  "Play / Pause media"),
    GestureAction(Gesture.FIST,        "volume_mute",      "Mute / Unmute"),
    GestureAction(Gesture.SWIPE_LEFT,  "media_previous",   "Previous track / tab left"),
    GestureAction(Gesture.SWIPE_RIGHT, "media_next",       "Next track / tab right"),
    GestureAction(Gesture.SWIPE_UP,    "volume_up",        "Volume up"),
    GestureAction(Gesture.SWIPE_DOWN,  "volume_down",      "Volume down"),
    GestureAction(Gesture.PEACE,       "toggle_gesture",   "Toggle gesture control"),
    GestureAction(Gesture.THUMBS_UP,   "brightness_up",    "Brightness up"),
    GestureAction(Gesture.THUMBS_DOWN, "brightness_down",  "Brightness down"),
]


# â”€â”€ Hand Tracking Engine â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class HandTracker:
    """Wraps MediaPipe Hands for landmark detection."""

    def __init__(self, max_hands: int = 2, min_detection_confidence: float = 0.7):
        self._mp_hands = mp.solutions.hands
        self._hands = self._mp_hands.Hands(
            static_image_mode=False,
            max_num_hands=max_hands,
            min_detection_confidence=min_detection_confidence,
            min_tracking_confidence=0.6,
        )

    def process(self, frame: np.ndarray) -> list[Any]:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result = self._hands.process(rgb)
        if result.multi_hand_landmarks:
            return result.multi_hand_landmarks
        return []

    def close(self):
        self._hands.close()


# â”€â”€ Gesture Recognizer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class GestureRecognizer:
    """Converts hand landmarks to named gestures."""

    def __init__(self):
        self._prev_centroid: tuple[float, float] | None = None
        self._swipe_threshold = 0.12  # normalized distance for swipe detection

    def _finger_states(self, landmarks) -> dict[str, bool]:
        """Return which fingers are up (True) or down (False)."""
        h, w = 1, 1  # landmarks are normalized
        lm = [(l.x, l.y, l.z) for l in landmarks.landmark]

        def _dist(i: int, j: int) -> float:
            return math.hypot(lm[i][0] - lm[j][0], lm[i][1] - lm[j][1])

        # Thumb: compare tip (4) with IP (3)
        thumb_up = lm[4][0] < lm[3][0]  # for right hand; adjust for left

        # Other fingers: tip Y < PIP Y
        index_up  = lm[8][1]  < lm[6][1]
        middle_up = lm[12][1] < lm[10][1]
        ring_up   = lm[16][1] < lm[14][1]
        pinky_up  = lm[20][1] < lm[18][1]

        return {
            "thumb": thumb_up,
            "index": index_up,
            "middle": middle_up,
            "ring": ring_up,
            "pinky": pinky_up,
        }

    def _pinch_distance(self, landmarks) -> float:
        lm = [(l.x, l.y, l.z) for l in landmarks.landmark]
        return math.hypot(lm[4][0] - lm[8][0], lm[4][1] - lm[8][1])

    def _centroid(self, landmarks) -> tuple[float, float]:
        """Average position of all landmarks (for swipe detection)."""
        xs = [l.x for l in landmarks.landmark]
        ys = [l.y for l in landmarks.landmark]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    def _detect_swipe(self, centroid: tuple[float, float]) -> Gesture | None:
        if self._prev_centroid is None:
            self._prev_centroid = centroid
            return None

        dx = centroid[0] - self._prev_centroid[0]
        dy = centroid[1] - self._prev_centroid[1]
        self._prev_centroid = centroid

        if abs(dx) > self._swipe_threshold or abs(dy) > self._swipe_threshold:
            if abs(dx) > abs(dy):
                return Gesture.SWIPE_LEFT if dx < 0 else Gesture.SWIPE_RIGHT
            else:
                return Gesture.SWIPE_UP if dy < 0 else Gesture.SWIPE_DOWN
        return None

    def recognize(self, landmarks, multi_handedness=None) -> Gesture:
        """Classify a single hand's landmarks into a Gesture."""
        fingers = self._finger_states(landmarks)
        pinch_dist = self._pinch_distance(landmarks)
        centroid = self._centroid(landmarks)

        # Detect swipe
        swipe = self._detect_swipe(centroid)
        if swipe:
            return swipe

        # Pinch (index + thumb close together)
        if pinch_dist < 0.05:
            return Gesture.PINCH

        # Count extended fingers
        extended = sum(fingers.values())

        # OK sign (thumb + index circle, others up)
        if (fingers["thumb"] and fingers["index"] and
            not fingers["middle"] and not fingers["ring"] and fingers["pinky"]):
            if pinch_dist < 0.08:
                return Gesture.OK

        # Peace / V sign (index + middle up, others down)
        if (fingers["index"] and fingers["middle"] and
            not fingers["ring"] and not fingers["pinky"] and not fingers["thumb"]):
            return Gesture.PEACE

        # Thumbs up (thumb up, all others down)
        if fingers["thumb"] and not any([fingers["index"], fingers["middle"],
                                          fingers["ring"], fingers["pinky"]]):
            return Gesture.THUMBS_UP

        # Thumbs down (thumb down, all others down)
        if not fingers["thumb"] and not any([fingers["index"], fingers["middle"],
                                              fingers["ring"], fingers["pinky"]]):
            return Gesture.THUMBS_DOWN

        # Fist (all down)
        if extended <= 1 and not fingers["thumb"]:
            return Gesture.FIST

        # Palm (all up)
        if extended >= 4:
            return Gesture.PALM

        return Gesture.NONE


# â”€â”€ Action Executor â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class GestureActionExecutor:
    """Executes PC control actions based on recognized gestures."""

    def __init__(self):
        self._cooldowns: dict[str, float] = {}
        self._cooldown_secs = 0.8  # prevent rapid repeat

    def _can_execute(self, action: str) -> bool:
        now = time.time()
        if action in self._cooldowns and now - self._cooldowns[action] < self._cooldown_secs:
            return False
        self._cooldowns[action] = now
        return True

    def execute(self, gesture: Gesture) -> str:
        """Execute action for gesture. Returns description string."""
        if gesture == Gesture.NONE:
            return ""

        for ga in GESTURE_MAP:
            if ga.gesture == gesture:
                if not self._can_execute(ga.action):
                    return ""
                return self._do_action(ga)

        if gesture == Gesture.PINCH:
            if self._can_execute("click"):
                pyautogui.click()
                return "Click"

        return ""

    def _do_action(self, ga: GestureAction) -> str:
        action = ga.action
        try:
            if action == "media_playpause":
                pyautogui.press("playpause")
            elif action == "media_previous":
                pyautogui.press("prevtrack")
            elif action == "media_next":
                pyautogui.press("nexttrack")
            elif action == "volume_up":
                pyautogui.press("volumeup")
            elif action == "volume_down":
                pyautogui.press("volumedown")
            elif action == "volume_mute":
                pyautogui.press("volumemute")
            elif action == "brightness_up":
                for _ in range(3):
                    pyautogui.press("brightnessup")
            elif action == "brightness_down":
                for _ in range(3):
                    pyautogui.press("brightnessdown")
            elif action == "toggle_gesture":
                return "__TOGGLE__"
            else:
                return ""
            return ga.description
        except Exception as e:
            return f"Error: {e}"


# â”€â”€ Gesture Control Service â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class HandGestureService:
    """
    Full hand gesture control service.
    Runs webcam â†’ hand tracking â†’ gesture recognition â†’ action execution loop.
    """

    def __init__(self, on_gesture: Callable[[str], None] | None = None,
                 camera_id: int = 0):
        self._camera_id = camera_id
        self._on_gesture = on_gesture  # callback with action description
        self._running = False
        self._paused = False
        self._thread: threading.Thread | None = None
        self._tracker: HandTracker | None = None
        self._recognizer = GestureRecognizer()
        self._executor = GestureActionExecutor()
        self._cap: cv2.VideoCapture | None = None
        self._show_preview = True
        self._last_gesture = Gesture.NONE
        self._gesture_count = 0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    def start(self):
        if self._running:
            return
        self._running = True
        self._paused = False
        self._thread = threading.Thread(target=self._loop, daemon=True, name="HandGesture")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        self._close_resources()

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def _close_resources(self):
        if self._cap:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        if self._tracker:
            try:
                self._tracker.close()
            except Exception:
                pass
            self._tracker = None
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass

    def _loop(self):
        # Use DirectShow backend on Windows for better camera compatibility
        import platform as _platform
        backend = cv2.CAP_DSHOW if _platform.system() == 'Windows' else cv2.CAP_ANY
        self._cap = cv2.VideoCapture(self._camera_id, backend)
        print(f'[HandGesture] Opening camera {self._camera_id} (backend={backend})')
        if not self._cap.isOpened():
            if self._on_gesture:
                self._on_gesture("Camera not available")
            self._running = False
            return

        self._tracker = HandTracker()

        if self._on_gesture:
            self._on_gesture("Gesture control started")

        while self._running:
            if self._paused:
                time.sleep(0.1)
                continue

            ret, frame = self._cap.read()
            if not ret:
                time.sleep(0.05)
                continue

            # Flip for mirror view
            frame = cv2.flip(frame, 1)
            h, w = frame.shape[:2]

            # Detect hands
            hands = self._tracker.process(frame)

            if hands:
                # Draw landmarks on preview
                for hand_landmarks in hands:
                    self._draw_landmarks(frame, hand_landmarks)

                # Recognize gesture from first hand
                gesture = self._recognizer.recognize(hands[0])
                self._last_gesture = gesture

                # Execute action
                if gesture != Gesture.NONE:
                    result = self._executor.execute(gesture)
                    if result == "__TOGGLE__":
                        self._paused = not self._paused
                        status = "paused" if self._paused else "resumed"
                        if self._on_gesture:
                            self._on_gesture(f"Gesture control {status}")
                        continue
                    elif result:
                        self._gesture_count += 1
                        if self._on_gesture:
                            self._on_gesture(result)

                # Show gesture text on preview
                self._draw_gesture_text(frame, gesture, h, w)
            else:
                self._last_gesture = Gesture.NONE

            # Show preview window
            if self._show_preview:
                cv2.imshow("IRA - Hand Gesture Control", frame)
                key = cv2.waitKey(1) & 0xFF
                if key == ord('q') or key == 27:  # q or ESC
                    self._running = False
                    break
                if key == ord('p'):
                    self._paused = not self._paused
            else:
                # Still need waitKey for OpenCV event loop
                cv2.waitKey(1)

        self._close_resources()
        if self._on_gesture:
            self._on_gesture("Gesture control stopped")

    def _draw_landmarks(self, frame, hand_landmarks):
        mp_drawing = mp.solutions.drawing_utils
        mp_drawing.draw_landmarks(
            frame, hand_landmarks, mp.solutions.hands.HAND_CONNECTIONS,
            mp_drawing.DrawingSpec(color=(0, 255, 0), thickness=2, circle_radius=2),
            mp_drawing.DrawingSpec(color=(0, 200, 255), thickness=2),
        )

    def _draw_gesture_text(self, frame, gesture: Gesture, h: int, w: int):
        label = gesture.value.upper().replace("_", " ")
        if self._paused:
            label = "PAUSED"

        # Background bar
        cv2.rectangle(frame, (0, h - 50), (w, h), (0, 0, 0), -1)
        cv2.rectangle(frame, (0, h - 50), (w, h), (0, 200, 255), 2)

        color = (0, 255, 0) if gesture != Gesture.NONE else (100, 100, 100)
        cv2.putText(frame, label, (20, h - 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

        # Camera indicator
        dot_color = (0, 0, 255) if self._paused else (0, 255, 0)
        cv2.circle(frame, (w - 30, 30), 8, dot_color, -1)

        if self._gesture_count > 0:
            cv2.putText(frame, f"Actions: {self._gesture_count}", (w - 160, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

