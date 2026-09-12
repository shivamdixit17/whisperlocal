"""
System audio capture through a CoreAudio process tap.

macOS 14.2 added process taps: a virtual audio source that carries what other
processes are playing. Put one inside an aggregate device together with the
microphone and the result is an ordinary input device with the mic on the
first channels and everyone else on the last two — which PortAudio can open
like any other device, so meeting capture reuses the same "drain from a thread
we own" recorder as push-to-talk. No Python ever runs on a realtime thread.

Creating the tap is what triggers the "System Audio Recording Only" prompt.
It is only ever attempted when a meeting recording starts, so someone who
never records a meeting never sees it.

Two layers here:

  * ctypes against CoreAudio.framework for property *reads* — the PyObjC
    bridging of AudioObjectGetPropertyData is broken for these calls, and
    ctypes is fine for fixed-size scalars and CFStrings.
  * PyObjC (pyobjc-framework-CoreAudio) for CATapDescription and the two
    create/destroy calls, which are only wrapped there.
"""

from __future__ import annotations

import ctypes
import platform
import struct
import time
import uuid
from dataclasses import dataclass

# ─── ctypes property reads ───────────────────────────────────────────────────────

_ca = None
_cf = None


def _libs():
    global _ca, _cf
    if _ca is None:
        _ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        _cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        _cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        _cf.CFStringGetCString.restype = ctypes.c_bool
        _cf.CFRelease.argtypes = [ctypes.c_void_p]
    return _ca, _cf


def fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode("ascii"))[0]


def fourcc_str(value: int) -> str:
    """Render an OSStatus the way CoreAudio spells them ('nope', '!hog')."""
    try:
        raw = struct.pack(">I", value & 0xFFFFFFFF).decode("ascii")
        if all(32 <= ord(c) < 127 for c in raw):
            return raw
    except Exception:
        pass
    return str(value)


class CoreAudioError(RuntimeError):
    def __init__(self, what: str, status: int):
        self.status = status
        self.code = fourcc_str(status)
        super().__init__(f"{what}: CoreAudio error {self.code} ({status})")


class _Address(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]


SYSTEM_OBJECT = 1
_GLOBAL = fourcc("glob")
_UTF8 = 0x08000100

# Selectors (values confirmed against pyobjc-framework-CoreAudio's constants).
SEL_DEFAULT_INPUT = "dIn "
SEL_DEFAULT_OUTPUT = "dOut"
SEL_DEVICE_UID = "uid "
SEL_NAME = "lnam"
SEL_RUNNING_SOMEWHERE = "gone"
SEL_NOMINAL_RATE = "nsrt"
SEL_PROCESS_LIST = "prs#"
SEL_PROCESS_BUNDLE = "pbid"
SEL_PROCESS_PID = "ppid"
SEL_PROCESS_RUNNING_INPUT = "piri"
SEL_PROCESS_RUNNING_OUTPUT = "piro"
SEL_PID_TO_PROCESS = "id2p"
SEL_TAP_FORMAT = "tfmt"


def _addr(selector: str) -> _Address:
    return _Address(fourcc(selector), _GLOBAL, 0)


def get_u32(obj: int, selector: str) -> int:
    ca, _ = _libs()
    addr = _addr(selector)
    size = ctypes.c_uint32(4)
    out = ctypes.c_uint32()
    st = ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(out))
    if st:
        raise CoreAudioError(f"read {selector!r}", st)
    return out.value


def get_i32(obj: int, selector: str) -> int:
    ca, _ = _libs()
    addr = _addr(selector)
    size = ctypes.c_uint32(4)
    out = ctypes.c_int32()
    st = ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(out))
    if st:
        raise CoreAudioError(f"read {selector!r}", st)
    return out.value


def get_f64(obj: int, selector: str) -> float:
    ca, _ = _libs()
    addr = _addr(selector)
    size = ctypes.c_uint32(8)
    out = ctypes.c_double()
    st = ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(out))
    if st:
        raise CoreAudioError(f"read {selector!r}", st)
    return out.value


def get_str(obj: int, selector: str) -> str | None:
    ca, cf = _libs()
    addr = _addr(selector)
    size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
    ref = ctypes.c_void_p()
    st = ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(ref))
    if st or not ref.value:
        return None
    buf = ctypes.create_string_buffer(1024)
    ok = cf.CFStringGetCString(ref, buf, 1024, _UTF8)
    cf.CFRelease(ref)
    return buf.value.decode("utf-8", "replace") if ok else None


