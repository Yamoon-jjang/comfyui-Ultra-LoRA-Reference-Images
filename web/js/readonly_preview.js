import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_CLASS = "UltraLoRAReferenceImages";
const PREVIEW_ROUTE = "/ultra_lora_reference_images/preview";
const CORE_IMAGE_PREVIEW_WIDGET = "$$canvas-image-preview";

function widgetValue(node, name, fallback = "") {
    const widget = node.widgets?.find((item) => item.name === name);
    return widget?.value ?? fallback;
}

function isInputConnected(node, name) {
    const input = node.inputs?.find((item) => item.name === name);
    return input?.link !== null && input?.link !== undefined;
}

function makePreviewPayload(node) {
    return {
        "트리거": widgetValue(node, "트리거", ""),
        "피사체": widgetValue(node, "피사체", "a korean woman"),
        "생성_모드": widgetValue(node, "생성_모드", "얼굴전용"),
        "배치수": String(widgetValue(node, "배치수", "24")),
        "조명_옵션": widgetValue(node, "조명_옵션", "안정적분산"),
        "의상_다양성": widgetValue(node, "의상_다양성", "보통"),
        "배경_옵션": widgetValue(node, "배경_옵션", "실내실외혼합"),
        "표정_변화": widgetValue(node, "표정_변화", "보통"),
        "정체성_보존": widgetValue(node, "정체성_보존", "강함"),
        "출력_비율": widgetValue(node, "출력_비율", "원본 비율"),
        "모델_계열": widgetValue(node, "모델_계열", "Qwen Image Edit 2511"),
        balanced_connected: isInputConnected(node, "밸런스_참조_이미지_상반신이상"),
    };
}

function saveOwnProperty(object, name) {
    return {
        name,
        descriptor: Object.getOwnPropertyDescriptor(object, name),
    };
}

function restoreOwnProperty(object, saved) {
    if (saved.descriptor) {
        Object.defineProperty(object, saved.name, saved.descriptor);
    } else {
        delete object[saved.name];
    }
}

