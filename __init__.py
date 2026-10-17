"""ComfyUI Ultra LoRA Reference Images.

Ultra LoRA Studio 통합 배포본에서 분리된 독립 커스텀 노드 패키지입니다.
학습 앱(Ultra LoRA Studio) 없이 단독으로 동작하며, Studio 쪽 코드를 전혀
import하지 않습니다.

등록 노드 (class_type은 통합 배포본과 완전히 동일 — 기존 워크플로우 호환)
- UltraLoRAReferenceImages : 사용자용 메인 노드
- _UltraExactSaveImage     : GraphBuilder 확장 전용 내부 노드
- _UltraAspectFitImage     : GraphBuilder 확장 전용 내부 노드
"""

from __future__ import annotations

import os
import traceback

_LOG_PREFIX = "[Ultra LoRA Reference Images]"


def _find_legacy_integrated_studio() -> str | None:
    """Reference Images가 아직 내장된 구버전 Ultra LoRA Studio 폴더를 찾는다.

    구버전 통합 Studio도 같은 class_type과 같은 미리보기 라우트를 등록한다.
    둘이 함께 로드되면 로드 순서에 따라 aiohttp 중복 라우트 오류로 ComfyUI
    기동 자체가 실패할 수 있으므로, 구버전이 발견되면 이 패키지는 등록을
    양보하고 경고만 출력한다.
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        parent = os.path.dirname(here)
        for name in os.listdir(parent):
            candidate = os.path.join(parent, name)
            if candidate == here or name.endswith(".disabled"):
                continue
            if not os.path.isdir(candidate):
                continue
            init_path = os.path.join(candidate, "__init__.py")
            legacy_module = os.path.join(candidate, "ultra_lora_studio", "reference_images.py")
            if not (os.path.isfile(init_path) and os.path.isfile(legacy_module)):
                continue
            # 새 Studio 위에 덮어쓰기 설치로 옛 파일만 남은 경우는 실제로
            # 등록되지 않으므로 구버전으로 보지 않는다. 루트 __init__.py가
            # 실제로 내장 Reference Images를 import하는지까지 확인한다.
            try:
                with open(init_path, "r", encoding="utf-8", errors="ignore") as handle:
                    if "ultra_lora_studio.reference_images" in handle.read():
                        return candidate
            except OSError:
                continue
    except Exception:
        return None
    return None


def _expected_non_comfy_import_error(exc: BaseException) -> bool:
    """ComfyUI 밖에서 소스 검증용으로 import할 때의 예상 오류."""
    return isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", None) in {
        "comfy",
        "comfy.samplers",
        "folder_paths",
        "comfy_execution",
        "comfy_execution.graph_utils",
        "torch",
    }


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

_LEGACY_STUDIO = _find_legacy_integrated_studio()

if _LEGACY_STUDIO is not None:
    print(
        f"{_LOG_PREFIX} Reference Images가 내장된 구버전 Ultra LoRA Studio가 감지되었습니다: "
        f"{_LEGACY_STUDIO}\n"
        f"{_LOG_PREFIX} 중복 등록을 막기 위해 이 패키지는 노드를 등록하지 않습니다. "
        "Ultra LoRA Studio를 분리 버전으로 업데이트한 뒤 ComfyUI를 다시 시작하십시오."
    )
else:
    try:
        if __package__:
            from .reference_images import (
                UltraAspectFitImage,
                UltraExactSaveImage,
                UltraSmartLoraDatasetGenerateSave,
            )
        else:  # pragma: no cover - standalone source validation
            from reference_images import (
                UltraAspectFitImage,
                UltraExactSaveImage,
                UltraSmartLoraDatasetGenerateSave,
            )
    except Exception as exc:
        if not _expected_non_comfy_import_error(exc):
            print(f"{_LOG_PREFIX} 노드 등록 실패: {exc}")
            traceback.print_exc()
    else:
        # 통합 배포본과 같은 노출 범위를 유지한다: 메인 노드만 사용자용이고,
        # 두 underscore 보조 노드는 GraphBuilder 확장에 필요하지만 내부용이다.
        NODE_CLASS_MAPPINGS.update({
            "_UltraExactSaveImage": UltraExactSaveImage,
            "_UltraAspectFitImage": UltraAspectFitImage,
            "UltraLoRAReferenceImages": UltraSmartLoraDatasetGenerateSave,
        })
        NODE_DISPLAY_NAME_MAPPINGS["UltraLoRAReferenceImages"] = "Ultra LoRA Reference Images"

# 구버전 통합 Studio가 같은 이름의 프런트엔드 확장을 이미 제공하므로,
# 그 경우에는 JS도 내보내지 않는다(중복 registerExtension 방지).
if _LEGACY_STUDIO is None:
    WEB_DIRECTORY = "./web/js"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
]
if _LEGACY_STUDIO is None:
    __all__.append("WEB_DIRECTORY")