def get_u32_list(obj: int, selector: str) -> list[int]:
    ca, _ = _libs()
    addr = _addr(selector)
    size = ctypes.c_uint32()
    st = ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size))
    if st:
        raise CoreAudioError(f"size of {selector!r}", st)
    n = size.value // 4
    if n == 0:
        return []
    buf = (ctypes.c_uint32 * n)()
    st = ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), buf)
    if st:
        raise CoreAudioError(f"read {selector!r}", st)
    return list(buf)


def translate_pid(pid: int) -> int | None:
    """The AudioObjectID for a process, or None if it has no audio object."""
    ca, _ = _libs()
    addr = _addr(SEL_PID_TO_PROCESS)
    qual = ctypes.c_int32(pid)
    size = ctypes.c_uint32(4)
    out = ctypes.c_uint32()
    st = ca.AudioObjectGetPropertyData(
        SYSTEM_OBJECT, ctypes.byref(addr), 4, ctypes.byref(qual), ctypes.byref(size), ctypes.byref(out)
    )
    return None if st or not out.value else out.value


# ─── Devices ─────────────────────────────────────────────────────────────────────


def default_input_device() -> int:
    return get_u32(SYSTEM_OBJECT, SEL_DEFAULT_INPUT)


def default_output_device() -> int:
    return get_u32(SYSTEM_OBJECT, SEL_DEFAULT_OUTPUT)


def device_uid(device_id: int) -> str | None:
    return get_str(device_id, SEL_DEVICE_UID)


def device_name(device_id: int) -> str | None:
    return get_str(device_id, SEL_NAME)


def default_input_uid() -> str | None:
    try:
        return device_uid(default_input_device())
    except CoreAudioError:
        return None


def device_is_running_somewhere(device_id: int) -> bool:
    """Whether any process has this device running — the "orange dot" signal
    for the default input, coarse but available on every macOS version."""
    try:
        return bool(get_u32(device_id, SEL_RUNNING_SOMEWHERE))
    except CoreAudioError:
        return False


def device_sample_rate(device_id: int) -> float | None:
    try:
        return get_f64(device_id, SEL_NOMINAL_RATE)
    except CoreAudioError:
        return None


# ─── Processes ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProcessAudioState:
    object_id: int
    pid: int
    bundle_id: str
    running_input: bool
    running_output: bool


def process_objects() -> list[ProcessAudioState]:
    """Every process CoreAudio knows about and whether it has input/output
    running right now. Per-process, so a meeting app with the mic open is
    told apart from a music player. Cheap: ~1 ms for a few dozen entries."""
    out: list[ProcessAudioState] = []
    try:
        ids = get_u32_list(SYSTEM_OBJECT, SEL_PROCESS_LIST)
    except CoreAudioError:
        return out
    for obj in ids:
        try:
            pid = get_i32(obj, SEL_PROCESS_PID)
            bundle = get_str(obj, SEL_PROCESS_BUNDLE) or ""
            inp = bool(get_u32(obj, SEL_PROCESS_RUNNING_INPUT))
            outp = bool(get_u32(obj, SEL_PROCESS_RUNNING_OUTPUT))
        except CoreAudioError:
            continue
        out.append(ProcessAudioState(obj, pid, bundle, inp, outp))
    return out


# ─── Availability ────────────────────────────────────────────────────────────────


def macos_version() -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in platform.mac_ver()[0].split(".") if p.isdigit())
    except Exception:
        return ()


def is_available() -> tuple[bool, str]:
    """Whether a process tap can be created on this Mac at all."""
    ver = macos_version()
    if ver and ver < (14, 2):
        return False, f"macOS {'.'.join(map(str, ver))} — process taps need 14.2 or later"
    try:
        import CoreAudio  # noqa: F401  (pyobjc-framework-CoreAudio)
    except ImportError:
        return False, "pyobjc-framework-CoreAudio is not installed"
    if not hasattr(__import__("CoreAudio"), "AudioHardwareCreateProcessTap"):
        return False, "this PyObjC build does not expose AudioHardwareCreateProcessTap"
    return True, "ok"


# ─── Tap and aggregate ───────────────────────────────────────────────────────────

# Set when a tap creation is refused, so the Permissions menu can point at the
# right System Settings pane. Cleared on the next success.
last_denial: str | None = None


