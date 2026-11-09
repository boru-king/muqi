"""摄像头人脸检测（Haar Cascade）—— 每次检测完立即释放，画面只存在于内存。

隐私契约（需求文档 5.1 / 5.2，用户 2026-11-06 再次强调）：

* **绝不落盘**：不调用 ``cv2.imwrite``、不打开任何输出文件、不把帧缓存到实例属性、
  不把帧写进日志、不把帧作为返回值传出。帧与灰度图都是 :meth:`detect_once`
  的局部变量，函数返回后即失去引用，由引用计数立即回收内存。
* **不上传**：本模块无任何网络调用。
* **只输出布尔结果**：返回 :class:`CameraResult`，仅含「是否有人」、设备可用性、
  耗时、错误文本与取消标记，不含任何图像数据。
* **磁盘只读不写**：唯一读取的磁盘文件是 OpenCV 自带的 Haar 级联 XML
  （静态模型，不是摄像头画面）。

资源契约：

* 检测流程严格为「打开摄像头 → 捕获帧 → 人脸检测 → ``cap.release()`` → 返回 bool」。
* ``cap.release()`` 位于 ``finally``，即使读帧或检测抛异常也必定执行；
  摄像头绝不常驻，单次检测结束即不再持有设备。
* Windows 使用 ``CAP_DSHOW`` 后端加速开关，并把帧降到 320x240 以减少耗时与内存。
* 检测间隔（默认 60 秒）由上层状态机 / 主循环控制，本模块只负责「一次检测」；
  ``cancel_event`` 供「键鼠一旦活动立即关闭摄像头检测」在可中断点提前退出。
"""

import logging
import platform
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

import cv2

from config import default as cfg

logger = logging.getLogger(__name__)

_CASCADE_FILENAME = "haarcascade_frontalface_default.xml"
_DEFAULT_CAMERA_INDEX = 0
_FRAME_WIDTH = 320
_FRAME_HEIGHT = 240
_MAX_READ_ATTEMPTS = 2
_SCALE_FACTOR = 1.1
_MIN_NEIGHBORS = 5
_MIN_FACE_SIZE = (60, 60)


def _elapsed_ms(started: float) -> float:
    """返回自 ``started`` 起经过的毫秒数（monotonic，不受系统时间调整影响）。"""
    return (time.monotonic() - started) * 1000.0


@dataclass(frozen=True)
class CameraResult:
    """单次检测结果——**不含任何图像数据**。

    Attributes:
        face_detected: 是否检测到人脸（屏幕前是否有人）。
        available: 摄像头与检测流程是否可用；False 表示设备故障/异常，
            调用方不应把「不可用」简单等同于「无人」。
        duration_ms: 本次检测耗时（毫秒），用于验证「瞬时开启」。
        error: 失败或取消的原因文本，成功时为空串。
        cancelled: 是否因键鼠恢复活动而在打开设备前取消。
        checked_at: ``time.monotonic()`` 采样时刻。
    """

    face_detected: bool
    available: bool
    duration_ms: float
    error: str = ""
    cancelled: bool = False
    checked_at: float = 0.0


