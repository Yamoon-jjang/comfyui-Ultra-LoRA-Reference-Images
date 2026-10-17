import json
import math
import os
import posixpath
import re
import threading
import uuid
from datetime import datetime, timedelta

import numpy as np
import torch
from PIL import Image, ImageFilter
from PIL.PngImagePlugin import PngInfo

import comfy.samplers
import folder_paths
import nodes
from comfy_execution.graph_utils import GraphBuilder

try:
    from comfy.cli_args import args as comfy_args
except Exception:  # pragma: no cover
    comfy_args = None

try:
    from aiohttp import web
    from server import PromptServer
except Exception:  # pragma: no cover - allows standalone source validation
    web = None
    PromptServer = None


class _AnyType(str):
    """ComfyUI wildcard type used only for dynamic LoRA widget inputs."""

    def __ne__(self, other: object) -> bool:
        return False


class _FlexibleOptionalInputs(dict):
    """Accept dynamic optional widget names while keeping server validation."""

    def __init__(self, value_type: str, initial: dict | None = None):
        self.value_type = value_type
        self.initial = initial or {}
        super().__init__(self.initial)

    def __getitem__(self, key):
        if key in self.initial:
            return self.initial[key]
        return (self.value_type,)

    def __contains__(self, key):
        return True


ANY_TYPE = _AnyType("*")
LORA_ORDER_MISSING = 2**31 - 1
LORA_MAX_STRENGTH = 100.0
DEFAULT_WEIGHT_DTYPE = "default"
DEFAULT_TEXT_ENCODER_DEVICE = "default"
QWEN_CLIP_TYPE = "qwen_image"


def _file_choices(folder_name: str, empty_label: str) -> tuple[str, ...]:
    values = tuple(folder_paths.get_filename_list(folder_name))
    return values if values else (empty_label,)


def _is_placeholder_choice(value: str) -> bool:
    text = str(value or "").strip()
    return text.startswith("<") and text.endswith(">")


def _preferred_choice(folder_name: str, candidates: tuple[str, ...], empty_label: str) -> tuple[str, tuple[str, ...]]:
    values = _file_choices(folder_name, empty_label)
    lowered = {str(value).lower(): value for value in values}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()], values
    return values[0], values


def _require_available_file(folder_name: str, value: str, label: str) -> None:
    if _is_placeholder_choice(value):
        raise ValueError(f"선택 가능한 {label} 파일이 없습니다.")
    available = set(folder_paths.get_filename_list(folder_name))
    if value not in available:
        raise ValueError(
            f"선택한 {label} 파일을 현재 ComfyUI 목록에서 찾을 수 없습니다: {value}. "
            f"모델 목록을 새로고침하거나 파일 위치를 확인하십시오."
        )


def _require_core_nodes(*class_names: str) -> None:
    missing = [name for name in class_names if nodes.NODE_CLASS_MAPPINGS.get(name) is None]
    if missing:
        raise RuntimeError(
            "현재 ComfyUI에서 필요한 기본 노드를 찾을 수 없습니다: " + ", ".join(missing) +
            ". ComfyUI를 업데이트한 뒤 다시 시도하십시오."
        )