class ProcessTap:
    """A process tap object. create() is what triggers the TCC prompt."""

    def __init__(self, *, exclude_pids: list[int] = (), only_pids: list[int] | None = None,
                 name: str = "WhisperLocal system audio"):
        self.exclude_pids = list(exclude_pids)
        self.only_pids = list(only_pids) if only_pids is not None else None
        self.name = name
        self.tap_id: int | None = None
        self._desc = None
        self.uid: str | None = None

    def create(self) -> int:
        global last_denial
        import CoreAudio

        desc = CoreAudio.CATapDescription.alloc()
        if self.only_pids is not None:
            objs = [o for o in (translate_pid(p) for p in self.only_pids) if o]
            desc = desc.initStereoMixdownOfProcesses_(objs)
        else:
            objs = [o for o in (translate_pid(p) for p in self.exclude_pids) if o]
            desc = desc.initStereoGlobalTapButExcludeProcesses_(objs)
        desc.setName_(self.name)
        desc.setPrivate_(True)
        desc.setMuteBehavior_(0)  # keep playing through the speakers as normal
        tap_uuid = uuid.uuid4()
        try:
            from Foundation import NSUUID

            desc.setUUID_(NSUUID.alloc().initWithUUIDString_(str(tap_uuid)))
        except Exception:
            pass

        status, tap_id = CoreAudio.AudioHardwareCreateProcessTap(desc, None)
        if status or not tap_id:
            last_denial = fourcc_str(status)
            raise CoreAudioError("create process tap", status)
        last_denial = None
        self._desc = desc
        self.tap_id = tap_id
        self.uid = str(desc.UUID().UUIDString())
        return tap_id

    def format(self) -> tuple[float, int] | None:
        """(sample_rate, channels) of the tap's stream."""
        if not self.tap_id:
            return None
        ca, _ = _libs()
        addr = _addr(SEL_TAP_FORMAT)
        size = ctypes.c_uint32(40)
        buf = ctypes.create_string_buffer(40)
        st = ca.AudioObjectGetPropertyData(self.tap_id, ctypes.byref(addr), 0, None, ctypes.byref(size), buf)
        if st:
            return None
        # AudioStreamBasicDescription: Float64 rate, UInt32 x8 ...
        rate, = struct.unpack_from("<d", buf.raw, 0)
        channels, = struct.unpack_from("<I", buf.raw, 28)
        return rate, channels

    def destroy(self) -> None:
        if self.tap_id:
            try:
                import CoreAudio

                CoreAudio.AudioHardwareDestroyProcessTap(self.tap_id)
            except Exception:
                pass
        self.tap_id = None
        self._desc = None


class AggregateDevice:
    """Microphone + tap as one input device. Private, so it never shows up in
    System Settings or other apps' device lists."""

    NAME = "WhisperLocal Meeting Capture"

    def __init__(self, mic_uid: str, tap: ProcessTap | None, *, name: str | None = None):
        self.mic_uid = mic_uid
        self.tap = tap
        self.name = name or self.NAME
        self.uid = f"com.shivamdixit.whisperlocal.meeting.{uuid.uuid4().hex[:8]}"
        self.device_id: int | None = None

    def create(self) -> int:
        import CoreAudio

        spec: dict = {
            "uid": self.uid,
            "name": self.name,
            "private": True,
            "stacked": False,
            "subdevices": [{"uid": self.mic_uid, "drift": False}],
            "master": self.mic_uid,
        }
        if self.tap and self.tap.uid:
            spec["taps"] = [{"uid": self.tap.uid, "drift": True}]
            spec["tapautostart"] = True
        status, device_id = CoreAudio.AudioHardwareCreateAggregateDevice(spec, None)
        if status or not device_id:
            raise CoreAudioError("create aggregate device", status)
        self.device_id = device_id
        # The device appears asynchronously; give it a beat before anyone lists it.
        time.sleep(0.2)
        return device_id

    def destroy(self) -> None:
        if self.device_id:
            try:
                import CoreAudio

                CoreAudio.AudioHardwareDestroyAggregateDevice(self.device_id)
            except Exception:
                pass
        self.device_id = None


@dataclass
class CaptureDevice:
    """What MeetingCapture opens: a PortAudio device index and its layout."""

    portaudio_index: int
    samplerate: int
    channels: int
    mic_channels: int
    has_system: bool
    label: str


def find_portaudio_index(name: str) -> int | None:
    """Locate a device by (exact or prefix) name in PortAudio's list."""
    import sounddevice

    for idx, dev in enumerate(sounddevice.query_devices()):
        dname = str(dev.get("name", ""))
        if dev.get("max_input_channels", 0) > 0 and (dname == name or dname.startswith(name)):
            return idx
    return None