app.registerExtension({
    name: "ultra_lora_reference_images.readonly_preview",

    async nodeCreated(node) {
        if (node.comfyClass !== NODE_CLASS) return;

        const textarea = document.createElement("textarea");
        textarea.readOnly = true;
        textarea.spellcheck = false;
        textarea.placeholder = "내용 미리보기 버튼을 누르면 현재 설정의 파일명·프롬프트·캡션이 표시됩니다.";
        textarea.setAttribute("aria-label", "프롬프트와 캡션 내용 미리보기");
        textarea.style.width = "100%";
        textarea.style.boxSizing = "border-box";
        textarea.style.resize = "none";
        textarea.style.overflow = "auto";
        textarea.style.whiteSpace = "pre-wrap";
        textarea.style.fontFamily = "monospace";
        textarea.style.fontSize = "12px";
        textarea.style.lineHeight = "1.4";
        textarea.style.padding = "8px";
        textarea.style.color = "var(--input-text, #ddd)";
        textarea.style.background = "var(--comfy-input-bg, #202020)";
        textarea.style.border = "1px solid var(--border-color, #555)";
        textarea.style.borderRadius = "4px";

        // ComfyUI intentionally ignores global shortcuts while a TEXTAREA has focus.
        // This preview is read-only, so forward a plain R key to ComfyUI's normal
        // node-definition refresh path instead of silently swallowing the shortcut.
        textarea.addEventListener("keydown", async (event) => {
            const isPlainRefreshKey =
                !event.isComposing &&
                !event.repeat &&
                event.key?.toLowerCase() === "r" &&
                !event.ctrlKey &&
                !event.altKey &&
                !event.metaKey &&
                !event.shiftKey;

            if (!isPlainRefreshKey) return;

            event.preventDefault();
            event.stopPropagation();
            textarea.blur();

            try {
                await app.refreshComboInNodes();
            } catch (error) {
                console.error("[Ultra LoRA Reference Images] 모델 목록 새로고침 실패", error);
            }
        });

        let requestGeneration = 0;
        let previewVisible = false;
        let previewWidget = null;
        let hiddenImagePreview = null;

        function redrawNode() {
            node.setDirtyCanvas?.(true, true);
            app.canvas?.setDirty?.(true, true);
            app.graph?.setDirtyCanvas?.(true, true);
        }

        let postExecutionRefreshGeneration = 0;

        function postExecutionRefreshPass(generation) {
            if (generation !== postExecutionRefreshGeneration) return;
            syncImagePreviewVisibility();
            redrawNode();
        }

        function schedulePostExecutionPreviewRefresh() {
            const generation = ++postExecutionRefreshGeneration;

            // Expanded/output child nodes can create ComfyUI's core
            // $$canvas-image-preview a little after this parent node finishes.
            // Redraw on the current turn and on a few bounded follow-up turns so
            // the first completed generation becomes visible without requiring
            // a workflow/tab switch.  Do not replace or duplicate the core
            // preview widget; ComfyUI remains the sole owner of the image data.
            postExecutionRefreshPass(generation);

            if (typeof requestAnimationFrame === "function") {
                requestAnimationFrame(() => {
                    postExecutionRefreshPass(generation);
                    requestAnimationFrame(() => postExecutionRefreshPass(generation));
                });
            }
            if (typeof setTimeout === "function") {
                setTimeout(() => postExecutionRefreshPass(generation), 60);
                setTimeout(() => postExecutionRefreshPass(generation), 240);
            }
        }

        function restoreImagePreviewWidget() {
            if (!hiddenImagePreview) return;

            const { widget, properties } = hiddenImagePreview;
            for (const property of properties) {
                restoreOwnProperty(widget, property);
            }
            hiddenImagePreview = null;
        }

        function collapseImagePreviewWidget(widget) {
            if (hiddenImagePreview?.widget === widget) return;

            restoreImagePreviewWidget();

            const properties = [
                saveOwnProperty(widget, "hidden"),
                saveOwnProperty(widget, "computeLayoutSize"),
                saveOwnProperty(widget, "drawWidget"),
            ];

            hiddenImagePreview = { widget, properties };

            // Keep the core image-preview widget registered so ComfyUI does not add
            // a duplicate, but remove only its current layout and drawing while the
            // read-only text screen is active. Image data and node dimensions remain
            // entirely owned by ComfyUI.
            widget.hidden = true;
            widget.computeLayoutSize = () => ({
                minWidth: 0,
                minHeight: 0,
                maxWidth: 0,
                maxHeight: 0,
            });
            widget.drawWidget = () => {};
        }

        function syncImagePreviewVisibility() {
            const imagePreviewWidget = node.widgets?.find(
                (widget) => widget?.name === CORE_IMAGE_PREVIEW_WIDGET
            );

            if (!previewVisible || !imagePreviewWidget) {
                restoreImagePreviewWidget();
                return;
            }

            collapseImagePreviewWidget(imagePreviewWidget);
        }

        function setPreviewVisible(visible) {
            previewVisible = Boolean(visible);

            // Let ComfyUI own node dimensions. The text widget participates in the
            // normal core layout, while the core image-preview widget is temporarily
            // collapsed in place so both views use the same lower preview region.
            if (previewWidget) {
                previewWidget.type = previewVisible ? "preview" : "hidden";
                previewWidget.hidden = !previewVisible;
            }

            textarea.style.display = previewVisible ? "block" : "none";
            textarea.setAttribute("aria-hidden", previewVisible ? "false" : "true");
            previewButton.name = previewVisible ? "이미지 화면으로" : "내용 미리보기";
            if (!previewVisible) textarea.blur();

            syncImagePreviewVisibility();
            redrawNode();
        }

        const previewButton = node.addWidget("button", "내용 미리보기", null, async () => {
            if (previewVisible) {
                ++requestGeneration;
                setPreviewVisible(false);
                return;
            }

            setPreviewVisible(true);
            const generation = ++requestGeneration;
            textarea.value = "내용을 불러오는 중입니다...";

            try {
                const response = await api.fetchApi(PREVIEW_ROUTE, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(makePreviewPayload(node)),
                });
                const data = await response.json();
                if (!response.ok) {
                    throw new Error(data?.error || `미리보기 요청 실패 (${response.status})`);
                }
                if (generation !== requestGeneration || !previewVisible) return;
                textarea.value = data?.preview || "표시할 내용이 없습니다.";
                textarea.scrollTop = 0;
            } catch (error) {
                if (generation !== requestGeneration || !previewVisible) return;
                textarea.value = `미리보기를 만들지 못했습니다.\n${error?.message || String(error)}`;
            }
        });

        // UI-only controls must not participate in workflow widget serialization.
        // `options.serialize = false` only excludes prompt/API serialization in current
        // ComfyUI; workflow persistence is controlled by `widget.serialize`.
        previewButton.serialize = false;
        previewButton.options = { ...(previewButton.options || {}), serialize: false };

        previewWidget = node.addDOMWidget(
            "내용_미리보기_표시",
            "preview",
            textarea,
            {
                serialize: false,
                hideOnZoom: false,
                margin: 8,
            }
        );
        previewWidget.serialize = false;
        previewWidget.options = { ...(previewWidget.options || {}), serialize: false };

        // The core image preview can be created later, including during execution.
        // Synchronize after ComfyUI's own background pass so a newly added preview
        // is immediately collapsed whenever the text screen is still selected.
        const originalOnDrawBackground = node.onDrawBackground;
        node.onDrawBackground = function (...args) {
            const result = originalOnDrawBackground?.apply(this, args);
            syncImagePreviewVisibility();
            return result;
        };

        const originalOnExecuted = node.onExecuted;
        node.onExecuted = function (...args) {
            const result = originalOnExecuted?.apply(this, args);
            schedulePostExecutionPreviewRefresh();
            return result;
        };

        const executionSuccessHandler = () => schedulePostExecutionPreviewRefresh();
        api.addEventListener?.("execution_success", executionSuccessHandler);

        const originalOnRemoved = node.onRemoved;
        node.onRemoved = function (...args) {
            ++postExecutionRefreshGeneration;
            api.removeEventListener?.("execution_success", executionSuccessHandler);
            restoreImagePreviewWidget();
            return originalOnRemoved?.apply(this, args);
        };

        setPreviewVisible(false);
    },
});