class CameraDetector:
    """按需开启摄像头做一次人脸检测，检测完立即释放设备。

    可以常驻的只有 ``CascadeClassifier``（静态 Haar 模型，约几十 KB），
    **不是摄像头资源**；摄像头每次检测都是 new → read → release 的完整生命周期。
    """

    def __init__(
        self,
        camera_index: int = _DEFAULT_CAMERA_INDEX,
        *,
        cascade_path: Optional[str] = None,
    ) -> None:
        """初始化检测器（此时**不会**打开摄像头）。

        Args:
            camera_index: 摄像头设备序号，默认 0。
            cascade_path: Haar 级联 XML 路径；None 时自动解析
                （优先 OpenCV 自带数据，打包后回退到 ``assets/``）。
        """
        self._camera_index = camera_index
        self._cascade_path = cascade_path
        self._classifier: Optional[Any] = None
        self._classifier_lock = threading.Lock()
        # 防止两个线程同时打开同一摄像头（设备独占，第二次会打开失败）。
        self._capture_lock = threading.Lock()

    def _resolve_cascade_path(self) -> str:
        """解析 Haar 级联 XML 的实际路径。

        Returns:
            存在的文件路径。

        Raises:
            FileNotFoundError: 自带数据与 ``assets/`` 都找不到 XML。
        """
        candidates = []
        if self._cascade_path:
            candidates.append(self._cascade_path)
        else:
            try:
                candidates.append(str(Path(cv2.data.haarcascades) / _CASCADE_FILENAME))
            except AttributeError:
                # OpenCV 5.x 已移除 cv2.data；此时依赖 PyInstaller 打包进 assets/ 的副本。
                pass
            candidates.append(str(cfg.ASSETS_DIR / _CASCADE_FILENAME))
        for path in candidates:
            if Path(path).is_file():
                return path
        raise FileNotFoundError("找不到 Haar 级联文件，已尝试: %s" % ", ".join(candidates))

    def _get_classifier(self) -> Any:
        """延迟加载并复用 Haar 分类器（只解析一次 XML，避免每次检测重复 IO）。

        Returns:
            已加载的 ``cv2.CascadeClassifier``。

        Raises:
            RuntimeError: 级联文件加载失败。
        """
        with self._classifier_lock:
            if self._classifier is None:
                path = self._resolve_cascade_path()
                classifier = cv2.CascadeClassifier(path)
                if classifier.empty():
                    raise RuntimeError("Haar 级联文件加载失败: %s" % path)
                self._classifier = classifier
            return self._classifier

    def _open_camera(self) -> Any:
        """打开摄像头并设置为低分辨率、单帧缓冲。

        Returns:
            ``cv2.VideoCapture`` 对象；调用方必须在 ``finally`` 中 ``release()``。
        """
        # Windows 上 MSMF 后端打开慢且可能卡住，CAP_DSHOW 明显更快。
        backend = cv2.CAP_DSHOW if platform.system() == "Windows" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self._camera_index, backend)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, _FRAME_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, _FRAME_HEIGHT)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 部分后端不支持，返回 False 可忽略
        return cap

    @staticmethod
    def _read_frame(cap: Any) -> Tuple[bool, Any]:
        """最多尝试两次读取，取第一张有效帧。

        Args:
            cap: 已打开的 ``VideoCapture``。

        Returns:
            ``(是否成功, 帧或 None)``；帧仅在内存中，不做任何持久化。
        """
        for _ in range(_MAX_READ_ATTEMPTS):
            ok, frame = cap.read()
            if ok and frame is not None and getattr(frame, "size", 0) > 0:
                return True, frame
        return False, None

    @staticmethod
    def detect_frame(frame: Any, classifier: Any) -> bool:
        """对内存中的单帧做人脸检测，只返回布尔结果。

        Args:
            frame: BGR 图像（``numpy.ndarray``），由调用方在内存中持有。
            classifier: ``cv2.CascadeClassifier`` 实例。

        Returns:
            检测到至少一张人脸返回 True。

        Raises:
            cv2.error: OpenCV 处理失败，由 :meth:`detect_once` 统一兜底。
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)  # 均衡化，降低室内弱光下的漏检
        faces = classifier.detectMultiScale(
            gray,
            scaleFactor=_SCALE_FACTOR,
            minNeighbors=_MIN_NEIGHBORS,
            minSize=_MIN_FACE_SIZE,
        )
        detected = len(faces) > 0
        # 只保留一个 bool；灰度图与检测结果都不外泄、不落盘。
        del gray, faces
        return detected

    def detect_once(
        self, cancel_event: Optional[threading.Event] = None
    ) -> CameraResult:
        """执行一次完整检测：打开 → 捕帧 → 检测 → **释放** → 返回 bool。

        Args:
            cancel_event: 键鼠恢复活动时置位的事件；在打开设备前后两个可中断点检查，
                置位则跳过本次检测（对应「键鼠有活动立即关闭摄像头检测」）。

        Returns:
            :class:`CameraResult`；任何异常都转换为 ``available=False`` 的结果返回，
            不会让后台计时线程因摄像头问题崩溃。
        """
        started = time.monotonic()
        if cancel_event is not None and cancel_event.is_set():
            return CameraResult(False, True, 0.0, "", True, started)

        cap = None
        frame = None
        try:
            classifier = self._get_classifier()
            with self._capture_lock:
                cap = self._open_camera()
                if cap is None or not cap.isOpened():
                    return CameraResult(
                        False, False, _elapsed_ms(started), "摄像头无法打开", False, started
                    )
                ok, frame = self._read_frame(cap)
                if not ok or frame is None:
                    return CameraResult(
                        False, False, _elapsed_ms(started), "未捕获到有效帧", False, started
                    )

            if cancel_event is not None and cancel_event.is_set():
                return CameraResult(False, True, _elapsed_ms(started), "", True, started)

            detected = self.detect_frame(frame, classifier)
            return CameraResult(detected, True, _elapsed_ms(started), "", False, started)

        except Exception as exc:  # noqa: BLE001 - 后台线程必须吞掉设备异常
            # 只记录异常文本，绝不记录帧内容。
            logger.warning("摄像头检测失败: %s", exc)
            return CameraResult(
                False, False, _elapsed_ms(started), str(exc), False, started
            )
        finally:
            # 无论成功、失败、异常、取消，设备与内存都在此释放。
            if cap is not None:
                try:
                    cap.release()
                except Exception:  # noqa: BLE001 - 释放失败也要继续清理内存
                    logger.exception("释放摄像头时发生异常")
            # 帧从未赋给 self、从未写入文件；此处主动解除引用，让 ndarray 立即回收。
            frame = None