def _parse_lora_widget(key: str, value, model_variant: str | None = None):
    if not str(key).lower().startswith("lora_") or not isinstance(value, dict):
        return None
    if not bool(value.get("on", True)):
        return None

    row_variant = str(value.get("model_variant", "") or "").strip()
    if model_variant and row_variant and row_variant != model_variant:
        return None

    lora_name = str(value.get("lora", "") or "").strip()
    if not lora_name or lora_name.lower() == "none":
        return None

    try:
        strength = float(value.get("strength", 1.0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"LoRA 강도가 올바르지 않습니다: {lora_name}") from exc
    if not math.isfinite(strength):
        raise ValueError(f"LoRA 강도는 유한한 수여야 합니다: {lora_name}")
    if abs(strength) > LORA_MAX_STRENGTH:
        raise ValueError(
            f"LoRA 강도는 -{int(LORA_MAX_STRENGTH)}~{int(LORA_MAX_STRENGTH)} 범위여야 합니다: {lora_name}"
        )
    if strength == 0.0:
        return None

    try:
        order = int(value.get("order", LORA_ORDER_MISSING))
    except (TypeError, ValueError):
        order = LORA_ORDER_MISSING
    return order, str(key), lora_name, strength


def _collect_lora_rows(values: dict, model_variant: str | None = None):
    parsed = []
    for position, (key, value) in enumerate(values.items()):
        item = _parse_lora_widget(key, value, model_variant=model_variant)
        if item is not None:
            parsed.append((item, position))

    if len(parsed) <= 1:
        return [item for item, _ in parsed]

    has_explicit_order = [item[0] != LORA_ORDER_MISSING for item, _ in parsed]
    if all(has_explicit_order):
        parsed.sort(key=lambda entry: (entry[0][0], entry[1]))
    elif not any(has_explicit_order):
        numeric_keys = []
        for item, _ in parsed:
            match = re.fullmatch(r"lora_(\d+)", item[1], flags=re.IGNORECASE)
            if not match:
                numeric_keys = []
                break
            numeric_keys.append(int(match.group(1)))
        if numeric_keys:
            parsed.sort(
                key=lambda entry: (
                    int(re.fullmatch(r"lora_(\d+)", entry[0][1], flags=re.IGNORECASE).group(1)),
                    entry[1],
                )
            )
    return [item for item, _ in parsed]


# ---------------------------------------------------------------------------
# Windows 예약 파일명 (대소문자 무관)
# ---------------------------------------------------------------------------
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

# 파일시스템별 255-byte/character 제한보다 충분한 여유를 둔다.
_MAX_FILENAME_CHARS = 160
_MAX_FILENAME_UTF8_BYTES = 180
_PROGRESS_DIRNAME = ".ultra_lora_reference_progress"
_INCOMPLETE_FILENAME = "_INCOMPLETE.txt"

# 파일명 공통 sanitize — UltraExactSaveImage / UltraVariantGenerateSave 공유
def _sanitize_filename(value: str, label: str = "저장명") -> str:
    """
    Windows/Linux 양쪽에서 안전한 파일명 stem을 반환한다.
    - lora_dataset/ / lora_ref/ prefix 제거
    - 출력 확장자(.png/.txt) 1개 제거
    - Windows 금지 문자와 제어 문자 치환
    - trailing dot / space 제거
    - 확장자가 붙은 Windows 예약 이름까지 차단
    - 파일시스템 한계보다 보수적인 길이 제한
    - 빈 문자열 / . / .. 차단
    """
    text = str(value).strip().replace("\\", "/")
    for prefix in ("lora_dataset/", "lora_ref/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    text = posixpath.basename(text)
    if text.lower().endswith((".png", ".txt")):
        text = text[:-4]
    # Windows 금지 문자 + ASCII 제어문자 치환
    text = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", text)
    # trailing dot / space 제거
    text = text.rstrip(" .")
    # 앞뒤 언더스코어 정리
    text = text.strip("_")
    if not text or text in {".", ".."}:
        raise ValueError(f"{label}이 올바르지 않습니다.")

    # Windows는 확장자가 붙어도 첫 번째 이름 부분이 예약 장치명이면 사용할 수 없다.
    reserved_base = text.split(".", 1)[0].rstrip(" .").upper()
    if reserved_base in _WIN_RESERVED:
        raise ValueError(f"{label} '{text}'는 Windows 예약 이름이라 사용할 수 없습니다.")

    utf8_length = len(text.encode("utf-8"))
    if len(text) > _MAX_FILENAME_CHARS or utf8_length > _MAX_FILENAME_UTF8_BYTES:
        raise ValueError(
            f"{label}이 너무 깁니다. 최대 {_MAX_FILENAME_CHARS}자이면서 "
            f"UTF-8 {_MAX_FILENAME_UTF8_BYTES}바이트 이하여야 합니다."
        )
    return text


def _atomic_write_text(path: str, text: str) -> None:
    """같은 폴더의 임시 파일에 쓴 뒤 원자적으로 교체한다."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    temp_path = os.path.join(folder, f".{os.path.basename(path)}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def _atomic_write_json(path: str, payload) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _atomic_save_png(img: Image.Image, path: str, pnginfo=None) -> None:
    """불완전한 PNG가 최종 파일명으로 남지 않도록 원자적으로 저장한다."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    temp_path = os.path.join(folder, f".{os.path.basename(path)}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp_path, "wb") as f:
            img.save(f, format="PNG", pnginfo=pnginfo, compress_level=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def _parse_dataset_plan(value: str) -> list[str]:
    """내부 저장 노드가 받는 예상 변형 목록을 엄격하게 검증한다."""
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("내부 데이터셋 저장 계획을 읽을 수 없습니다.") from exc

    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("내부 데이터셋 저장 계획 형식이 올바르지 않습니다.")
    expected = data.get("expected_stems")
    if not isinstance(expected, list) or not expected or len(expected) > 512:
        raise ValueError("내부 데이터셋 저장 계획의 변형 목록이 올바르지 않습니다.")

    cleaned = []
    seen = set()
    for index, item in enumerate(expected, start=1):
        if not isinstance(item, str):
            raise ValueError(f"내부 데이터셋 저장 계획의 {index}번 파일명이 문자열이 아닙니다.")
        stem = _sanitize_filename(item, f"내부 데이터셋 저장 계획 {index}번 파일명")
        key = stem.casefold()
        if key in seen:
            raise ValueError(f"내부 데이터셋 저장 계획의 파일명이 중복되었습니다: {stem!r}")
        seen.add(key)
        cleaned.append(stem)
    return cleaned


def _ensure_incomplete_marker(full_output_folder: str) -> None:
    marker_path = os.path.join(full_output_folder, _INCOMPLETE_FILENAME)
    text = (
        "이 폴더는 아직 생성 중이거나 작업이 중단된 데이터셋입니다.\n"
        "모든 변형 저장이 정상 완료되면 이 파일과 내부 진행 폴더는 자동으로 제거됩니다.\n"
    )
    try:
        with open(marker_path, "x", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        pass


def _write_progress_marker(full_output_folder: str, stem: str, files: list[str]) -> None:
    progress_dir = os.path.join(full_output_folder, _PROGRESS_DIRNAME)
    os.makedirs(progress_dir, exist_ok=True)
    payload = {"schema_version": 1, "stem": stem, "files": files}
    _atomic_write_json(os.path.join(progress_dir, f"{stem}.json"), payload)


def _try_finalize_dataset(full_output_folder: str, date_str: str, expected_stems: list[str]) -> None:
    """모든 변형 저장을 확인한 뒤 내부 진행 표시만 정리한다."""
    del date_str  # 기존 내부 호출 계약을 유지하되 완료 파일은 생성하지 않는다.
    progress_dir = os.path.join(full_output_folder, _PROGRESS_DIRNAME)

    for stem in expected_stems:
        marker_path = os.path.join(progress_dir, f"{stem}.json")
        try:
            with open(marker_path, "r", encoding="utf-8") as f:
                marker = json.load(f)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return

        if not isinstance(marker, dict) or marker.get("schema_version") != 1 or marker.get("stem") != stem:
            return
        files = marker.get("files")
        if not isinstance(files, list) or not files:
            return

        for filename in files:
            if not isinstance(filename, str) or os.path.basename(filename) != filename:
                return
            file_path = os.path.join(full_output_folder, filename)
            try:
                if not os.path.isfile(file_path) or os.path.getsize(file_path) <= 0:
                    return
            except OSError:
                return

    # 모든 PNG/TXT가 확인된 정상 완료 시에만 내부 진행 표시를 정리한다.
    # 진행 폴더 정리가 끝난 뒤 마지막에 _INCOMPLETE.txt를 제거하여 상태 불일치를 막는다.
    for stem in expected_stems:
        try:
            os.remove(os.path.join(progress_dir, f"{stem}.json"))
        except FileNotFoundError:
            pass
        except OSError:
            return

    try:
        os.rmdir(progress_dir)
    except FileNotFoundError:
        pass
    except OSError:
        return

    # 이전 수정본이 같은 폴더에 남긴 완료 manifest가 있으면 제거한다.
    try:
        os.remove(os.path.join(full_output_folder, "dataset_manifest.json"))
    except FileNotFoundError:
        pass
    except OSError:
        return

    try:
        os.remove(os.path.join(full_output_folder, _INCOMPLETE_FILENAME))
    except FileNotFoundError:
        pass


def _normalize_caption_fragment(value: str) -> str:
    """
    TXT 캡션 조각(trigger / subject / caption)을 보수적으로 정리한다.
    의미 있는 문자(_ - . 등)는 보존하고, 실수로 생기기 쉬운 중복 쉼표와 공백만 정리한다.
    """
    text = "" if value is None else str(value)
    text = text.replace("\x00", "").strip()

    # 전체를 감싼 따옴표만 제거: "a korean woman" -> a korean woman
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()

    # 줄바꿈/탭/연속 공백을 한 칸으로 정리
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text)

    # 쉼표 주변 공백, 중복 쉼표, 앞뒤 쉼표 정리
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"(?:,\s*){2,}", ", ", text)
    text = re.sub(r"^(?:,\s*)+|(?:,\s*)+$", "", text)

    return text.strip()


# ---------------------------------------------------------------------------
# 기본 variants JSON (24개)
# ---------------------------------------------------------------------------
DEFAULT_VARIANTS_JSON = json.dumps([
    {
        "filename": "01_front_neutral",
        "prompt": "Make the person face directly toward the camera with a neutral expression.",
        "caption": "front view, neutral expression, looking at viewer"
    },
    {
        "filename": "02_front_smile",
        "prompt": "Make the person face directly toward the camera with a gentle smile.",
        "caption": "front view, gentle smile, looking at viewer"
    },
    {
        "filename": "03_front_laugh",
        "prompt": "Make the person face the camera and laugh with mouth open.",
        "caption": "front view, laughing, open mouth, looking at viewer"
    },
    {
        "filename": "04_front_surprised",
        "prompt": "Make the person face the camera with a surprised expression, raised eyebrows and wide eyes.",
        "caption": "front view, surprised expression, wide eyes, raised eyebrows, looking at viewer"
    },
    {
        "filename": "05_front_crying",
        "prompt": "Make the person face the camera with a crying expression, watery eyes and sad mouth.",
        "caption": "front view, crying expression, watery eyes, looking at viewer"
    },
    {
        "filename": "06_front_wink",
        "prompt": "Make the person face the camera and wink one eye playfully.",
        "caption": "front view, winking, looking at viewer"
    },
    {
        "filename": "07_threequarter_left_neutral",
        "prompt": "Turn the person to a three-quarter left angle with a neutral expression.",
        "caption": "three-quarter left view, neutral expression"
    },
    {
        "filename": "08_threequarter_left_smile",
        "prompt": "Turn the person to a three-quarter left angle with a gentle smile.",
        "caption": "three-quarter left view, gentle smile"
    },
    {
        "filename": "09_threequarter_left_angry",
        "prompt": "Turn the person to a three-quarter left angle with an angry expression and furrowed brows.",
        "caption": "three-quarter left view, angry expression, furrowed brows"
    },
    {
        "filename": "10_threequarter_left_annoyed",
        "prompt": "Turn the person to a three-quarter left angle with an annoyed and displeased expression.",
        "caption": "three-quarter left view, annoyed expression"
    },
    {
        "filename": "11_threequarter_right_neutral",
        "prompt": "Turn the person to a three-quarter right angle with a neutral expression.",
        "caption": "three-quarter right view, neutral expression"
    },
    {
        "filename": "12_threequarter_right_smile",
        "prompt": "Turn the person to a three-quarter right angle with a gentle smile.",
        "caption": "three-quarter right view, gentle smile"
    },
    {
        "filename": "13_threequarter_right_sad",
        "prompt": "Turn the person to a three-quarter right angle with a sad expression and downcast eyes.",
        "caption": "three-quarter right view, sad expression, eyes downcast"
    },
    {
        "filename": "14_threequarter_right_shouting",
        "prompt": "Turn the person to a three-quarter right angle with mouth open as if shouting.",
        "caption": "three-quarter right view, shouting, open mouth"
    },
    {
        "filename": "15_profile_left_neutral",
        "prompt": "Turn the person to a full left profile with a neutral expression.",
        "caption": "left profile view, neutral expression"
    },
    {
        "filename": "16_profile_left_smile",
        "prompt": "Turn the person to a full left profile with a gentle smile.",
        "caption": "left profile view, gentle smile"
    },
    {
        "filename": "17_profile_right_neutral",
        "prompt": "Turn the person to a full right profile with a neutral expression.",
        "caption": "right profile view, neutral expression"
    },
    {
        "filename": "18_profile_right_laugh",
        "prompt": "Turn the person to a full right profile and make them laugh with mouth open.",
        "caption": "right profile view, laughing, open mouth"
    },
    {
        "filename": "19_lookdown_neutral",
        "prompt": "Shoot the person from slightly above, looking downward with a neutral expression.",
        "caption": "high angle view, looking down, neutral expression"
    },
    {
        "filename": "20_lookdown_smile",
        "prompt": "Shoot the person from slightly above, looking downward with a gentle smile.",
        "caption": "high angle view, looking down, gentle smile"
    },
    {
        "filename": "21_lookup_neutral",
        "prompt": "Shoot the person from slightly below, looking upward with a neutral expression.",
        "caption": "low angle view, looking up, neutral expression"
    },
    {
        "filename": "22_lookup_smirk",
        "prompt": "Shoot the person from slightly below, looking upward with a confident smirk.",
        "caption": "low angle view, looking up, smirk, confident expression"
    },
    {
        "filename": "23_thoughtful_lookaway",
        "prompt": "Turn the person slightly to the side, looking away with a thoughtful expression.",
        "caption": "slight side turn, looking away, thoughtful expression"
    },
    {
        "filename": "24_over_shoulder",
        "prompt": "Turn the person mostly away from the camera, slightly rotating the head to reveal the face in an over-the-shoulder angle.",
        "caption": "over-the-shoulder view, slight face turn, looking back"
    }
], ensure_ascii=False, indent=2)


_DATASET_FOLDER_LOCK = threading.Lock()
_LAST_DATASET_FOLDER_TIME = None


def _build_main_dataset_subfolder(count: int, mode: str, now=None) -> str:
    """짧고 사람이 읽기 쉬우며 한 프로세스 안에서 중복되지 않는 폴더명을 만든다."""
    global _LAST_DATASET_FOLDER_TIME
    count_value = int(count)
    if count_value not in (24, 30, 40):
        raise ValueError(f"데이터셋 장수가 올바르지 않습니다: {count_value}")
    mode_slug = {"얼굴전용": "face", "밸런스": "balance"}.get(mode)
    if mode_slug is None:
        raise ValueError(f"데이터셋 모드가 올바르지 않습니다: {mode!r}")

    current = now or datetime.now()
    if now is None:
        # 같은 순간 여러 Queue가 확장되거나 Windows 시계 해상도가 낮아도
        # 날짜·시간 자체를 1마이크로초씩 전진시켜 폴더명이 겹치지 않게 한다.
        with _DATASET_FOLDER_LOCK:
            if _LAST_DATASET_FOLDER_TIME is not None and current <= _LAST_DATASET_FOLDER_TIME:
                current = _LAST_DATASET_FOLDER_TIME + timedelta(microseconds=1)
            _LAST_DATASET_FOLDER_TIME = current
    timestamp = current.strftime("%Y%m%d_%H%M%S_%f")
    return f"{timestamp}_{count_value}_{mode_slug}"


# ---------------------------------------------------------------------------
# UltraExactSaveImage  (내부 전용 — 검색 목록 비표시)
# ---------------------------------------------------------------------------
class UltraExactSaveImage:
    """PNG + 동명 TXT(caption)를 ComfyUI/output/lora_dataset/<날짜시간>_<장수>_<모드>/ 에 저장."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "filename_stem": (
                    "STRING",
                    {"multiline": False, "default": "01_front_neutral", "dynamicPrompts": False},
                ),
                "caption": (
                    "STRING",
                    {"multiline": True, "default": "", "dynamicPrompts": False, "forceInput": True},
                ),
                "date_subfolder": (
                    "STRING",
                    {"multiline": False, "default": "", "dynamicPrompts": False, "forceInput": True},
                ),
                "dataset_plan": (
                    "STRING",
                    {"multiline": False, "default": "", "dynamicPrompts": False, "forceInput": True},
                ),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "save_exact"
    OUTPUT_NODE = True
    CATEGORY = "_internal"

    # 내부 저장 폴더 형식:
    # - 메인 노드: YYYYMMDD_HHMMSS_ffffff_<24|30|40>_<face|balance>
    # - 구형 보조 노드 호환: YYYY-MM-DD_HHMMSS 또는 뒤에 hex8 run id
    _DATE_RE = re.compile(
        r'^(?:\d{8}_\d{6}_\d{6}_(?:24|30|40)_(?:face|balance)|'
        r'\d{4}-\d{2}-\d{2}_\d{6}(?:_[0-9a-f]{8})?)$'
    )

    @classmethod
    def _validate_date_subfolder(cls, value: str) -> str:
        """
        내부 전용 노드이므로 잘못된 값은 조용히 대체하지 않고 예외를 냄.
        expand_and_save()가 항상 올바른 형식을 넣으므로 실사용에서는 통과해야 함.
        """
        cleaned = value.strip().replace("\x00", "")
        if cls._DATE_RE.match(cleaned):
            return cleaned
        raise ValueError(
            f"date_subfolder 형식이 올바르지 않습니다: {cleaned!r}\n"
            "기대 형식: YYYYMMDD_HHMMSS_ffffff_<24|30|40>_<face|balance>"
        )

    def save_exact(self, images, filename_stem, caption="", date_subfolder="", dataset_plan="",
                   prompt=None, extra_pnginfo=None):
        stem = _sanitize_filename(filename_stem, "저장 파일명")
        expected_stems = _parse_dataset_plan(dataset_plan)
        if stem not in expected_stems:
            raise ValueError(f"저장 파일명 {stem!r}이 내부 데이터셋 저장 계획에 없습니다.")

        # CRITICAL-1: 엄격한 형식 검증 — fallback 없음
        date_str = self._validate_date_subfolder((date_subfolder or "").strip())

        output_dir = folder_paths.get_output_directory()
        subfolder = os.path.join("lora_dataset", date_str)
        full_output_folder = os.path.join(output_dir, subfolder)
        os.makedirs(full_output_folder, exist_ok=True)
        _ensure_incomplete_marker(full_output_folder)

        disable_metadata = (
            bool(getattr(comfy_args, "disable_metadata", False)) if comfy_args is not None else False
        )
        ui_images = []
        saved_files = []

        for batch_index, image in enumerate(images):
            arr = np.clip(255.0 * image.cpu().numpy(), 0, 255).astype(np.uint8)
            img = Image.fromarray(arr)

            file_stem = stem if batch_index == 0 else f"{stem}_{batch_index + 1:02d}"

            # PNG 저장
            png_filename = f"{file_stem}.png"
            png_path = os.path.join(full_output_folder, png_filename)

            pnginfo = None
            if not disable_metadata:
                pnginfo = PngInfo()
                if prompt is not None:
                    pnginfo.add_text("prompt", json.dumps(prompt))
                if extra_pnginfo is not None:
                    for key, value in extra_pnginfo.items():
                        pnginfo.add_text(key, json.dumps(value))

            _atomic_save_png(img, png_path, pnginfo=pnginfo)

            # TXT 저장
            saved_files.append(png_filename)

            txt_content = _normalize_caption_fragment(caption)
            if txt_content:
                txt_filename = f"{file_stem}.txt"
                txt_path = os.path.join(full_output_folder, txt_filename)
                _atomic_write_text(txt_path, txt_content)
                saved_files.append(txt_filename)

            ui_images.append({"filename": png_filename, "subfolder": subfolder, "type": "output"})

        # 이 변형의 PNG/TXT가 모두 최종 파일명으로 교체된 뒤에만 진행 완료를 기록한다.
        _write_progress_marker(full_output_folder, stem, saved_files)
        _try_finalize_dataset(full_output_folder, date_str, expected_stems)

        return {"ui": {"images": ui_images}, "result": ()}


# ---------------------------------------------------------------------------
# UltraAspectFitImage  (내부 전용 — 자동 구도별 비율용)
# ---------------------------------------------------------------------------
class UltraAspectFitImage:
    """참조 이미지를 자르지 않고 3:4 또는 2:3 캔버스에 보존 배치한다."""

    ASPECTS = {
        "3:4": (3, 4),
        "2:3": (2, 3),
    }
    MAX_PIXELS = 1_500_000

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "aspect": (list(cls.ASPECTS),),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "fit"
    CATEGORY = "_internal"

    @classmethod
    def _target_size(cls, source_width: int, source_height: int, aspect: str) -> tuple[int, int]:
        if aspect not in cls.ASPECTS:
            raise ValueError(f"지원하지 않는 자동 출력 비율입니다: {aspect!r}")
        if source_width <= 0 or source_height <= 0:
            raise ValueError("참조 이미지 크기가 올바르지 않습니다.")

        ratio_width, ratio_height = cls.ASPECTS[aspect]
        source_pixels = int(source_width) * int(source_height)
        target_pixels = max(64 * 64, min(source_pixels, cls.MAX_PIXELS))

        # 두 비율을 정확히 유지하면서 Qwen VAE에 안전한 32 배수 크기를 만든다.
        # 3:4 -> 96n x 128n, 2:3 -> 64n x 96n
        unit_width = ratio_width * 32
        unit_height = ratio_height * 32
        unit_pixels = unit_width * unit_height
        scale_units = max(1, round((target_pixels / unit_pixels) ** 0.5))
        while (unit_width * scale_units) * (unit_height * scale_units) > cls.MAX_PIXELS:
            scale_units -= 1
            if scale_units <= 1:
                scale_units = 1
                break
        return unit_width * scale_units, unit_height * scale_units

    @staticmethod
    def _fit_one(image: Image.Image, target_width: int, target_height: int) -> Image.Image:
        source = image.convert("RGB")
        source_width, source_height = source.size
        if (source_width, source_height) == (target_width, target_height):
            return source

        # 흐린 cover 배경 위에 전체 원본을 contain 배치한다. 검은 띠나 피사체 잘림 없이
        # 목표 비율의 VAE 입력을 만들기 위한 내부 전처리이며 최종 저장 이미지는 아니다.
        target_ratio = target_width / target_height
        source_ratio = source_width / source_height
        if source_ratio > target_ratio:
            crop_width = max(1, round(source_height * target_ratio))
            crop_left = max(0, (source_width - crop_width) // 2)
            background_source = source.crop(
                (crop_left, 0, min(source_width, crop_left + crop_width), source_height)
            )
        else:
            crop_height = max(1, round(source_width / target_ratio))
            crop_top = max(0, (source_height - crop_height) // 2)
            background_source = source.crop(
                (0, crop_top, source_width, min(source_height, crop_top + crop_height))
            )
        background = background_source.resize(
            (target_width, target_height), Image.Resampling.LANCZOS
        )
        background = background.filter(
            ImageFilter.GaussianBlur(radius=max(8.0, max(target_width, target_height) / 36.0))
        )

        contain_scale = min(target_width / source_width, target_height / source_height)
        foreground_size = (
            max(1, round(source_width * contain_scale)),
            max(1, round(source_height * contain_scale)),
        )
        foreground = source.resize(foreground_size, Image.Resampling.LANCZOS)
        paste_x = (target_width - foreground.width) // 2
        paste_y = (target_height - foreground.height) // 2
        background.paste(foreground, (paste_x, paste_y))
        return background

    def fit(self, image, aspect):
        if not isinstance(image, torch.Tensor) or image.ndim != 4:
            raise ValueError("IMAGE 입력은 [batch, height, width, channels] 텐서여야 합니다.")
        if image.shape[-1] < 3:
            raise ValueError("IMAGE 입력에는 RGB 채널이 필요합니다.")

        source_height = int(image.shape[1])
        source_width = int(image.shape[2])
        target_width, target_height = self._target_size(source_width, source_height, str(aspect))
        device = image.device
        dtype = image.dtype
        output = []
        for frame in image:
            arr = frame[..., :3].detach().float().cpu().numpy()
            arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=0.0)
            arr = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
            fitted = self._fit_one(Image.fromarray(arr), target_width, target_height)
            fitted_arr = np.array(fitted, dtype=np.float32, copy=True) / 255.0
            output.append(torch.from_numpy(fitted_arr))
        return (torch.stack(output, dim=0).to(device=device, dtype=dtype),)


# ---------------------------------------------------------------------------
# UltraVariantGenerateSave
# ---------------------------------------------------------------------------
class UltraVariantGenerateSave:
    """24개 변형을 독립 브랜치로 펼쳐 PNG+TXT를 날짜+시각+runid 폴더에 저장."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "clip": ("CLIP",),
                "vae": ("VAE",),
                "image": ("IMAGE",),
                "negative": ("CONDITIONING",),
                "latent_image": ("LATENT",),
                "trigger": (
                    "STRING",
                    {"multiline": False, "default": "", "dynamicPrompts": False},
                ),
                "subject": (
                    "STRING",
                    {"multiline": False, "default": "a korean woman", "dynamicPrompts": False},
                ),
                "variants_json": (
                    "STRING",
                    {"multiline": True, "default": DEFAULT_VARIANTS_JSON, "dynamicPrompts": False},
                ),
                "seed": (
                    "INT",
                    {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF, "control_after_generate": True},
                ),
                "steps": ("INT", {"default": 8, "min": 1, "max": 10000}),
                "cfg": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 100.0, "step": 0.1}),
                "sampler_name": (comfy.samplers.KSampler.SAMPLERS,),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS,),
                "denoise": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
            }
        }

    RETURN_TYPES = ()
    FUNCTION = "expand_and_save"
    OUTPUT_NODE = True
    CATEGORY = "Ultra/Batch"

    @classmethod
    def IS_CHANGED(cls, model, clip, vae, image, negative, latent_image,
                   trigger, subject, variants_json, seed, steps, cfg,
                   sampler_name, scheduler, denoise):
        # 항상 새 uuid 반환 → 동일 입력 반복 실행 시 캐시 건너뜀
        return uuid.uuid4().hex

    @classmethod
    def VALIDATE_INPUTS(cls, trigger, subject, variants_json, **kwargs):
        # trigger는 선택사항이다. 공백/쉼표만 있는 입력은 빈 trigger로 취급한다.
        _normalize_caption_fragment(trigger)
        if not _normalize_caption_fragment(subject):
            return "subject가 비어 있습니다."
        try:
            cls._parse_variants(variants_json)
        except ValueError as exc:
            return str(exc)
        return True

    @classmethod
    def _parse_variants(cls, variants_json):
        try:
            data = json.loads(variants_json)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"변형 JSON을 읽을 수 없습니다: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno})"
            ) from exc

        if not isinstance(data, list):
            raise ValueError("변형 JSON의 최상위 값은 배열이어야 합니다.")
        if not data:
            raise ValueError("변형 목록이 비어 있습니다.")
        if len(data) > 512:
            raise ValueError("변형은 최대 512개까지 사용할 수 있습니다.")

        variants = []
        # HIGH-1: 대소문자 비민감 중복 검사 (Windows 파일시스템 대응)
        seen_filenames: set[str] = set()

        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(f"{index + 1}번 항목은 객체여야 합니다.")

            prompt = item.get("prompt")
            caption = item.get("caption", "")
            filename = item.get("filename", item.get("name"))

            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError(f"{index + 1}번 항목의 prompt가 비어 있거나 문자열이 아닙니다.")

            # caption 타입 검증
            if caption is None:
                caption = ""
            elif not isinstance(caption, str):
                raise ValueError(
                    f"{index + 1}번 항목의 caption이 문자열이 아닙니다. "
                    f"(현재 타입: {type(caption).__name__})"
                )

            stem = _sanitize_filename(str(filename) if filename is not None else "", f"{index + 1}번 저장명")

            # HIGH-1: casefold 기준 중복 검사
            key = stem.casefold()
            if key in seen_filenames:
                raise ValueError(f"저장명이 중복되었습니다 (대소문자 무관): {stem!r}")
            seen_filenames.add(key)
            variants.append((prompt.strip(), _normalize_caption_fragment(caption), stem))

        return variants

    def expand_and_save(self, model, clip, vae, image, negative, latent_image,
                        trigger, subject, variants_json, seed, steps, cfg,
                        sampler_name, scheduler, denoise):
        variants = self._parse_variants(variants_json)

        trig = _normalize_caption_fragment(trigger)
        subj = _normalize_caption_fragment(subject)
        prefix_parts = [p for p in [trig, subj] if p]
        prefix = ", ".join(prefix_parts)

        # 저장 폴더 충돌 방지용 run_id. 그래프 노드 ID에는 사용하지 않는다.
        run_id = uuid.uuid4().hex[:8]  # 항상 0-9a-f 8자리

        # CRITICAL-1: _DATE_RE 패턴에 맞는 형식으로 생성
        # → _validate_date_subfolder()를 반드시 통과함
        date_str = datetime.now().strftime("%Y-%m-%d_%H%M%S") + "_" + run_id

        prepared_variants = []
        for prompt_text, caption_text, filename_stem in variants:
            if prefix and caption_text:
                full_caption = f"{prefix}, {caption_text}"
            elif prefix:
                full_caption = prefix
            else:
                full_caption = caption_text
            prepared_variants.append(
                (prompt_text, _normalize_caption_fragment(full_caption), filename_stem)
            )

        dataset_plan = json.dumps(
            {
                "schema_version": 1,
                "expected_stems": [item[2] for item in prepared_variants],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        graph = GraphBuilder()

        for index, (prompt_text, full_caption, filename_stem) in enumerate(prepared_variants, start=1):
            # GraphBuilder가 부모 노드 ID/call index prefix를 자동 부여하므로,
            # 내부 ID는 실행마다 같은 인덱스 기반 값이어야 캐시와 부분 실행이 안정적이다.
            node_id = f"{index:03d}"

            encode = graph.node(
                "TextEncodeQwenImageEditPlus",
                id=f"{node_id}_enc",
                clip=clip, vae=vae, image1=image, prompt=prompt_text,
            )
            sampler = graph.node(
                "KSampler",
                id=f"{node_id}_smp",
                model=model, positive=encode.out(0), negative=negative,
                latent_image=latent_image, seed=seed, steps=steps, cfg=cfg,
                sampler_name=sampler_name, scheduler=scheduler, denoise=denoise,
            )
            decode = graph.node(
                "VAEDecode",
                id=f"{node_id}_dec",
                samples=sampler.out(0), vae=vae,
            )

            graph.node(
                "_UltraExactSaveImage",
                id=f"{node_id}_sav",
                images=decode.out(0),
                filename_stem=filename_stem,
                caption=full_caption,
                date_subfolder=date_str,
                dataset_plan=dataset_plan,
            )

        return {"result": (), "expand": graph.finalize()}



# ---------------------------------------------------------------------------
# UltraSmartLoraDatasetGenerateSave  (한국어 UI / Qwen Image Edit 2511 / Qwen Image 2.1)
# ---------------------------------------------------------------------------
class UltraSmartLoraDatasetGenerateSave:
    """
    얼굴전용 / 밸런스 LoRA 레퍼런스 데이터셋을 자동 생성·저장한다.

    설계 원칙
    - 얼굴전용: 얼굴 참조 1장만 사용하고 각도·표정·시선 변화에 집중한다.
    - 밸런스: image1은 상반신~허벅지 위 밸런스 참조, image2는 얼굴 정체성 참조로
      역할을 명확히 분리하여 Qwen Image Edit 2511 또는 Qwen Image 2.1에 전달한다.
    - 밸런스 참조가 없을 때 얼굴 이미지를 조용히 대신 쓰지 않는다.
    - 인물과 배경의 색·명암 대비, 실루엣·머리카락·의상 경계 분리를 모든 변형에 강제한다.
    - 편집 프롬프트와 LoRA 학습 캡션은 서로 다른 문형으로 생성한다.
    - 선택한 모델 계열별로 별도의 내부 LoRA 스택을 적용할 수 있다.
    """

    MODE_OPTIONS = ["얼굴전용", "밸런스"]
    COUNT_OPTIONS = ["24", "30", "40"]
    ASPECT_OPTIONS = ["원본 비율", "자동 구도별"]
    LIGHTING_OPTIONS = ["사용안함", "안정적분산", "다양한분산"]
    OUTFIT_OPTIONS = ["낮음", "보통", "높음"]
    BACKGROUND_OPTIONS = ["단순", "실내실외혼합", "다양화"]
    EXPRESSION_OPTIONS = ["낮음", "보통", "높음"]
    IDENTITY_OPTIONS = ["강함", "균형", "자유"]
    MODEL_VARIANT_2511 = "Qwen Image Edit 2511"
    MODEL_VARIANT_21 = "Qwen Image 2.1"
    MODEL_VARIANTS = [MODEL_VARIANT_2511, MODEL_VARIANT_21]
    MODEL_2511_UNET_CANDIDATES = ("qwen_image_edit_2511_bf16.safetensors", "qwen_image_edit_2511_int8.safetensors", "qwen_image_edit_2511_int8_convrot.safetensors")
    MODEL_2511_TEXT_ENCODER_CANDIDATES = ("qwen_2.5_vl_7b_fp8_scaled.safetensors", "qwen_2.5_vl_7b_bf16.safetensors", "qwen_2.5_vl_7b_int8_convrot.safetensors")
    MODEL_2511_VAE_CANDIDATES = ("qwen_image_vae.safetensors",)
    MODEL_21_UNET_CANDIDATES = ("qwen_image_2.1_bf16.safetensors", "qwen_image_2.1_fp8_scaled.safetensors")
    MODEL_21_TEXT_ENCODER_CANDIDATES = ("qwen3vl_8b_bf16.safetensors", "qwen3vl_8b_fp8_scaled.safetensors", "qwen3vl_4b_bf16.safetensors")
    MODEL_21_VAE_CANDIDATES = ("qwen_image_2.1_vae_bf16.safetensors", "qwen_image_2.1_vae_fp16.safetensors")
    CACHE_DEVICE_OPTIONS = ["auto", "gpu", "cpu", "off"]
    CACHE_DTYPE_OPTIONS = ["default", "int8", "int4"]
    # Qwen Image Edit의 네거티브 컨디셔닝 입력은 유지하되 사용자 입력란은 두지 않는다.
    # 공식 예시처럼 의미 없는 공백 프롬프트를 사용해 양의 편집 명령만 계획에 관여하게 한다.
    EMPTY_NEGATIVE_PROMPT = " "
    EMPTY_NEGATIVE_PROMPT_21 = ""

    MODE_COUNTS = {
        # 얼굴전용은 얼굴·머리·어깨가 충분히 보이는 얼굴 중심 구도만 생성한다.
        "얼굴전용": {
            24: {"close": 24, "upper": 0, "knee": 0, "full": 0},
            30: {"close": 30, "upper": 0, "knee": 0, "full": 0},
            40: {"close": 40, "upper": 0, "knee": 0, "full": 0},
        },
        # 공식 학습 문서는 장수별 고정 비율을 제시하지 않는다. 여기서는 인물 정체성 정보가
        # 작은 전신 이미지에 과도하게 희석되지 않도록 얼굴 35~38%, 상반신 29~30%,
        # 허벅지 위 20~21%, 전신 12~15%의 보수적인 인물 LoRA 구성을 사용한다.
        "밸런스": {
            24: {"close": 9, "upper": 7, "knee": 5, "full": 3},
            30: {"close": 11, "upper": 9, "knee": 6, "full": 4},
            40: {"close": 14, "upper": 12, "knee": 8, "full": 6},
        },
    }

    FACE_FRAMES = [
        ("close-up face portrait", "face_closeup"),
        ("head-and-shoulders portrait", "head_shoulders"),
        ("close head-and-shoulders portrait", "close_head_shoulders"),
    ]

    SHOT_LABELS = {
        "upper": ("upper-body portrait", "upper_body"),
        "knee": ("thighs-up portrait", "thighs_up"),
        "full": ("full-body portrait", "full_body"),
    }

    FACE_ANGLES = [
        ("front-facing", "front"),
        ("slightly turned left", "slight_left"),
        ("slightly turned right", "slight_right"),
        ("three-quarter left", "threequarter_left"),
        ("three-quarter right", "threequarter_right"),
        ("left profile", "left_profile"),
        ("right profile", "right_profile"),
        ("view from above", "high_angle"),
        ("view from below", "low_angle"),
        ("gentle over-the-shoulder face turn", "over_shoulder"),
    ]

    # 얼굴전용의 30장과 40장은 기존 핵심 24장을 그대로 포함하고, 안정적인 정면·약한 회전·
    # 3/4 시점을 우선 추가한다. 프로필·높고 낮은 카메라·오버숄더는 보완 장면으로 제한한다.
    FACE_ONLY_ANGLE_SLUG_PLANS = {
        24: [
            "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right",
            "front", "left_profile", "right_profile", "high_angle", "low_angle",
            "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right",
            "over_shoulder", "front", "left_profile", "right_profile", "slight_left",
            "slight_right", "threequarter_left", "threequarter_right", "over_shoulder",
        ],
        30: [
            "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right",
            "front", "left_profile", "right_profile", "high_angle", "low_angle",
            "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right",
            "over_shoulder", "front", "left_profile", "right_profile", "slight_left",
            "slight_right", "threequarter_left", "threequarter_right", "over_shoulder",
            "front", "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right",
        ],
        40: [
            "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right",
            "front", "left_profile", "right_profile", "high_angle", "low_angle",
            "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right",
            "over_shoulder", "front", "left_profile", "right_profile", "slight_left",
            "slight_right", "threequarter_left", "threequarter_right", "over_shoulder",
            "front", "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right",
            "front", "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right",
            "left_profile", "right_profile", "high_angle", "low_angle",
        ],
    }

    # 밸런스의 얼굴 분기는 정면·약한 회전·3/4를 중심으로 좌우를 대칭 배치한다.
    # 몸 구도에서 이미 포즈와 프레이밍 다양성이 확보되므로 얼굴 분기에는 과격한 오버숄더를 넣지 않는다.
    BALANCED_FACE_ANGLE_SLUG_PLANS = {
        9: [
            "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right",
            "front", "high_angle", "low_angle", "front",
        ],
        11: [
            "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right",
            "front", "slight_left", "slight_right", "high_angle", "low_angle", "front",
        ],
        14: [
            "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right",
            "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right",
            "high_angle", "low_angle", "front", "front",
        ],
    }

    BODY_ANGLES = {
        "upper": [
            ("front-facing", "front"),
            ("slightly turned left", "slight_left"),
            ("slightly turned right", "slight_right"),
            ("three-quarter left", "threequarter_left"),
            ("three-quarter right", "threequarter_right"),
            ("view from above", "high_angle"),
            ("view from below", "low_angle"),
        ],
        "knee": [
            ("front-facing", "front"),
            ("slightly turned left", "slight_left"),
            ("slightly turned right", "slight_right"),
            ("three-quarter left", "threequarter_left"),
            ("three-quarter right", "threequarter_right"),
            ("view from above", "high_angle"),
            ("view from below", "low_angle"),
        ],
        "full": [
            ("front-facing", "front"),
            ("slightly turned left", "slight_left"),
            ("slightly turned right", "slight_right"),
            ("three-quarter left", "threequarter_left"),
            ("three-quarter right", "threequarter_right"),
            ("view from above", "high_angle"),
            ("view from below", "low_angle"),
        ],
    }

    # 짧은 몸 구도 묶음에서도 왼쪽과 오른쪽 수가 어긋나지 않도록 장수별 각도 계획을 고정한다.
    BODY_ANGLE_SLUG_PLANS = {
        3: ["front", "high_angle", "low_angle"],
        4: ["threequarter_left", "threequarter_right", "high_angle", "low_angle"],
        5: ["front", "slight_left", "slight_right", "high_angle", "low_angle"],
        6: ["front", "front", "threequarter_left", "threequarter_right", "high_angle", "low_angle"],
        7: ["front", "slight_left", "slight_right", "threequarter_left", "threequarter_right", "high_angle", "low_angle"],
        8: ["front", "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right", "high_angle", "low_angle"],
        9: ["front", "slight_left", "slight_right", "threequarter_left", "threequarter_right", "front", "high_angle", "low_angle", "front"],
        12: [
            "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right",
            "front", "slight_left", "slight_right", "threequarter_left", "threequarter_right",
            "high_angle", "low_angle",
        ],
    }

    POSES = {
        "upper": [
            ("relaxed upright pose with shoulders level and arms lowered away from the face", "relaxed_upright"),
            ("natural portrait pose with relaxed shoulders and hands away from the face", "natural_relaxed_pose"),
            ("calm standing pose with both arms relaxed and no hand covering the face", "arms_relaxed"),
            ("natural portrait pose with a small weight shift and clear shoulder line", "natural_portrait_pose"),
        ],
        "knee": [
            ("natural standing pose with weight slightly shifted and both arms relaxed", "weight_shift"),
            ("relaxed standing pose with one hand naturally at the side and the face unobstructed", "one_hand_side"),
            ("clear torso pose with hands away from the face and a readable silhouette", "clear_torso_pose"),
            ("upright casual pose with arms resting naturally and legs clearly separated", "upright_casual"),
        ],
        "full": [
            ("natural full-body standing pose with both arms relaxed at the sides", "full_standing"),
            ("relaxed full-body pose with a slight weight shift and feet clearly visible", "full_weight_shift"),
            ("gentle walking stance with hands away from the face and the full body visible", "walking_stance"),
            ("clean standing pose with a readable full-body silhouette", "clean_standing"),
        ],
    }

    # 얼굴 클로즈업은 눈썹·눈·볼·입 모양이 실제로 달라지는 표현을 사용한다.
    # "smile" 계열만 이름을 바꿔 늘리지 않고, 슬픔·큰 웃음·애교·화남처럼
    # 학습 가치가 분명한 표정을 시각적으로 직접 설명한다.
    FACE_EXPRESSIONS = {
        "낮음": [
            ("neutral expression", "neutral"),
            ("gentle smile", "gentle_smile"),
            ("calm expression", "calm"),
            ("soft smile", "soft_smile"),
        ],
        "보통": [
            ("neutral expression", "neutral"),
            ("gentle smile", "gentle_smile"),
            ("calm expression", "calm"),
            ("bright smile with raised cheeks", "bright_smile"),
            ("thoughtful expression with softly focused eyes", "thoughtful"),
            ("confident expression with steady eyes", "confident"),
            ("sad expression with gently lowered brows and slightly downturned lips", "sad"),
            ("big open-mouth laugh with raised cheeks and smiling eyes", "big_laugh"),
            ("playful cute expression with slightly puffed cheeks and a small pout", "playful_cute"),
            ("serious expression with relaxed lips and focused eyes", "serious"),
        ],
        "높음": [
            ("neutral expression", "neutral"),
            ("gentle smile", "gentle_smile"),
            ("calm expression", "calm"),
            ("bright smile with raised cheeks", "bright_smile"),
            ("thoughtful expression with softly focused eyes", "thoughtful"),
            ("confident expression with steady eyes", "confident"),
            ("sad expression with gently lowered brows and slightly downturned lips", "sad"),
            ("big open-mouth laugh with raised cheeks and smiling eyes", "big_laugh"),
            ("playful cute expression with slightly puffed cheeks and a small pout", "playful_cute"),
            ("angry expression with furrowed brows and tightened lips", "angry"),
            ("surprised expression with widened eyes and a slightly open mouth", "surprised"),
            ("playful wink with a small smile", "playful_wink"),
            ("serious expression with relaxed lips and focused eyes", "serious"),
            ("worried expression with raised inner brows and pressed lips", "worried"),
            ("shy closed-mouth smile with softly raised cheeks", "shy_smile"),
            ("determined expression with focused eyes and a firm mouth", "determined"),
        ],
    }

    # 밸런스 몸 구도에서는 표정이 몸·구도 학습을 압도하지 않도록 강도를 낮춘다.
    # 그래도 웃음·슬픔·애교·화남을 일부 포함해 얼굴이 늘 같은 표정으로 고착되지 않게 한다.
    BALANCED_EXPRESSIONS = {
        "낮음": [
            ("neutral expression", "neutral"),
            ("gentle smile", "gentle_smile"),
            ("calm expression", "calm"),
            ("soft smile", "soft_smile"),
        ],
        "보통": [
            ("neutral expression", "neutral"),
            ("gentle smile", "gentle_smile"),
            ("calm expression", "calm"),
            ("bright smile with raised cheeks", "bright_smile"),
            ("thoughtful expression with softly focused eyes", "thoughtful"),
            ("confident expression with steady eyes", "confident"),
            ("sad expression with gently lowered brows and slightly downturned lips", "sad"),
            ("playful cute expression with slightly puffed cheeks and a small pout", "playful_cute"),
        ],
        "높음": [
            ("neutral expression", "neutral"),
            ("gentle smile", "gentle_smile"),
            ("calm expression", "calm"),
            ("bright smile with raised cheeks", "bright_smile"),
            ("thoughtful expression with softly focused eyes", "thoughtful"),
            ("confident expression with steady eyes", "confident"),
            ("sad expression with gently lowered brows and slightly downturned lips", "sad"),
            ("big open-mouth laugh with raised cheeks and smiling eyes", "big_laugh"),
            ("playful cute expression with slightly puffed cheeks and a small pout", "playful_cute"),
            ("angry expression with furrowed brows and tightened lips", "angry"),
            ("surprised expression with widened eyes and a slightly open mouth", "surprised"),
            ("serious expression with relaxed lips and focused eyes", "serious"),
        ],
    }

    # 생성 프롬프트는 표정의 시각적 특징을 자세히 설명하지만,
    # LoRA 학습 TXT 캡션은 결과 개념만 짧게 기록한다.
    EXPRESSION_CAPTIONS = {
        "neutral": "neutral expression",
        "gentle_smile": "gentle smile",
        "calm": "calm expression",
        "soft_smile": "soft smile",
        "bright_smile": "bright smile",
        "thoughtful": "thoughtful expression",
        "confident": "confident expression",
        "sad": "sad expression",
        "big_laugh": "big open-mouth laugh",
        "playful_cute": "playful cute expression",
        "angry": "angry expression",
        "surprised": "surprised expression",
        "playful_wink": "playful wink",
        "serious": "serious expression",
        "worried": "worried expression",
        "shy_smile": "shy smile",
        "determined": "determined expression",
    }

    HEAD_POSES = {
        "upright": (
            "Keep the head upright and level.",
            "head upright and level",
        ),
        "tilt_left": (
            "Tilt the head about 8 degrees toward the left shoulder while keeping both eyes visible.",
            "head tilted about 8 degrees toward the left shoulder",
        ),
        "tilt_right": (
            "Tilt the head about 8 degrees toward the right shoulder while keeping both eyes visible.",
            "head tilted about 8 degrees toward the right shoulder",
        ),
        "chin_up": (
            "Raise the chin slightly while keeping the full face visible.",
            "chin slightly raised",
        ),
        "chin_down": (
            "Lower the chin slightly while keeping both eyes visible.",
            "chin slightly lowered",
        ),
    }

    FACE_HEAD_POSE_PLAN = [
        "upright", "tilt_left", "upright", "tilt_right", "chin_up",
        "upright", "chin_down", "tilt_left", "upright", "tilt_right",
    ]

    # 밸런스 몸 구도는 절반 이상을 정자세로 두고 약한 얼굴 포즈만 간헐적으로 넣는다.
    BALANCED_BODY_HEAD_POSE_PLAN = [
        "upright", "tilt_left", "upright", "tilt_right",
        "upright", "chin_down", "upright", "chin_up",
    ]

    GAZES = [
        ("looking at the camera", "looking_camera"),
        ("looking slightly left", "looking_left"),
        ("looking slightly right", "looking_right"),
        ("looking slightly upward", "looking_up"),
        ("looking slightly downward", "looking_down"),
    ]

    OUTFITS = {
        "낮음": [
            ("a simple blouse", "simple_blouse"),
            ("a plain knit top", "knit_top"),
            ("a casual shirt", "casual_shirt"),
            ("a light cardigan", "cardigan"),
        ],
        "보통": [
            ("a white blouse", "white_blouse"),
            ("a plain knit top", "knit_top"),
            ("a casual t-shirt", "tshirt"),
            ("a casual shirt", "shirt"),
            ("a light cardigan", "cardigan"),
            ("a simple black jacket", "black_jacket"),
            ("a denim jacket", "denim_jacket"),
            ("a simple long coat", "long_coat"),
        ],
        "높음": [
            ("a white blouse", "white_blouse"),
            ("a plain knit top", "knit_top"),
            ("a casual t-shirt", "tshirt"),
            ("a casual shirt", "shirt"),
            ("a light cardigan", "cardigan"),
            ("a simple black jacket", "black_jacket"),
            ("a denim jacket", "denim_jacket"),
            ("a simple long coat", "long_coat"),
            ("a simple dress", "simple_dress"),
            ("a plain hoodie", "hoodie"),
            ("a tailored jacket", "tailored_jacket"),
            ("a short trench coat", "trench_coat"),
        ],
    }

    # 배경은 다양성을 주되, 인물보다 강해지지 않고 색·명암 분리가 가능한 장면만 사용한다.
    BACKGROUNDS = {
        "단순": [
            ("a clean neutral studio background", "neutral_studio"),
            ("a plain light-gray background", "light_gray"),
            ("a plain warm-gray background", "warm_gray"),
            ("a softly blurred simple indoor background", "blurred_indoor"),
        ],
        "실내실외혼합": [
            ("a clean neutral studio background", "neutral_studio"),
            ("a softly blurred simple indoor background", "blurred_indoor"),
            ("a clean room background with minimal objects", "clean_room"),
            ("a softly blurred window-side interior", "window_interior"),
            ("a softly blurred quiet street background", "quiet_street"),
            ("a softly blurred open park background", "open_park"),
        ],
        "다양화": [
            ("a clean neutral studio background", "neutral_studio"),
            ("a softly blurred simple indoor background", "blurred_indoor"),
            ("a clean room background with minimal objects", "clean_room"),
            ("a softly blurred window-side interior", "window_interior"),
            ("a softly blurred quiet street background", "quiet_street"),
            ("a softly blurred open park background", "open_park"),
            ("a minimal cafe interior with no crowd", "minimal_cafe"),
            ("a simple modern hallway background", "modern_hallway"),
        ],
    }

    LIGHTINGS = {
        "사용안함": [],
        "안정적분산": [
            ("soft even daylight", "soft_daylight"),
            ("even neutral studio lighting", "studio_lighting"),
            ("soft window light", "window_light"),
            ("neutral indoor lighting", "indoor_lighting"),
            ("soft overcast daylight", "overcast_daylight"),
        ],
        "다양한분산": [
            ("soft even daylight", "soft_daylight"),
            ("even neutral studio lighting", "studio_lighting"),
            ("soft window light", "window_light"),
            ("neutral indoor lighting", "indoor_lighting"),
            ("soft overcast daylight", "overcast_daylight"),
            ("soft warm indoor lighting", "warm_indoor"),
            ("soft evening daylight", "evening_daylight"),
            ("gentle side lighting with visible facial detail", "gentle_side_light"),
            ("soft shaded outdoor light", "shaded_outdoor"),
        ],
    }

    IDENTITY_TEXT = {
        "강함": "Keep the same facial identity and hairstyle.",
        "균형": "Keep the same facial identity.",
        "자유": "Keep the person recognizable.",
    }

    BALANCED_IDENTITY_TEXT = {
        "강함": "Keep the person clearly recognizable as image2.",
        "균형": "Keep the face recognizable as image2.",
        "자유": "Keep the face similar to image2.",
    }

    PERSON_BACKGROUND_SEPARATION_FACE = (
        "Keep the head, hair, neck, and shoulders clearly separated from the background. "
        "Keep the full head and hair inside the frame."
    )

    PERSON_BACKGROUND_SEPARATION_BODY = (
        "Keep the person's silhouette clearly separated from the background. "
        "Do not let background objects overlap the person."
    )

    @classmethod
    def INPUT_TYPES(cls):
        default_2511_unet, choices_2511_unet = _preferred_choice(
            "diffusion_models",
            cls.MODEL_2511_UNET_CANDIDATES,
            "<2511 확산 모델 없음>",
        )
        default_2511_te, choices_2511_te = _preferred_choice(
            "text_encoders",
            cls.MODEL_2511_TEXT_ENCODER_CANDIDATES,
            "<2511 텍스트 인코더 없음>",
        )
        default_2511_vae, choices_2511_vae = _preferred_choice(
            "vae",
            cls.MODEL_2511_VAE_CANDIDATES,
            "<2511 VAE 없음>",
        )
        default_21_unet, choices_21_unet = _preferred_choice(
            "diffusion_models",
            cls.MODEL_21_UNET_CANDIDATES,
            "<2.1 확산 모델 없음>",
        )
        default_21_te, choices_21_te = _preferred_choice(
            "text_encoders",
            cls.MODEL_21_TEXT_ENCODER_CANDIDATES,
            "<2.1 텍스트 인코더 없음>",
        )
        default_21_vae, choices_21_vae = _preferred_choice(
            "vae",
            cls.MODEL_21_VAE_CANDIDATES,
            "<2.1 VAE 없음>",
        )
        return {
            "required": {
                "얼굴_참조_이미지": ("IMAGE",),
                "트리거": (
                    "STRING",
                    {"multiline": False, "default": "", "dynamicPrompts": False},
                ),
                "피사체": (
                    "STRING",
                    {"multiline": False, "default": "a korean woman", "dynamicPrompts": False},
                ),
                "생성_모드": (cls.MODE_OPTIONS, {"default": "얼굴전용"}),
                "배치수": (cls.COUNT_OPTIONS, {"default": "24"}),
                "조명_옵션": (cls.LIGHTING_OPTIONS, {"default": "안정적분산"}),
                "의상_다양성": (cls.OUTFIT_OPTIONS, {"default": "보통"}),
                "배경_옵션": (cls.BACKGROUND_OPTIONS, {"default": "단순"}),
                "표정_변화": (cls.EXPRESSION_OPTIONS, {"default": "보통"}),
                "정체성_보존": (cls.IDENTITY_OPTIONS, {"default": "균형"}),
                "seed": (
                    "INT",
                    {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF, "control_after_generate": True},
                ),
                "steps": ("INT", {"default": 8, "min": 1, "max": 10000}),
                "cfg": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 100.0, "step": 0.1}),
                "sampler_name": (comfy.samplers.KSampler.SAMPLERS, {"default": "euler"}),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS, {"default": "simple"}),
                "denoise": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                "출력_비율": (cls.ASPECT_OPTIONS, {"default": "원본 비율"}),
                "모델_계열": (cls.MODEL_VARIANTS, {"default": cls.MODEL_VARIANT_2511}),
                "2511_확산모델": (choices_2511_unet, {"default": default_2511_unet}),
                "2511_텍스트인코더": (choices_2511_te, {"default": default_2511_te}),
                "2511_VAE": (choices_2511_vae, {"default": default_2511_vae}),
                "2.1_확산모델": (choices_21_unet, {"default": default_21_unet}),
                "2.1_텍스트인코더": (choices_21_te, {"default": default_21_te}),
                "2.1_VAE": (choices_21_vae, {"default": default_21_vae}),
                "2.1_캐시_장치": (cls.CACHE_DEVICE_OPTIONS, {"default": "auto"}),
                "2.1_캐시_정밀도": (cls.CACHE_DTYPE_OPTIONS, {"default": "default"}),
            },
            "optional": _FlexibleOptionalInputs(ANY_TYPE, {
                # 얼굴전용에서는 연결하지 않아도 된다. 밸런스에서는 필수다.
                "밸런스_참조_이미지_상반신이상": ("IMAGE",),
            }),
        }

    RETURN_TYPES = ()
    FUNCTION = "generate_and_save"
    OUTPUT_NODE = True
    CATEGORY = "Ultra/Batch"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return uuid.uuid4().hex

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        if kwargs.get("생성_모드") not in cls.MODE_OPTIONS:
            return "생성 모드 옵션이 올바르지 않습니다."
        if kwargs.get("배치수") not in cls.COUNT_OPTIONS:
            return "이미지 수 옵션이 올바르지 않습니다."
        if kwargs.get("조명_옵션") not in cls.LIGHTING_OPTIONS:
            return "조명 옵션이 올바르지 않습니다."
        if kwargs.get("의상_다양성") not in cls.OUTFIT_OPTIONS:
            return "의상 다양성 옵션이 올바르지 않습니다."
        if kwargs.get("배경_옵션") not in cls.BACKGROUND_OPTIONS:
            return "배경 옵션이 올바르지 않습니다."
        if kwargs.get("표정_변화") not in cls.EXPRESSION_OPTIONS:
            return "표정 변화 옵션이 올바르지 않습니다."
        if kwargs.get("정체성_보존") not in cls.IDENTITY_OPTIONS:
            return "정체성 보존 옵션이 올바르지 않습니다."
        if kwargs.get("출력_비율", "원본 비율") not in cls.ASPECT_OPTIONS:
            return "출력 비율 옵션이 올바르지 않습니다."
        model_variant = kwargs.get("모델_계열", cls.MODEL_VARIANT_2511)
        if model_variant not in cls.MODEL_VARIANTS:
            return "모델 계열 옵션이 올바르지 않습니다."
        try:
            if model_variant == cls.MODEL_VARIANT_2511:
                _require_available_file("diffusion_models", kwargs.get("2511_확산모델", ""), "2511 확산 모델")
                _require_available_file("text_encoders", kwargs.get("2511_텍스트인코더", ""), "2511 텍스트 인코더")
                _require_available_file("vae", kwargs.get("2511_VAE", ""), "2511 VAE")
            else:
                _require_available_file("diffusion_models", kwargs.get("2.1_확산모델", ""), "2.1 확산 모델")
                _require_available_file("text_encoders", kwargs.get("2.1_텍스트인코더", ""), "2.1 텍스트 인코더")
                _require_available_file("vae", kwargs.get("2.1_VAE", ""), "2.1 VAE")
                if kwargs.get("2.1_캐시_장치", "auto") not in cls.CACHE_DEVICE_OPTIONS:
                    return "2.1 캐시 장치 옵션이 올바르지 않습니다."
                if kwargs.get("2.1_캐시_정밀도", "default") not in cls.CACHE_DTYPE_OPTIONS:
                    return "2.1 캐시 정밀도 옵션이 올바르지 않습니다."
        except ValueError as exc:
            return str(exc)
        return True

    def _pick(items, index: int, stride: int = 1):
        if not items:
            return ("", "")
        return items[(index * stride) % len(items)]

    @staticmethod
    def _classify_background_scene(background_slug: str) -> str:
        if background_slug in {"neutral_studio", "light_gray", "warm_gray"}:
            return "studio"
        if background_slug in {"blurred_indoor", "clean_room", "window_interior", "minimal_cafe", "modern_hallway"}:
            return "indoor"
        if background_slug in {"quiet_street", "open_park"}:
            return "outdoor"
        return "mixed"

    @classmethod
    def _pick_lighting(cls, lighting_option: str, background_slug: str, global_index: int):
        if lighting_option == "사용안함":
            return ("", "")
        scene = cls._classify_background_scene(background_slug)
        if lighting_option == "안정적분산":
            pool_map = {
                "studio": [
                    ("even neutral studio lighting", "studio_lighting"),
                    ("soft even daylight", "soft_daylight"),
                ],
                "indoor": [
                    ("soft window light", "window_light"),
                    ("neutral indoor lighting", "indoor_lighting"),
                    ("soft warm indoor lighting", "warm_indoor"),
                ],
                "outdoor": [
                    ("soft overcast daylight", "overcast_daylight"),
                    ("soft even daylight", "soft_daylight"),
                    ("soft shaded outdoor light", "shaded_outdoor"),
                ],
            }
        else:
            pool_map = {
                "studio": [
                    ("even neutral studio lighting", "studio_lighting"),
                    ("soft even daylight", "soft_daylight"),
                    ("gentle side lighting with visible facial detail", "gentle_side_light"),
                ],
                "indoor": [
                    ("soft window light", "window_light"),
                    ("neutral indoor lighting", "indoor_lighting"),
                    ("soft warm indoor lighting", "warm_indoor"),
                    ("gentle side lighting with visible facial detail", "gentle_side_light"),
                ],
                "outdoor": [
                    ("soft overcast daylight", "overcast_daylight"),
                    ("soft even daylight", "soft_daylight"),
                    ("soft evening daylight", "evening_daylight"),
                    ("soft shaded outdoor light", "shaded_outdoor"),
                ],
            }
        pool = pool_map.get(scene) or [("soft even daylight", "soft_daylight")]
        return cls._pick(pool, global_index, 1)

    @classmethod
    def _pick_face_angle(cls, mode: str, quantity: int, local_index: int, global_index: int):
        del global_index  # 장수별 명시 계획이 유일한 권위다.
        plans = (
            cls.FACE_ONLY_ANGLE_SLUG_PLANS
            if mode == "얼굴전용"
            else cls.BALANCED_FACE_ANGLE_SLUG_PLANS
        )
        slug_plan = plans.get(quantity)
        if not slug_plan or len(slug_plan) != quantity:
            raise ValueError(f"내부 얼굴 각도 계획이 없습니다: {mode} {quantity}장")
        angle_by_slug = {slug: (text, slug) for text, slug in cls.FACE_ANGLES}
        slug = slug_plan[local_index]
        return angle_by_slug[slug]

    @classmethod
    def _pick_body_angle(cls, shot_kind: str, quantity: int, local_index: int):
        slug_plan = cls.BODY_ANGLE_SLUG_PLANS.get(quantity)
        if not slug_plan or len(slug_plan) != quantity:
            raise ValueError(f"내부 몸 각도 계획이 없습니다: {shot_kind} {quantity}장")
        angle_by_slug = {slug: (text, slug) for text, slug in cls.BODY_ANGLES[shot_kind]}
        slug = slug_plan[local_index]
        return angle_by_slug[slug]

    @classmethod
    def _pick_face_gaze(cls, angle_slug: str, occurrence_index: int):
        # 측면 얼굴에서 '카메라 정면 응시'처럼 해부학적으로 충돌하는 조합을 만들지 않는다.
        if angle_slug == "left_profile":
            options = [
                ("looking naturally toward the left", "looking_left"),
                ("looking slightly downward", "looking_down"),
                ("looking slightly upward", "looking_up"),
            ]
        elif angle_slug == "right_profile":
            options = [
                ("looking naturally toward the right", "looking_right"),
                ("looking slightly downward", "looking_down"),
                ("looking slightly upward", "looking_up"),
            ]
        elif angle_slug == "over_shoulder":
            options = [
                ("looking back toward the camera", "looking_back_camera"),
                ("looking slightly past the camera", "looking_past_camera"),
            ]
        elif angle_slug == "high_angle":
            options = [
                ("looking naturally toward the elevated camera", "looking_elevated_camera"),
            ]
        elif angle_slug == "low_angle":
            options = [
                ("looking naturally toward the lower camera", "looking_lower_camera"),
            ]
        elif angle_slug in {"threequarter_left", "slight_left"}:
            options = [
                ("looking at the camera", "looking_camera"),
                ("looking slightly left", "looking_left"),
                ("looking slightly downward", "looking_down"),
            ]
        elif angle_slug in {"threequarter_right", "slight_right"}:
            options = [
                ("looking at the camera", "looking_camera"),
                ("looking slightly right", "looking_right"),
                ("looking slightly downward", "looking_down"),
            ]
        else:
            options = cls.GAZES
        # 장수별 명시 각도 계획에서 같은 각도가 다시 등장한 횟수로 시선을 순환한다.
        # 전역 인덱스를 쓰면 가까운 위치에 반복된 정면 각도가 같은 시선을 받을 수 있다.
        return cls._pick(options, occurrence_index, 1)

    @classmethod
    def _pick_face_head_pose_slug(cls, angle_slug: str, global_index: int) -> str:
        # high/low는 실제 카메라 시점이므로 머리 기울임으로 대체하지 않는다.
        if angle_slug in {"high_angle", "low_angle"}:
            return "upright"
        # 프로필·오버숄더에서는 "두 눈이 보이게" 하는 좌우 기울임이 각도와 충돌한다.
        if angle_slug in {"left_profile", "right_profile", "over_shoulder"}:
            safe_plan = ["upright", "chin_up", "upright", "chin_down"]
            return safe_plan[global_index % len(safe_plan)]
        return cls.FACE_HEAD_POSE_PLAN[global_index % len(cls.FACE_HEAD_POSE_PLAN)]

    @classmethod
    def _build_variants(cls, mode: str, count: int, lighting_option: str,
                        outfit_option: str, background_option: str,
                        expression_option: str, identity_option: str):
        plan = cls.MODE_COUNTS[mode][count]
        outfits = cls.OUTFITS[outfit_option]
        backgrounds = cls.BACKGROUNDS[background_option]
        lightings = cls.LIGHTINGS[lighting_option]
        face_expressions = cls.FACE_EXPRESSIONS[expression_option]
        balanced_expressions = cls.BALANCED_EXPRESSIONS[expression_option]
        identity_sentence = cls.IDENTITY_TEXT[identity_option]

        variants = []
        running_index = 0
        face_angle_occurrences = {}
        body_pose_usage = {}
        for shot_kind in ("close", "upper", "knee", "full"):
            quantity = plan.get(shot_kind, 0)
            for local_index in range(quantity):
                global_index = running_index
                background_text, background_slug = cls._pick(backgrounds, global_index, 5)
                lighting_text, lighting_slug = cls._pick_lighting(lighting_option, background_slug, global_index)

                if shot_kind == "close":
                    # 7은 4/10/16개 얼굴 표정 풀과 모두 서로소라 전체 풀을 고르게 순환한다.
                    expression_text, expression_slug = cls._pick(face_expressions, global_index, 7)
                    expression_caption = cls.EXPRESSION_CAPTIONS.get(expression_slug, expression_text)
                    frame_text, frame_slug = cls._pick(cls.FACE_FRAMES, global_index, 2)
                    angle_text, angle_slug = cls._pick_face_angle(
                        mode, quantity, local_index, global_index
                    )
                    occurrence_index = face_angle_occurrences.get(angle_slug, 0)
                    face_angle_occurrences[angle_slug] = occurrence_index + 1
                    if angle_slug in {"high_angle", "low_angle"}:
                        # high/low는 시점 판별용 핵심 장면이다. 표정·고개 포즈가 카메라
                        # 위치 지시를 덮지 않도록 중립 표정과 정자세로 고정한다.
                        expression_text = "natural neutral expression"
                        expression_slug = "neutral"
                        expression_caption = "neutral expression"
                        # 40장 얼굴전용에서 같은 시점이 두 번째로 등장할 때만
                        # 프레임을 바꿔 중립 표정 고정으로 인한 완전 중복을 막는다.
                        # 첫 high/low와 밸런스 얼굴 분기의 기존 프레임·자동 비율은 보존한다.
                        if occurrence_index > 0:
                            alternate_frame = {
                                "high_angle": ("close-up face portrait", "face_closeup"),
                                "low_angle": ("head-and-shoulders portrait", "head_shoulders"),
                            }
                            frame_text, frame_slug = alternate_frame[angle_slug]
                    gaze_text, gaze_slug = cls._pick_face_gaze(
                        angle_slug, occurrence_index
                    )
                    head_pose_slug = cls._pick_face_head_pose_slug(angle_slug, global_index)
                    head_pose_prompt, head_pose_caption = cls.HEAD_POSES[head_pose_slug]
                    prompt = cls._render_face_prompt(
                        frame_text=frame_text,
                        angle_text=angle_text,
                        angle_slug=angle_slug,
                        expression_text=expression_text,
                        gaze_text=gaze_text,
                        head_pose_prompt=head_pose_prompt,
                        background_text=background_text,
                        lighting_text=lighting_text,
                        identity_sentence=identity_sentence,
                    )
                    if angle_slug in {"high_angle", "low_angle"}:
                        caption = cls._render_caption([
                            frame_text, angle_text, expression_caption,
                            lighting_text, background_text,
                        ])
                    else:
                        caption = cls._render_caption([
                            frame_text, angle_text, expression_caption, gaze_text, head_pose_caption,
                            lighting_text, background_text,
                        ])
                    stem = cls._build_filename(
                        global_index + 1, frame_slug, angle_slug, expression_slug, gaze_slug
                    )
                    ref_kind = "face"
                else:
                    shot_text, shot_slug = cls.SHOT_LABELS[shot_kind]
                    # 장수별 명시 계획으로 정면 비중과 좌우 대칭을 동시에 유지한다.
                    angle_text, angle_slug = cls._pick_body_angle(
                        shot_kind, quantity, local_index
                    )
                    # 전체 포즈 풀은 local_index 순서로 고르게 순환하되, 같은 몸
                    # 각도에서 이미 사용한 포즈와 겹치면 다음 포즈로 이동한다.
                    # 장수별 계획에서 같은 각도는 최대 4회이고 포즈 풀도 4종이므로
                    # 반복 각도마다 고유 포즈를 유지하면서 전체 분포도 균형을 이룬다.
                    pose_pool = cls.POSES[shot_kind]
                    body_key = (shot_kind, angle_slug)
                    used_pose_indices = body_pose_usage.setdefault(body_key, set())
                    preferred_pose_index = local_index % len(pose_pool)
                    pose_index = next(
                        (preferred_pose_index + offset) % len(pose_pool)
                        for offset in range(len(pose_pool))
                        if (preferred_pose_index + offset) % len(pose_pool) not in used_pose_indices
                    )
                    used_pose_indices.add(pose_index)
                    pose_text, pose_slug = pose_pool[pose_index]
                    # 7은 4/8/12개 밸런스 표정 풀과 모두 서로소라 전체 풀을 고르게 순환한다.
                    expression_text, expression_slug = cls._pick(balanced_expressions, global_index, 7)
                    expression_caption = cls.EXPRESSION_CAPTIONS.get(expression_slug, expression_text)
                    outfit_text, outfit_slug = cls._pick(outfits, global_index, 5)
                    if angle_slug in {"high_angle", "low_angle"}:
                        expression_text = "natural neutral expression"
                        expression_slug = "neutral"
                        expression_caption = "neutral expression"
                        head_pose_slug = "upright"
                    else:
                        head_pose_slug = cls.BALANCED_BODY_HEAD_POSE_PLAN[
                            global_index % len(cls.BALANCED_BODY_HEAD_POSE_PLAN)
                        ]
                    head_pose_prompt, head_pose_caption = cls.HEAD_POSES[head_pose_slug]
                    prompt = cls._render_balanced_prompt(
                        shot_kind=shot_kind,
                        shot_text=shot_text,
                        angle_text=angle_text,
                        angle_slug=angle_slug,
                        pose_text=pose_text,
                        expression_text=expression_text,
                        head_pose_prompt=head_pose_prompt,
                        outfit_text=outfit_text,
                        background_text=background_text,
                        lighting_text=lighting_text,
                        identity_sentence=cls.BALANCED_IDENTITY_TEXT[identity_option],
                    )
                    caption = cls._render_caption([
                        shot_text, angle_text, pose_text, expression_caption, head_pose_caption,
                        outfit_text, lighting_text, background_text,
                    ])
                    stem = cls._build_filename(
                        global_index + 1, shot_slug, angle_slug, pose_slug, outfit_slug
                    )
                    ref_kind = "balanced"

                if shot_kind == "close":
                    auto_aspect = "2:3" if frame_slug == "close_head_shoulders" else "3:4"
                elif shot_kind == "upper":
                    auto_aspect = "3:4"
                else:
                    auto_aspect = "2:3"

                variants.append({
                    "filename": stem,
                    "prompt": prompt,
                    "caption": caption,
                    "ref_kind": ref_kind,
                    "auto_aspect": auto_aspect,
                })
                running_index += 1

        if len(variants) != count:
            raise ValueError(f"내부 변형 생성 개수가 잘못되었습니다. 기대 {count}, 실제 {len(variants)}")
        filenames = [item["filename"].casefold() for item in variants]
        if len(filenames) != len(set(filenames)):
            raise ValueError("내부 변형 저장 파일명이 중복되었습니다.")
        prompts = [item["prompt"] for item in variants]
        if len(prompts) != len(set(prompts)):
            raise ValueError("내부 변형 편집 프롬프트가 중복되었습니다.")
        captions = [item["caption"].casefold() for item in variants]
        if len(captions) != len(set(captions)):
            raise ValueError("내부 변형 TXT 캡션이 중복되었습니다.")
        return variants

    @staticmethod
    def _with_indefinite_article(text: str) -> str:
        value = str(text or "").strip()
        if not value:
            return value
        article = "an" if value[0].casefold() in "aeiou" else "a"
        return f"{article} {value}"

    @classmethod
    def _render_face_prompt(cls, frame_text: str, angle_text: str, angle_slug: str, expression_text: str,
                            gaze_text: str, head_pose_prompt: str, background_text: str, lighting_text: str,
                            identity_sentence: str) -> str:
        if angle_slug == "high_angle":
            # 얼굴 클로즈업의 high/low는 새 카메라 시점과 얼굴 원근을 직접적으로
            # 지정해야 한다. 일반 인물사진식 보존 문장이 강하면 다시 eye-level
            # 정면 얼굴로 돌아가기 쉬우므로, high/low에서는 카메라 위치와 얼굴
            # 원근 관계를 가장 먼저 짧고 명확하게 고정한다.
            parts = [
                "Change the image to a high angle portrait of the person's face.",
                "Place the camera clearly above the person and angle it downward at the face.",
                f"Frame it as a {frame_text} from this elevated viewpoint.",
                "Show the face from above so the forehead and top of the head appear closer to the camera and the chin appears farther away.",
                "Keep a natural neutral expression and keep the person recognizable.",
            ]
        elif angle_slug == "low_angle":
            parts = [
                "Change the image to a low angle portrait of the person's face.",
                "Place the camera clearly below the person's eye level and angle it upward at the face.",
                f"Frame it as a {frame_text} from this lower viewpoint.",
                "Show the face from below so the chin and underside of the jaw appear closer to the camera and the forehead appears farther away.",
                "Keep a natural neutral expression and keep the person recognizable.",
            ]
        else:
            expression_phrase = cls._with_indefinite_article(expression_text)
            parts = [
                f"Change the image to a {angle_text} {frame_text}.",
                f"Show {expression_phrase}, {gaze_text}. {head_pose_prompt}",
                f"{identity_sentence} Follow the requested new expression, gaze, and camera angle instead of copying the reference view.",
            ]
            if lighting_text:
                parts.append(f"Use {lighting_text}.")
            parts.append(f"Use {background_text}.")
            parts.append("Keep the face clear and recognizable.")
            parts.append(cls.PERSON_BACKGROUND_SEPARATION_FACE)
        return " ".join(parts)

    @classmethod
    def _render_balanced_prompt(cls, shot_kind: str, shot_text: str, angle_text: str, angle_slug: str,
                                pose_text: str, expression_text: str, head_pose_prompt: str, outfit_text: str,
                                background_text: str, lighting_text: str,
                                identity_sentence: str) -> str:
        if angle_slug == "high_angle":
            perspective_detail = {
                "upper": "Show the upper body clearly from above, including the top surfaces of the shoulders and torso.",
                "knee": "Show the person clearly from above from the head through the thighs, with the shoulders and chest seen from above.",
                "full": "Show the full body clearly from above, with the head and shoulders closer to the camera than the lower body.",
            }[shot_kind]
            if shot_kind == "full":
                parts = [
                    "Change the image to a zoomed-out high angle full-body portrait of the person in image1.",
                    "Place the camera clearly above the person, angle it downward, and pull the framing back.",
                    "Reframe the shot to show the entire body from head to toe.",
                    "Show both feet fully, with a little space above the head and below the feet. Do not crop the legs or feet.",
                    perspective_detail,
                    f"Use {pose_text}. Dress the person in {outfit_text}.",
                    "Keep a natural neutral expression and keep the head upright and level.",
                    f"Use image1 for body proportions. Use image2 only for the face and hairstyle. {identity_sentence}",
                    "The person should appear fully visible and smaller in the frame, not as a close crop. Follow the requested high camera angle and full-body framing instead of copying the reference framing. Do not create another person.",
                ]
            else:
                parts = [
                    "Change the image to a high angle shot of the person in image1.",
                    "Place the camera clearly above the person and angle it downward.",
                    f"Use {cls._with_indefinite_article(shot_text)}.",
                    perspective_detail,
                    f"Use {pose_text}. Dress the person in {outfit_text}.",
                    "Keep a natural neutral expression and keep the head upright and level.",
                    f"Use image1 for body proportions. Use image2 only for the face and hairstyle. {identity_sentence}",
                    "Follow the requested high camera angle instead of copying the reference framing. Do not create another person.",
                ]
        elif angle_slug == "low_angle":
            perspective_detail = {
                "upper": "Show the upper body clearly from below, with the shoulders and upper torso seen from a lower viewpoint.",
                "knee": "Show the person clearly from below from the head through the thighs, with an upward perspective along the body.",
                "full": "Show the full body clearly from below, with an upward perspective from the legs through the torso and the head above them.",
            }[shot_kind]
            if shot_kind == "full":
                parts = [
                    "Change the image to a zoomed-out low angle full-body portrait of the person in image1.",
                    "Place the camera clearly below the person, angle it upward, and pull the framing back.",
                    "Reframe the shot to show the entire body from head to toe.",
                    "Show both feet fully, with a little space above the head and below the feet. Do not crop the legs or feet.",
                    perspective_detail,
                    f"Use {pose_text}. Dress the person in {outfit_text}.",
                    "Keep a natural neutral expression and keep the head upright and level.",
                    f"Use image1 for body proportions. Use image2 only for the face and hairstyle. {identity_sentence}",
                    "The person should appear fully visible and smaller in the frame, not as a close crop. Follow the requested low camera angle and full-body framing instead of copying the reference framing. Do not imitate the angle only by raising the chin. Do not create another person.",
                ]
            else:
                if shot_kind == "upper":
                    parts = [
                        "Change the image to a low angle shot of the person in image1.",
                        "Place the camera below shoulder level and angle it upward toward the person.",
                        f"Use {cls._with_indefinite_article(shot_text)}.",
                        perspective_detail,
                        f"Use {pose_text}. Dress the person in {outfit_text}.",
                        "Keep a natural neutral expression and keep the head naturally aligned with the torso, without tilting it backward.",
                        f"Use image1 for body proportions. Use image2 only for the face and hairstyle. {identity_sentence}",
                        "Create the low-angle perspective through the camera position and the upper-body perspective. Do not imitate the angle by raising the chin or tilting the head backward. Do not create another person.",
                    ]
                else:
                    parts = [
                        "Change the image to a low angle shot of the person in image1.",
                        "Place the camera clearly below the person and angle it upward.",
                        f"Use {cls._with_indefinite_article(shot_text)}.",
                        perspective_detail,
                        f"Use {pose_text}. Dress the person in {outfit_text}.",
                        "Keep a natural neutral expression and keep the head upright and level.",
                        f"Use image1 for body proportions. Use image2 only for the face and hairstyle. {identity_sentence}",
                        "Follow the requested low camera angle instead of copying the reference framing. Do not imitate the angle only by raising the chin. Do not create another person.",
                    ]
        else:
            expression_phrase = cls._with_indefinite_article(expression_text)
            if shot_kind == "full":
                parts = [
                    f"Change the image to a zoomed-out {angle_text} full-body portrait of the person in image1, showing {expression_phrase} while wearing {outfit_text}.",
                    "Pull the framing back and reframe the shot to show the entire body from head to toe.",
                    "Show both feet fully, with a little space above the head and below the feet. Do not crop the legs or feet.",
                    "The person should appear smaller in the frame. Do not use a close crop, mid-shot, or upper-body framing.",
                    f"Use {pose_text}. {head_pose_prompt}",
                    f"Use image1 for body proportions. Use image2 for facial identity and hairstyle. {identity_sentence}",
                    "Follow the requested new pose, expression, camera angle, and full-body framing instead of copying image2's crop or viewpoint.",
                    "Do not create an additional person.",
                ]
            else:
                parts = [
                    f"Change the person in image1 to a {angle_text} {shot_text}, showing {expression_phrase} while wearing {outfit_text}.",
                    f"Use {pose_text}. {head_pose_prompt}",
                    f"Use image1 for body proportions. Use image2 for facial identity and hairstyle. {identity_sentence}",
                    "Follow the requested new pose, expression, and camera angle instead of copying image2's crop or viewpoint.",
                    "Do not create an additional person.",
                ]
        # 밸런스 몸 변형의 핵심 목적은 참조의 체형/프레이밍은 활용하되
        # 참조 의상을 고정하지 않고 각 변형에서 계획된 의상을 실제로 분산하는 것이다.
        # 얼굴전용/밸런스 얼굴 클로즈업은 이 함수를 사용하지 않으므로 의상 교체 지시를 받지 않는다.
        parts.append(
            "Treat clothing in the reference images as non-binding. "
            "Do not preserve or copy it; use the requested outfit for this variant."
        )
        if shot_kind == "full":
            parts.append("Keep the full body inside the frame, including the full head and both feet.")
        elif shot_kind == "knee":
            parts.append("Keep the person visible from the full head through the thighs.")
        else:
            parts.append("Keep the full head, shoulders, and upper torso visible.")
        if lighting_text:
            parts.append(f"Use {lighting_text}.")
        parts.append(f"Use {background_text}.")
        parts.append("Keep the face recognizable and the person unobstructed.")
        parts.append(cls.PERSON_BACKGROUND_SEPARATION_BODY)
        return " ".join(parts)

    @staticmethod
    def _render_caption(items) -> str:
        cleaned = []
        seen = set()
        for item in items:
            value = _normalize_caption_fragment(item)
            if not value:
                continue
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(value)
        return ", ".join(cleaned)

    @staticmethod
    def _build_filename(index: int, *parts: str) -> str:
        return "_".join([f"{index:02d}"] + [part for part in parts if part])

    @classmethod
    def _prepare_variants(cls, *, mode: str, count: int, lighting_option: str,
                          outfit_option: str, background_option: str,
                          expression_option: str, identity_option: str,
                          trigger: str, subject: str, aspect_option: str = "원본 비율"):
        """실제 생성과 버튼 미리보기가 공유하는 유일한 변형 계획 생성 경로다."""
        variants = cls._build_variants(
            mode=mode,
            count=count,
            lighting_option=lighting_option,
            outfit_option=outfit_option,
            background_option=background_option,
            expression_option=expression_option,
            identity_option=identity_option,
        )
        if aspect_option not in cls.ASPECT_OPTIONS:
            raise ValueError(f"출력 비율 옵션이 올바르지 않습니다: {aspect_option!r}")
        trig = _normalize_caption_fragment(trigger)
        subj = _normalize_caption_fragment(subject)
        if not subj:
            raise ValueError("피사체가 비어 있습니다.")
        prefix = ", ".join([item for item in (trig, subj) if item])

        prepared_variants = []
        for item in variants:
            caption_text = item["caption"]
            full_caption = (
                f"{prefix}, {caption_text}"
                if prefix and caption_text
                else (prefix or caption_text)
            )
            prepared_variants.append({
                **item,
                "caption": _normalize_caption_fragment(full_caption),
                "output_aspect": (
                    item["auto_aspect"] if aspect_option == "자동 구도별" else "원본"
                ),
            })
        return prepared_variants

    @classmethod
    def _build_preview_text(cls, prepared_variants, *, mode: str, count: int,
                            lighting_option: str, outfit_option: str,
                            background_option: str, expression_option: str,
                            identity_option: str, aspect_option: str,
                            model_variant: str, balanced_connected: bool) -> str:
        """실제 생성에 사용하는 prepared_variants를 사람이 읽기 쉬운 문자열로 표시한다."""
        face_count = sum(1 for item in prepared_variants if item["ref_kind"] == "face")
        balanced_count = len(prepared_variants) - face_count
        outfit_display = outfit_option if mode != "얼굴전용" else f"{outfit_option} (얼굴전용에서는 미사용)"
        aspect_counts = {}
        for item in prepared_variants:
            key = item.get("output_aspect", "원본")
            aspect_counts[key] = aspect_counts.get(key, 0) + 1
        if aspect_option == "자동 구도별":
            aspect_summary = " / ".join(
                f"{ratio} {aspect_counts.get(ratio, 0)}장" for ratio in ("3:4", "2:3")
                if aspect_counts.get(ratio, 0)
            )
        else:
            aspect_summary = "참조 이미지 비율 유지"
        lines = [
            "Ultra 로라 레퍼런스 생성 계획",
            "=" * 38,
            f"모델 계열: {model_variant}",
            f"생성 모드: {mode}",
            f"이미지 수: {count}",
            f"참조 분기: 얼굴 단일 참조 {face_count}장 / 밸런스+얼굴 이중 참조 {balanced_count}장",
            f"조명: {lighting_option}",
            f"의상 다양성: {outfit_display}",
            f"배경: {background_option}",
            f"표정 변화: {expression_option}",
            f"정체성 보존: {identity_option}",
            f"출력 비율: {aspect_option} ({aspect_summary})",
        ]

        if mode == "밸런스":
            connection_text = "연결됨" if balanced_connected else "미연결"
            lines.append(f"밸런스 참조 이미지: {connection_text}")
            if not balanced_connected:
                lines.extend([
                    "주의: 계획은 확인할 수 있지만 실제 생성은 실행할 수 없습니다.",
                    "실제 생성 전 상반신~허벅지 위 밸런스 참조 이미지를 연결하세요.",
                ])

        lines.extend(["", "아래 프롬프트와 캡션은 실제 생성·저장에 사용하는 내용입니다.", ""])

        for index, item in enumerate(prepared_variants, start=1):
            role = "얼굴 참조" if item["ref_kind"] == "face" else "밸런스 참조 + 얼굴 참조"
            lines.extend([
                f"[{index:02d}/{count}] {role}",
                f"파일명: {item['filename']}",
                f"출력 비율: {item.get('output_aspect', '원본')}",
                "프롬프트:",
                item["prompt"],
                "캡션:",
                item["caption"] or "(빈 캡션)",
                "",
            ])

        return "\n".join(lines).rstrip() + "\n"

    @classmethod
    def build_preview_from_payload(cls, payload: dict) -> str:
        """Queue 실행 없이 웹 버튼이 요청한 현재 옵션의 읽기 전용 미리보기를 만든다."""
        if not isinstance(payload, dict):
            raise ValueError("미리보기 요청 형식이 올바르지 않습니다.")

        mode = str(payload.get("생성_모드", ""))
        if mode not in cls.MODE_OPTIONS:
            raise ValueError(f"생성 모드가 올바르지 않습니다: {mode!r}")
        try:
            count = int(str(payload.get("배치수", "")))
        except Exception as exc:
            raise ValueError("이미지 수가 올바르지 않습니다.") from exc
        if str(count) not in cls.COUNT_OPTIONS:
            raise ValueError("이미지 수는 24, 30, 40 중 하나여야 합니다.")

        lighting_option = str(payload.get("조명_옵션", ""))
        outfit_option = str(payload.get("의상_다양성", ""))
        background_option = str(payload.get("배경_옵션", ""))
        expression_option = str(payload.get("표정_변화", ""))
        identity_option = str(payload.get("정체성_보존", ""))
        aspect_option = str(payload.get("출력_비율", "원본 비율"))
        model_variant = str(payload.get("모델_계열", cls.MODEL_VARIANT_2511))
        option_checks = (
            (lighting_option, cls.LIGHTING_OPTIONS, "조명 옵션"),
            (outfit_option, cls.OUTFIT_OPTIONS, "의상 다양성"),
            (background_option, cls.BACKGROUND_OPTIONS, "배경 옵션"),
            (expression_option, cls.EXPRESSION_OPTIONS, "표정 변화"),
            (identity_option, cls.IDENTITY_OPTIONS, "정체성 보존"),
            (aspect_option, cls.ASPECT_OPTIONS, "출력 비율"),
            (model_variant, cls.MODEL_VARIANTS, "모델 계열"),
        )
        for value, allowed, label in option_checks:
            if value not in allowed:
                raise ValueError(f"{label}이 올바르지 않습니다: {value!r}")

        prepared_variants = cls._prepare_variants(
            mode=mode,
            count=count,
            lighting_option=lighting_option,
            outfit_option=outfit_option,
            background_option=background_option,
            expression_option=expression_option,
            identity_option=identity_option,
            trigger=str(payload.get("트리거", "") or ""),
            subject=str(payload.get("피사체", "a korean woman") or ""),
            aspect_option=aspect_option,
        )
        return cls._build_preview_text(
            prepared_variants,
            mode=mode,
            count=count,
            lighting_option=lighting_option,
            outfit_option=outfit_option,
            background_option=background_option,
            expression_option=expression_option,
            identity_option=identity_option,
            aspect_option=aspect_option,
            model_variant=model_variant,
            # 프런트엔드는 실제 boolean을 보내며, 문자열 "false" 같은 임의 API
            # 값이 연결됨으로 오인되지 않도록 True만 연결 상태로 인정한다.
            balanced_connected=payload.get("balanced_connected", False) is True,
        )

    @staticmethod
    def _validate_lora_rows(lora_rows):
        if not lora_rows:
            return
        available_loras = set(folder_paths.get_filename_list("loras"))
        for _, _, lora_name, _ in lora_rows:
            if lora_name not in available_loras:
                raise ValueError(
                    f"선택한 LoRA 파일을 찾을 수 없습니다: {lora_name}. 모델·LoRA 목록을 새로고침하고 다시 선택하십시오."
                )

    @staticmethod
    def _append_lora_stack(graph, model, lora_rows, model_variant):
        """GraphBuilder 안에서 LoRA를 순서대로 적용한다.

        동적 LoRA를 Python에서 직접 실행하면 확장 그래프에는 적용 단계가 보이지 않아
        캐시/오류 추적이 불명확해질 수 있다. 내부 그래프의 정식 노드로 추가해
        사용자가 정한 순서와 실행 그래프를 정확히 일치시킨다.
        """
        if not lora_rows:
            return model
        if nodes.NODE_CLASS_MAPPINGS.get("LoraLoaderModelOnly") is None:
            raise RuntimeError(
                "ComfyUI의 LoraLoaderModelOnly 노드를 찾을 수 없습니다. ComfyUI를 업데이트한 뒤 다시 시도하십시오."
            )
        current_model = model
        variant_tag = "2511" if model_variant == UltraSmartLoraDatasetGenerateSave.MODEL_VARIANT_2511 else "21"
        for index, (_, _, lora_name, strength) in enumerate(lora_rows, start=1):
            loader = graph.node(
                "LoraLoaderModelOnly",
                id=f"smart_lora_{variant_tag}_{index:02d}",
                model=current_model,
                lora_name=lora_name,
                strength_model=strength,
            )
            current_model = loader.out(0)
        return current_model

    @classmethod
    def _prompt_for_model_variant(cls, prompt: str, model_variant: str) -> str:
        text = str(prompt or "")
        if model_variant != cls.MODEL_VARIANT_21:
            return text
        # Qwen Image 2.1 공식 편집 템플릿은 참조 이미지를 <image1>, <image2> ...
        # 형식으로 지칭한다. 기존 2511 프롬프트 문형은 그대로 보존하고 2.1에서만
        # 참조 표기를 변환한다. 이미 태그가 붙은 문자열은 중복 변환하지 않는다.
        text = re.sub(r"(?<!<)\bimage1\b(?!>)", "<image1>", text, flags=re.IGNORECASE)
        text = re.sub(r"(?<!<)\bimage2\b(?!>)", "<image2>", text, flags=re.IGNORECASE)
        return text

    def generate_and_save(self, **kwargs):
        face_image = kwargs["얼굴_참조_이미지"]
        balanced_image = kwargs.get("밸런스_참조_이미지_상반신이상")
        trigger = kwargs.get("트리거", "")
        subject = kwargs.get("피사체", "a korean woman")
        mode = kwargs["생성_모드"]
        count = int(str(kwargs["배치수"]))
        lighting_option = kwargs["조명_옵션"]
        outfit_option = kwargs["의상_다양성"]
        background_option = kwargs["배경_옵션"]
        expression_option = kwargs["표정_변화"]
        identity_option = kwargs["정체성_보존"]
        aspect_option = kwargs.get("출력_비율", "원본 비율")
        model_variant = kwargs.get("모델_계열", self.MODEL_VARIANT_2511)
        if aspect_option not in self.ASPECT_OPTIONS:
            raise ValueError(f"출력 비율 옵션이 올바르지 않습니다: {aspect_option!r}")
        if model_variant not in self.MODEL_VARIANTS:
            raise ValueError(f"모델 계열 옵션이 올바르지 않습니다: {model_variant!r}")

        if model_variant == self.MODEL_VARIANT_2511:
            diffusion_model = kwargs.get("2511_확산모델", "")
            text_encoder = kwargs.get("2511_텍스트인코더", "")
            vae_name = kwargs.get("2511_VAE", "")
            cache_device = None
            cache_dtype = None
        else:
            diffusion_model = kwargs.get("2.1_확산모델", "")
            text_encoder = kwargs.get("2.1_텍스트인코더", "")
            vae_name = kwargs.get("2.1_VAE", "")
            cache_device = kwargs.get("2.1_캐시_장치", "auto")
            cache_dtype = kwargs.get("2.1_캐시_정밀도", "default")

        _require_available_file("diffusion_models", diffusion_model, f"{model_variant} 확산 모델")
        _require_available_file("text_encoders", text_encoder, f"{model_variant} 텍스트 인코더")
        _require_available_file("vae", vae_name, f"{model_variant} VAE")
        _require_core_nodes("UNETLoader", "CLIPLoader", "VAELoader", "KSampler", "VAEDecode")
        if model_variant == self.MODEL_VARIANT_2511:
            _require_core_nodes("TextEncodeQwenImageEditPlus", "FluxKontextMultiReferenceLatentMethod", "VAEEncode")
        else:
            _require_core_nodes("TextEncodeQwenImage21")
        seed = kwargs["seed"]
        steps = kwargs["steps"]
        cfg = kwargs["cfg"]
        sampler_name = kwargs["sampler_name"]
        scheduler = kwargs["scheduler"]
        denoise = kwargs["denoise"]

        if mode == "밸런스" and balanced_image is None:
            raise ValueError(
                "밸런스 모드에는 '밸런스 참조 이미지 (상반신 이상)'가 필요합니다. "
                "얼굴 클로즈업을 대신 연결하면 얼굴이 과도하게 크게 생성될 수 있으므로 자동 대체하지 않습니다. "
                "얼굴이 선명하게 보이는 상반신~허벅지 위 이미지를 연결하세요."
            )

        lora_rows = _collect_lora_rows(kwargs, model_variant=model_variant)
        self._validate_lora_rows(lora_rows)

        prepared_variants = self._prepare_variants(
            mode=mode,
            count=count,
            lighting_option=lighting_option,
            outfit_option=outfit_option,
            background_option=background_option,
            expression_option=expression_option,
            identity_option=identity_option,
            trigger=trigger,
            subject=subject,
            aspect_option=aspect_option,
        )

        date_str = _build_main_dataset_subfolder(count, mode)

        dataset_plan = json.dumps(
            {
                "schema_version": 1,
                "expected_stems": [item["filename"] for item in prepared_variants],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        graph = GraphBuilder()
        model = graph.node(
            "UNETLoader",
            id=f"smart_model_loader_{'2511' if model_variant == self.MODEL_VARIANT_2511 else '21'}",
            unet_name=diffusion_model,
            weight_dtype=DEFAULT_WEIGHT_DTYPE,
        ).out(0)
        clip = graph.node(
            "CLIPLoader",
            id=f"smart_clip_loader_{'2511' if model_variant == self.MODEL_VARIANT_2511 else '21'}",
            clip_name=text_encoder,
            type=QWEN_CLIP_TYPE,
            device=DEFAULT_TEXT_ENCODER_DEVICE,
        ).out(0)
        vae = graph.node(
            "VAELoader",
            id=f"smart_vae_loader_{'2511' if model_variant == self.MODEL_VARIANT_2511 else '21'}",
            vae_name=vae_name,
        ).out(0)
        model = self._append_lora_stack(graph, model, lora_rows, model_variant)
        if model_variant == self.MODEL_VARIANT_2511:
            # Qwen Image Edit 2511 uses ComfyUI CFGNorm internally with the
            # standard strength used by the reference workflow.  This is an
            # implementation detail, not a user-facing tuning option.
            # Qwen Image 2.1 never enters this branch.
            if nodes.NODE_CLASS_MAPPINGS.get("CFGNorm") is not None:
                model = graph.node(
                    "CFGNorm",
                    id="smart_qwen2511_cfg_norm",
                    model=model,
                    strength=1.0,
                    pre_cfg=False,
                ).out(0)
            else:
                print(
                    "[Ultra LoRA Reference Images] CFGNorm is unavailable in this ComfyUI build; "
                    "continuing the 2511 generation without CFG normalization."
                )
        if model_variant == self.MODEL_VARIANT_21 and nodes.NODE_CLASS_MAPPINGS.get("QwenImage21Cache") is not None:
            model = graph.node(
                "QwenImage21Cache",
                id="smart_qwen21_cache",
                model=model,
                device=cache_device,
                dtype=cache_dtype,
            ).out(0)

        source_cache = {}
        latent_cache = {}
        negative_cache = {}

        def _aspect_id(value: str) -> str:
            return value.replace(":", "x")

        def _source_for(ref_kind: str, aspect: str):
            source = face_image if ref_kind == "face" else balanced_image
            if source is None:
                raise RuntimeError("내부 참조 이미지 준비 상태가 올바르지 않습니다.")
            if aspect == "원본":
                return source
            key = (ref_kind, aspect)
            if key not in source_cache:
                source_cache[key] = graph.node(
                    "_UltraAspectFitImage",
                    id=f"smart_{ref_kind}_fit_{_aspect_id(aspect)}",
                    image=source,
                    aspect=aspect,
                ).out(0)
            return source_cache[key]

        def _latent_for(ref_kind: str, aspect: str, source):
            key = (ref_kind, aspect)
            if key not in latent_cache:
                if aspect == "원본":
                    node_id = "smart_face_latent" if ref_kind == "face" else "smart_balanced_latent"
                else:
                    node_id = f"smart_{ref_kind}_latent_{_aspect_id(aspect)}"
                latent_cache[key] = graph.node(
                    "VAEEncode", id=node_id, pixels=source, vae=vae
                ).out(0)
            return latent_cache[key]

        def _negative_for(ref_kind: str, aspect: str, source):
            key = (ref_kind, aspect)
            if key not in negative_cache:
                if aspect == "원본":
                    node_id = "smart_face_neg_shared" if ref_kind == "face" else "smart_balanced_neg_shared"
                else:
                    node_id = f"smart_{ref_kind}_neg_{_aspect_id(aspect)}"
                kwargs_negative = {
                    "clip": clip,
                    "vae": vae,
                    "image1": source,
                    "prompt": self.EMPTY_NEGATIVE_PROMPT,
                }
                if ref_kind == "balanced":
                    kwargs_negative["image2"] = face_image
                negative_encode = graph.node(
                    "TextEncodeQwenImageEditPlus", id=node_id, **kwargs_negative
                )
                negative_cache[key] = graph.node(
                    "FluxKontextMultiReferenceLatentMethod",
                    id=f"{node_id}_ref",
                    conditioning=negative_encode.out(0),
                    reference_latents_method="index_timestep_zero",
                )
            return negative_cache[key]

        if model_variant == self.MODEL_VARIANT_2511 and aspect_option == "원본 비율":
            # 2511 원본 비율은 기존 그래프와 노드 ID를 그대로 유지한다.
            original_face = _source_for("face", "원본")
            _latent_for("face", "원본", original_face)
            _negative_for("face", "원본", original_face)
            if any(item["ref_kind"] == "balanced" for item in prepared_variants):
                original_balanced = _source_for("balanced", "원본")
                _latent_for("balanced", "원본", original_balanced)
                _negative_for("balanced", "원본", original_balanced)

        for index, item in enumerate(prepared_variants, start=1):
            node_id = f"smart_{index:03d}"
            ref_kind = item["ref_kind"]
            output_aspect = item.get("output_aspect", "원본")
            source = _source_for(ref_kind, output_aspect)

            if model_variant == self.MODEL_VARIANT_2511:
                latent = _latent_for(ref_kind, output_aspect, source)
                negative_conditioning = _negative_for(ref_kind, output_aspect, source)

                if ref_kind == "face":
                    positive_encode = graph.node(
                        "TextEncodeQwenImageEditPlus",
                        id=f"{node_id}_pos",
                        clip=clip,
                        vae=vae,
                        image1=source,
                        prompt=item["prompt"],
                    )
                else:
                    if balanced_image is None:
                        raise RuntimeError("내부 밸런스 참조 이미지 준비 상태가 올바르지 않습니다.")
                    positive_encode = graph.node(
                        "TextEncodeQwenImageEditPlus",
                        id=f"{node_id}_pos",
                        clip=clip,
                        vae=vae,
                        image1=source,
                        image2=face_image,
                        prompt=item["prompt"],
                    )

                positive_conditioning = graph.node(
                    "FluxKontextMultiReferenceLatentMethod",
                    id=f"{node_id}_pos_ref",
                    conditioning=positive_encode.out(0),
                    reference_latents_method="index_timestep_zero",
                )

                sampler = graph.node(
                    "KSampler",
                    id=f"{node_id}_smp",
                    model=model,
                    positive=positive_conditioning.out(0),
                    negative=negative_conditioning.out(0),
                    latent_image=latent,
                    seed=seed,
                    steps=steps,
                    cfg=cfg,
                    sampler_name=sampler_name,
                    scheduler=scheduler,
                    denoise=denoise,
                )
            else:
                qwen21_args = {
                    "clip": clip,
                    "vae": vae,
                    "prompt": self._prompt_for_model_variant(item["prompt"], model_variant),
                    "negative_prompt": self.EMPTY_NEGATIVE_PROMPT_21,
                    # 공식 2.1 이미지 편집 템플릿과 같은 정책: 첫 참조 이미지의 실제
                    # 크기/비율을 32배수로 유지해 TextEncodeQwenImage21이 시작 latent를
                    # 직접 만들게 한다. 자동 구도별일 때는 source가 이미 목표 비율이다.
                    "resolution": 0,
                    "images.image_1": source,
                }
                if ref_kind == "balanced":
                    if balanced_image is None:
                        raise RuntimeError("내부 밸런스 참조 이미지 준비 상태가 올바르지 않습니다.")
                    qwen21_args["images.image_2"] = face_image
                encode21 = graph.node(
                    "TextEncodeQwenImage21",
                    id=f"{node_id}_q21",
                    **qwen21_args,
                )
                sampler = graph.node(
                    "KSampler",
                    id=f"{node_id}_smp",
                    model=model,
                    positive=encode21.out(0),
                    negative=encode21.out(1),
                    latent_image=encode21.out(2),
                    seed=seed,
                    steps=steps,
                    cfg=cfg,
                    sampler_name=sampler_name,
                    scheduler=scheduler,
                    denoise=denoise,
                )

            decode = graph.node(
                "VAEDecode",
                id=f"{node_id}_dec",
                samples=sampler.out(0),
                vae=vae,
            )
            graph.node(
                "_UltraExactSaveImage",
                id=f"{node_id}_sav",
                images=decode.out(0),
                filename_stem=item["filename"],
                caption=item["caption"],
                date_subfolder=date_str,
                dataset_plan=dataset_plan,
            )

        return {
            "result": (),
            "expand": graph.finalize(),
        }

# ---------------------------------------------------------------------------
# Queue와 모델 로드 없이 메인 노드의 읽기 전용 미리보기를 갱신하는 경량 API
# ---------------------------------------------------------------------------
_PREVIEW_ROUTE_PATH = "/ultra_lora_reference_images/preview"


def _preview_route_already_registered() -> bool:
    """같은 POST 라우트가 이미 등록돼 있는지 확인한다.

    aiohttp는 같은 경로·메서드가 연속 등록되면 서버 시작 시
    ``RuntimeError: Added route will never be executed`` 로 ComfyUI 전체 기동을
    중단시킨다.  Reference Images가 두 번 로드되는 경우(구버전 통합 Studio와
    분리 패키지 동시 설치, 폴더 중복 복사 등)에도 ComfyUI가 뜨도록 방어한다.
    """
    try:
        for route in PromptServer.instance.routes:
            if (
                getattr(route, "path", None) == _PREVIEW_ROUTE_PATH
                and str(getattr(route, "method", "")).upper() == "POST"
            ):
                return True
    except Exception:
        return False
    return False


if PromptServer is not None and web is not None and not _preview_route_already_registered():
    @PromptServer.instance.routes.post(_PREVIEW_ROUTE_PATH)
    async def ultra_lora_reference_images_preview(request):
        try:
            payload = await request.json()
            preview = UltraSmartLoraDatasetGenerateSave.build_preview_from_payload(payload)
            return web.json_response({"preview": preview})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)


# ---------------------------------------------------------------------------
NODE_CLASS_MAPPINGS = {
    "_UltraExactSaveImage": UltraExactSaveImage,
    "_UltraAspectFitImage": UltraAspectFitImage,
    "UltraLoRAPngCaptionVariants": UltraVariantGenerateSave,
    "UltraLoRAReferenceImages": UltraSmartLoraDatasetGenerateSave,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "UltraLoRAPngCaptionVariants": "🎴 Ultra LoRA PNG & Caption Variants",
    "UltraLoRAReferenceImages": "🎴 Ultra LoRA Reference Images",
}


WEB_DIRECTORY = "./web/js"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
