import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_CLASS = "UltraLoRAReferenceImages";
const MODEL_WIDGET = "모델_계열";
const ROW_PREFIX = "lora_";
const ROW_HEIGHT = 26;
const MIN_NODE_WIDTH = 340;
const MAX_STRENGTH = 100;
const REFRESH_HOOK_FLAG = "__ultraRefImagesRefreshHook";
const CONTEXT_MENU_HOOK_FLAG = "__ultraRefImagesLoraMenuHook";
const LORA_COLLAPSED_PROPERTY = "ultra_ref_lora_collapsed";
const MODEL_SETTINGS_COLLAPSED_PROPERTY = "ultra_ref_model_settings_collapsed";
const NODE_SCHEMA_PROPERTY = "ultra_ref_schema";
const NODE_SCHEMA_VERSION = 5;
const NODE_SETTINGS_KEY_PROPERTY = "ultra_ref_settings_key";
const SETTINGS_STORAGE_KEY = "ultra_lora_reference_images.settings.v2";
const SETTINGS_STORE_VERSION = 2;
const MAX_STORED_NODE_KEYS = 128;
const QUEUE_HOOK_FLAG = "__ultraRefImagesQueueSettingsHook";
const LEGACY_EXTERNAL_INPUT_NAMES = new Set(["model", "clip", "vae", "모델", "CLIP / 텍스트 인코더", "VAE"]);
const PREVIEW_WIDGET_NAMES = new Set(["내용 미리보기", "이미지 화면으로", "내용_미리보기_표시", "$$canvas-image-preview"]);
const MODEL_VARIANTS = ["Qwen Image Edit 2511", "Qwen Image 2.1"];
const VARIANT_WIDGETS = {
  "Qwen Image Edit 2511": ["2511_확산모델", "2511_텍스트인코더", "2511_VAE"],
  "Qwen Image 2.1": ["2.1_확산모델", "2.1_텍스트인코더", "2.1_VAE", "2.1_캐시_장치", "2.1_캐시_정밀도"],
};

let loraListCache = null;
let loraListPromise = null;
let loraListLookupFailed = false;
let loraListLookupError = null;
let loraListCacheVersion = 0;

function isOurNode(node) {
  return node?.comfyClass === NODE_CLASS || node?.type === NODE_CLASS;
}

function isLoraRow(widget) {
  return Boolean(widget?._ultraReferenceLoraRow);
}

function isDynamicWidget(widget) {
  return isLoraRow(widget) || Boolean(widget?._ultraReferenceLoraAddButton) || Boolean(widget?._ultraReferenceLoraHeader) || Boolean(widget?._ultraReferenceModelHeader);
}

function isLoraSectionCollapsed(node) {
  node.properties = node.properties && typeof node.properties === "object" ? node.properties : {};
  return node.properties[LORA_COLLAPSED_PROPERTY] !== false;
}

function setLoraSectionCollapsed(node, collapsed) {
  node.properties = node.properties && typeof node.properties === "object" ? node.properties : {};
  node.properties[LORA_COLLAPSED_PROPERTY] = Boolean(collapsed);
}

function isModelSettingsCollapsed(node) {
  node.properties = node.properties && typeof node.properties === "object" ? node.properties : {};
  return node.properties[MODEL_SETTINGS_COLLAPSED_PROPERTY] !== false;
}

function setModelSettingsCollapsed(node, collapsed) {
  node.properties = node.properties && typeof node.properties === "object" ? node.properties : {};
  node.properties[MODEL_SETTINGS_COLLAPSED_PROPERTY] = Boolean(collapsed);
}

function currentUiLanguageTag() {
  const candidates = [
    app?.extensionManager?.locale,
    app?.ui?.settings?.getSettingValue?.("Comfy.Locale"),
    globalThis?.app?.extensionManager?.locale,
    globalThis?.app?.ui?.settings?.getSettingValue?.("Comfy.Locale"),
    globalThis?.document?.documentElement?.lang,
    globalThis?.navigator?.language,
  ];
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim().toLowerCase();
  }
  return "en";
}

function isKoreanUi() {
  return currentUiLanguageTag().startsWith("ko");
}

function tr(english, korean) {
  return isKoreanUi() ? korean : english;
}

function alertMessage(messageEn, messageKo) {
  app.extensionManager?.toast?.addAlert?.(tr(messageEn, messageKo));
}

function triggerRedraw(node, widget = null) {
  widget?.triggerDraw?.();
  node?.setDirtyCanvas?.(true, true);
  app.canvas?.setDirty?.(true, true);
}

function currentModelVariant(node) {
  const widget = node?.widgets?.find((item) => item?.name === MODEL_WIDGET);
  const value = String(widget?.value || "").trim();
  return value || "Qwen Image Edit 2511";
}

function clearLoraListCache() {
  loraListCacheVersion += 1;
  loraListCache = null;
  loraListPromise = null;
  loraListLookupFailed = false;
  loraListLookupError = null;
}

function uniqueSortedStrings(values) {
  return [...new Set((Array.isArray(values) ? values : []).map((value) => String(value || "").trim()).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b));
}

async function getLoraList(force = false) {
  if (force) clearLoraListCache();
  if (loraListCache) return loraListCache;
  if (loraListPromise) return loraListPromise;

  const requestVersion = loraListCacheVersion;
  const requestPromise = (async () => {
    const classNames = ["LoraLoaderModelOnly", "LoraLoader"];
    let hadSuccessfulLookup = false;
    let lastError = null;
    for (const className of classNames) {
      try {
        const response = await api.fetchApi(`/object_info/${className}`);
        if (!response.ok) {
          lastError = new Error(`${className} object_info failed (${response.status})`);
          continue;
        }
        hadSuccessfulLookup = true;
        const data = await response.json();
        const definition = data?.[className] || data;
        const input = definition?.input?.required?.lora_name;
        const values = Array.isArray(input?.[0]) ? input[0] : [];
        if (values.length) {
          const result = uniqueSortedStrings(values);
          if (requestVersion === loraListCacheVersion) {
            loraListCache = result;
            loraListLookupFailed = false;
            loraListLookupError = null;
          }
          return result;
        }
      } catch (error) {
        lastError = error;
        console.warn(`[Ultra LoRA Reference Images] Failed to read ${className} LoRA list.`, error);
      }
    }
    if (requestVersion === loraListCacheVersion) {
      loraListCache = [];
      loraListLookupFailed = !hadSuccessfulLookup;
      loraListLookupError = lastError;
    }
    return [];
  })();
  loraListPromise = requestPromise;
  try {
    return await requestPromise;
  } finally {
    if (loraListPromise === requestPromise) loraListPromise = null;
  }
}

function ensureRefreshHook() {
  const currentRefresh = app.refreshComboInNodes;
  if (typeof currentRefresh !== "function") return false;
  if (currentRefresh[REFRESH_HOOK_FLAG]) return true;

  const wrappedRefresh = async function ultraRefImagesRefreshHook(...args) {
    clearLoraListCache();
    return await currentRefresh.apply(this, args);
  };
  Object.defineProperty(wrappedRefresh, REFRESH_HOOK_FLAG, { value: true });
  app.refreshComboInNodes = wrappedRefresh;
  return true;
}

function normalizeRowValue(value = {}, modelVariant = "Qwen Image Edit 2511") {
  const parsedStrength = Number(value?.strength);
  const strength = Number.isFinite(parsedStrength)
    ? Math.max(-MAX_STRENGTH, Math.min(MAX_STRENGTH, parsedStrength))
    : 1.0;
  return {
    on: value?.on !== false,
    lora: String(value?.lora || ""),
    strength: Math.round(strength * 1000) / 1000,
    model_variant: String(value?.model_variant || modelVariant || "Qwen Image Edit 2511"),
  };
}

function makeMenuTree(paths) {
  const root = { children: new Map(), values: [] };
  for (const fullPath of paths) {
    const parts = String(fullPath).split(/[\\/]/g).filter(Boolean);
    let current = root;
    for (let index = 0; index < Math.max(0, parts.length - 1); index += 1) {
      const part = parts[index];
      if (!current.children.has(part)) current.children.set(part, { children: new Map(), values: [] });
      current = current.children.get(part);
    }
    current.values.push({ label: parts.at(-1) || fullPath, value: fullPath });
  }
  return root;
}

function treeToMenuOptions(tree, onChoose) {
  const options = [];
  const folders = [...tree.children.entries()].sort(([a], [b]) => a.localeCompare(b));
  for (const [folder, child] of folders) {
    options.push({
      content: `📁 ${folder}`,
      submenu: { options: treeToMenuOptions(child, onChoose) },
    });
  }
  const values = [...tree.values].sort((a, b) => a.label.localeCompare(b.label));
  for (const item of values) {
    options.push({ content: item.label, callback: () => onChoose(item.value) });
  }
  return options;
}

async function showLoraChooser(event, onChoose) {
  const loras = await getLoraList(false);
  if (!loras.length) {
    if (loraListLookupFailed) {
      console.warn("[Ultra LoRA Reference Images] LoRA list lookup failed.", loraListLookupError);
      alertMessage(
        "Could not read the LoRA list from ComfyUI. Try Refresh model and LoRA lists, then restart ComfyUI if the problem continues.",
        "ComfyUI에서 LoRA 목록을 읽지 못했습니다. 모델·LoRA 목록 새로고침을 시도하고, 계속되면 ComfyUI를 재시작하십시오.",
      );
    } else {
      alertMessage("No LoRA files were found in the models/loras folder.", "models/loras 폴더에서 LoRA 파일을 찾지 못했습니다.");
    }
    return;
  }
  new LiteGraph.ContextMenu(treeToMenuOptions(makeMenuTree(loras), onChoose), {
    event,
    title: tr("Select LoRA", "LoRA 선택"),
    scale: Math.max(1, app.canvas?.ds?.scale || 1),
  });
}

function clampStrength(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return null;
  return Math.max(-MAX_STRENGTH, Math.min(MAX_STRENGTH, parsed));
}

async function promptStrength(widget, node, event) {
  const applyValue = (value) => {
    if (value == null) return;
    const parsed = clampStrength(value);
    if (parsed == null) return;
    widget.value.strength = Math.round(parsed * 1000) / 1000;
    persistFamilySettings(node, String(widget.value.model_variant || currentModelVariant(node)));
    triggerRedraw(node, widget);
  };

  if (app.canvas?.prompt) {
    app.canvas.prompt(tr("LoRA strength", "LoRA 강도"), String(widget.value.strength), applyValue, event);
    return;
  }
  const dialog = app.extensionManager?.dialog;
  if (dialog?.prompt) {
    const value = await dialog.prompt({
      title: tr("LoRA strength", "LoRA 강도"),
      message: tr(`Enter a value between -${MAX_STRENGTH} and ${MAX_STRENGTH}.`, `-${MAX_STRENGTH}에서 ${MAX_STRENGTH} 사이의 값을 입력하십시오.`),
      defaultValue: String(widget.value.strength),
    });
    applyValue(value);
    return;
  }
  alertMessage(
    "This ComfyUI frontend cannot open the LoRA strength input dialog.",
    "현재 ComfyUI 프런트엔드에서는 LoRA 강도 입력창을 열 수 없습니다.",
  );
}

function fitText(ctx, text, maxWidth) {
  const value = String(text || "");
  if (!value) return "";
  if (ctx.measureText(value).width <= maxWidth) return value;
  const ellipsis = "…";
  let output = value;
  while (output.length > 1 && ctx.measureText(output + ellipsis).width > maxWidth) {
    output = output.slice(0, -1);
  }
  return output + ellipsis;
}

function roundedRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function getAllLoraRows(node) {
  return (node.widgets || []).filter(isLoraRow);
}

function isRowVisible(node, row) {
  return !isLoraSectionCollapsed(node) && String(row?.value?.model_variant || "") === currentModelVariant(node);
}

function rowsForVariant(node, variant) {
  const target = String(variant || "");
  return getAllLoraRows(node).filter((row) => String(row?.value?.model_variant || "") === target);
}

function rowsForCurrentModel(node) {
  return rowsForVariant(node, currentModelVariant(node));
}

function widgetByName(node, name) {
  return node.widgets?.find((widget) => widget?.name === name) || null;
}

function setWidgetHidden(widget, hidden) {
  if (!widget) return;
  if (!widget.__ultraVisibilityState) {
    widget.__ultraVisibilityState = {
      computeSize: widget.computeSize,
      draw: widget.draw,
      disabled: widget.disabled,
      hidden: widget.hidden,
    };
  }

  // Keep the original widget.type intact. ComfyUI uses widget types while
  // serializing/configuring workflows; mutating combo types to "hidden" can
  // leave a workflow-loaded node in an invalid/red state on some frontends.
  if (hidden) {
    widget.hidden = true;
    widget.disabled = true;
    widget.computeSize = () => [0, 0];
    widget.draw = () => {};
    if (widget.inputEl) {
      widget.inputEl.style.display = "none";
      widget.inputEl.setAttribute?.("aria-hidden", "true");
      widget.inputEl.tabIndex = -1;
    }
    return;
  }

  const state = widget.__ultraVisibilityState;
  widget.hidden = state.hidden;
  widget.disabled = state.disabled ?? false;
  widget.computeSize = state.computeSize;
  if (state.draw) widget.draw = state.draw;
  else {
    try { delete widget.draw; } catch (_) { widget.draw = undefined; }
  }
  if (widget.inputEl) {
    widget.inputEl.style.display = "";
    widget.inputEl.removeAttribute?.("aria-hidden");
    widget.inputEl.tabIndex = 0;
  }
}

function getVisibleLoraRows(node) {
  return rowsForCurrentModel(node).filter((row) => isRowVisible(node, row));
}

function pruneLegacyExternalInputs(node) {
  if (!Array.isArray(node?.inputs) || typeof node.removeInput !== "function") return false;
  let changed = false;
  for (let index = node.inputs.length - 1; index >= 0; index -= 1) {
    const input = node.inputs[index];
    const name = String(input?.name || "");
    const localized = String(input?.localized_name || "");
    if (LEGACY_EXTERNAL_INPUT_NAMES.has(name) || LEGACY_EXTERNAL_INPUT_NAMES.has(localized)) {
      node.removeInput(index);
      changed = true;
    }
  }
  return changed;
}

function previewAnchorIndex(node) {
  if (!Array.isArray(node?.widgets)) return -1;
  return node.widgets.findIndex((widget) => PREVIEW_WIDGET_NAMES.has(String(widget?.name || "")));
}

function moveWidgetToIndex(node, widget, targetIndex) {
  if (!Array.isArray(node?.widgets) || !widget) return;
  const current = node.widgets.indexOf(widget);
  if (current < 0) return;
  node.widgets.splice(current, 1);
  const bounded = Math.max(0, Math.min(targetIndex, node.widgets.length));
  node.widgets.splice(bounded, 0, widget);
}


function drawComfyArrow(ctx, direction, centerX, top, rowHeight) {
  // Match ComfyUI BaseSteppedWidget exactly: arrowWidth=10 and vertical
  // extent from y+5 to y+height-5. Keep the same bounding box/proportion
  // for vertical collapse arrows so they visually match native combo arrows.
  const arrowWidth = 10;
  const centerY = top + rowHeight * 0.5;
  const halfHeight = Math.max(1, (rowHeight - 10) * 0.5);
  const halfWidth = arrowWidth * 0.5;
  ctx.beginPath();
  if (direction === "right") {
    ctx.moveTo(centerX + halfWidth, centerY);
    ctx.lineTo(centerX - halfWidth, centerY - halfHeight);
    ctx.lineTo(centerX - halfWidth, centerY + halfHeight);
  } else if (direction === "left") {
    ctx.moveTo(centerX - halfWidth, centerY);
    ctx.lineTo(centerX + halfWidth, centerY - halfHeight);
    ctx.lineTo(centerX + halfWidth, centerY + halfHeight);
  } else if (direction === "down") {
    ctx.moveTo(centerX, centerY + halfHeight);
    ctx.lineTo(centerX - halfWidth, centerY - halfHeight);
    ctx.lineTo(centerX + halfWidth, centerY - halfHeight);
  } else {
    ctx.moveTo(centerX, centerY - halfHeight);
    ctx.lineTo(centerX - halfWidth, centerY + halfHeight);
    ctx.lineTo(centerX + halfWidth, centerY + halfHeight);
  }
  ctx.closePath();
  ctx.fill();
}

class UltraModelSettingsHeaderWidget {
  constructor() {
    this.name = "ultra_model_settings_section";
    this.type = "custom";
    this.value = "";
    this.options = { serialize: false };
    this.serialize = false;
    this._ultraReferenceModelHeader = true;
    Object.defineProperty(this, "width", { configurable: true, get: () => undefined, set: () => {} });
  }

  computeSize(width) {
    return [width, ROW_HEIGHT];
  }

  draw(ctx, node, width, y, height) {
    const left = 15;
    const top = y + 2;
    const rowHeight = Math.max(20, height - 4);
    const collapsed = isModelSettingsCollapsed(node);
    const variant = currentModelVariant(node);
    ctx.save();
    ctx.fillStyle = LiteGraph.WIDGET_BGCOLOR || "#222";
    ctx.strokeStyle = LiteGraph.WIDGET_OUTLINE_COLOR || "#666";
    roundedRect(ctx, left, top, width - 30, rowHeight, 5);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = LiteGraph.WIDGET_TEXT_COLOR || "#ddd";
    ctx.textBaseline = "middle";
    const family = variant === "Qwen Image 2.1" ? "2.1" : "2511";
    const midY = top + rowHeight / 2;
    drawComfyArrow(ctx, collapsed ? "right" : "down", left + 11, top, rowHeight);
    ctx.font = "12px sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(`${tr("Model settings", "모델 설정")} · ${family}`, left + 31, midY);
    drawComfyArrow(ctx, collapsed ? "down" : "up", width - 26, top, rowHeight);
    ctx.restore();
  }

  mouse(event, pos, node) {
    if (event.type !== "pointerdown" || event.button === 2) return false;
    setModelSettingsCollapsed(node, !isModelSettingsCollapsed(node));
    syncModelVisibility(node);
    return true;
  }
}

function widgetValueSafe(node, name) {
  return widgetByName(node, name)?.value;
}

class UltraLoraRowWidget {
  constructor(name, value, node) {
    this.name = name;
    this.type = "custom";
    this.options = {};
    this.value = normalizeRowValue(value, currentModelVariant(node));
    this._node = node;
    this._ultraReferenceLoraRow = true;
    this.last_y = 0;
    this._lastWidth = MIN_NODE_WIDTH;
    Object.defineProperty(this, "width", {
      configurable: true,
      get: () => undefined,
      set: () => {},
    });
  }

  computeSize(width) {
    if (!isRowVisible(this._node, this)) return [width, 0];
    return [width, ROW_HEIGHT];
  }

  draw(ctx, node, width, y, height) {
    this._node = node;
    this.last_y = y;
    this._lastWidth = width;
    if (!isRowVisible(node, this)) return;
    const left = 15;
    const right = width - 15;
    const top = y + 2;
    const rowHeight = Math.max(20, height - 4);
    const toggleWidth = 24;
    const strengthWidth = 68;
    const gap = 4;
    const loraX = left + toggleWidth + gap;
    const strengthX = right - strengthWidth;
    const loraWidth = Math.max(60, strengthX - gap - loraX);

    ctx.save();
    ctx.globalAlpha = this.value.on ? 1 : 0.5;
    ctx.fillStyle = LiteGraph.WIDGET_BGCOLOR || "#222";
    ctx.strokeStyle = LiteGraph.WIDGET_OUTLINE_COLOR || "#666";
    ctx.lineWidth = 1;

    roundedRect(ctx, left, top, toggleWidth, rowHeight, 5);
    ctx.fill();
    ctx.stroke();
    roundedRect(ctx, loraX, top, loraWidth, rowHeight, 5);
    ctx.fill();
    ctx.stroke();
    roundedRect(ctx, strengthX, top, strengthWidth, rowHeight, 5);
    ctx.fill();
    ctx.stroke();

    const midY = top + rowHeight / 2;
    ctx.textBaseline = "middle";
    ctx.font = "12px sans-serif";
    ctx.fillStyle = this.value.on ? "#8fd18f" : "#888";
    ctx.textAlign = "center";
    ctx.fillText(this.value.on ? "●" : "○", left + toggleWidth / 2, midY);

    ctx.fillStyle = LiteGraph.WIDGET_TEXT_COLOR || "#ddd";
    ctx.textAlign = "left";
    const label = this.value.lora || tr("Select LoRA", "LoRA 선택");
    ctx.fillText(fitText(ctx, label, loraWidth - 14), loraX + 7, midY);

    ctx.textAlign = "center";
    ctx.fillText(Number(this.value.strength).toFixed(2), strengthX + strengthWidth / 2, midY);
    ctx.restore();
  }

  serializeValue(node) {
    const ownVariant = String(this.value.model_variant || currentModelVariant(node));
    const rows = rowsForVariant(node, ownVariant);
    return {
      on: this.value.on !== false,
      lora: String(this.value.lora || ""),
      strength: Number(this.value.strength),
      order: Math.max(0, rows.indexOf(this)),
      model_variant: ownVariant,
    };
  }

  mouse(event, pos, node) {
    if (!isRowVisible(node, this)) return false;
    if (event.type !== "pointerdown" || event.button === 2) return false;
    const width = this._lastWidth || node.size?.[0] || MIN_NODE_WIDTH;
    const left = 15;
    const right = width - 15;
    const toggleEnd = left + 24;
    const strengthStart = right - 68;
    const x = pos[0];

    if (x >= left && x <= toggleEnd) {
      this.value.on = !this.value.on;
      persistFamilySettings(node, String(this.value.model_variant || currentModelVariant(node)));
      triggerRedraw(node, this);
      return true;
    }
    if (x >= strengthStart && x <= right) {
      promptStrength(this, node, event);
      return true;
    }
    if (x > toggleEnd && x < strengthStart) {
      showLoraChooser(event, (selected) => {
        this.value.lora = selected;
        persistFamilySettings(node, String(this.value.model_variant || currentModelVariant(node)));
        triggerRedraw(node, this);
      });
      return true;
    }
    return false;
  }
}

class UltraLoraSectionHeaderWidget {
  constructor() {
    this.name = "ultra_lora_section";
    this.type = "custom";
    this.value = "";
    this.options = { serialize: false };
    this.serialize = false;
    this._ultraReferenceLoraHeader = true;
    this.last_y = 0;
    Object.defineProperty(this, "width", {
      configurable: true,
      get: () => undefined,
      set: () => {},
    });
  }

  computeSize(width) {
    return [width, ROW_HEIGHT];
  }

  draw(ctx, node, width, y, height) {
    this._node = node;
    this.last_y = y;
    const left = 15;
    const top = y + 2;
    const rowHeight = Math.max(20, height - 4);
    const buttonWidth = width - 30;
    const rows = rowsForCurrentModel(node);
    const enabled = rows.filter((row) => row?.value?.on !== false && row?.value?.lora).length;
    const collapsed = isLoraSectionCollapsed(node);
    ctx.save();
    ctx.fillStyle = LiteGraph.WIDGET_BGCOLOR || "#222";
    ctx.strokeStyle = LiteGraph.WIDGET_OUTLINE_COLOR || "#666";
    roundedRect(ctx, left, top, buttonWidth, rowHeight, 5);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = LiteGraph.WIDGET_TEXT_COLOR || "#ddd";
    ctx.textBaseline = "middle";
    const summary = rows.length
      ? tr(`${rows.length} configured / ${enabled} enabled`, `${rows.length}개 설정 / ${enabled}개 활성`)
      : tr("not configured", "사용 안 함");
    const midY = top + rowHeight / 2;
    drawComfyArrow(ctx, collapsed ? "right" : "down", left + 11, top, rowHeight);
    ctx.font = "12px sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(`${tr("LoRA settings", "LoRA 설정")} · ${summary}`, left + 31, midY);
    drawComfyArrow(ctx, collapsed ? "down" : "up", width - 26, top, rowHeight);
    ctx.restore();
  }

  mouse(event, pos, node) {
    if (event.type !== "pointerdown" || event.button === 2) return false;
    setLoraSectionCollapsed(node, !isLoraSectionCollapsed(node));
    resizeNode(node);
    triggerRedraw(node, this);
    return true;
  }
}

class UltraLoraAddButtonWidget {
  constructor() {
    this.name = "ultra_add_lora";
    this.type = "custom";
    this.value = "";
    this.options = { serialize: false };
    this.serialize = false;
    this._ultraReferenceLoraAddButton = true;
    this.last_y = 0;
    Object.defineProperty(this, "width", {
      configurable: true,
      get: () => undefined,
      set: () => {},
    });
  }

  computeSize(width) {
    if (this._node && isLoraSectionCollapsed(this._node)) return [width, 0];
    return [width, ROW_HEIGHT];
  }

  draw(ctx, node, width, y, height) {
    this._node = node;
    this.last_y = y;
    if (isLoraSectionCollapsed(node)) return;
    const left = 15;
    const top = y + 2;
    const rowHeight = Math.max(20, height - 4);
    const buttonWidth = width - 30;
    const visibleRows = getVisibleLoraRows(node);
    ctx.save();
    ctx.fillStyle = LiteGraph.WIDGET_BGCOLOR || "#222";
    ctx.strokeStyle = LiteGraph.WIDGET_OUTLINE_COLOR || "#666";
    roundedRect(ctx, left, top, buttonWidth, rowHeight, 5);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = LiteGraph.WIDGET_TEXT_COLOR || "#ddd";
    ctx.font = "12px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const label = tr(
      `＋ Add LoRA (${visibleRows.length})`,
      `＋ LoRA 추가 (${visibleRows.length})`,
    );
    ctx.fillText(label, width / 2, top + rowHeight / 2);
    ctx.restore();
  }

  mouse(event, pos, node) {
    if (isLoraSectionCollapsed(node)) return false;
    if (event.type !== "pointerdown" || event.button === 2) return false;
    showLoraChooser(event, (selected) => addLoraRow(node, { lora: selected, model_variant: currentModelVariant(node) }));
    return true;
  }
}

function removeDynamicWidgets(node) {
  if (!Array.isArray(node.widgets)) return;
  for (let index = node.widgets.length - 1; index >= 0; index -= 1) {
    if (isDynamicWidget(node.widgets[index])) node.widgets.splice(index, 1);
  }
}

function ensureCounter(node, name) {
  const match = String(name || "").match(/^lora_(\d+)$/i);
  if (match) node._ultraReferenceLoraCounter = Math.max(node._ultraReferenceLoraCounter || 0, Number(match[1]));
}

function nextRowName(node) {
  node._ultraReferenceLoraCounter = (node._ultraReferenceLoraCounter || 0) + 1;
  return `${ROW_PREFIX}${node._ultraReferenceLoraCounter}`;
}

function ensureModelSettingsHeader(node) {
  let header = (node.widgets || []).find((widget) => widget?._ultraReferenceModelHeader);
  if (!header) header = node.addCustomWidget(new UltraModelSettingsHeaderWidget());
  const modelWidget = widgetByName(node, MODEL_WIDGET);
  if (modelWidget) {
    const modelIndex = node.widgets.indexOf(modelWidget);
    moveWidgetToIndex(node, header, modelIndex + 1);
  }
  return header;
}

function placeDynamicSections(node) {
  ensureModelSettingsHeader(node);
  const header = (node.widgets || []).find((widget) => widget?._ultraReferenceLoraHeader);
  const button = (node.widgets || []).find((widget) => widget?._ultraReferenceLoraAddButton);
  const rows = getAllLoraRows(node);
  const ordered = [header, ...rows, button].filter(Boolean);
  // 먼저 동적 LoRA 위젯을 제거한 뒤 preview 위치를 다시 계산해야 한다.
  // 제거 전 인덱스를 재사용하면 앞쪽 위젯 수만큼 위치가 밀려 preview 아래로 내려간다.
  for (const widget of ordered) {
    const current = node.widgets.indexOf(widget);
    if (current >= 0) node.widgets.splice(current, 1);
  }
  const anchor = previewAnchorIndex(node);
  let index = anchor >= 0 ? anchor : node.widgets.length;
  for (const widget of ordered) node.widgets.splice(index++, 0, widget);
}

function dynamicSectionsNeedRepair(node) {
  if (!Array.isArray(node?.widgets)) return false;
  const previewIndexes = node.widgets
    .map((widget, index) => PREVIEW_WIDGET_NAMES.has(String(widget?.name || "")) ? index : -1)
    .filter((index) => index >= 0);
  if (!previewIndexes.length) return false;
  const firstPreview = Math.min(...previewIndexes);
  const dynamicIndexes = node.widgets
    .map((widget, index) => (Boolean(widget?._ultraReferenceLoraHeader) || isLoraRow(widget) || Boolean(widget?._ultraReferenceLoraAddButton)) ? index : -1)
    .filter((index) => index >= 0);
  return dynamicIndexes.some((index) => index > firstPreview);
}

function attachDynamicLayoutRepairHook(node) {
  if (node.__ultraReferenceDynamicLayoutRepairHook) return;
  const originalOnDrawBackground = node.onDrawBackground;
  let repairScheduled = false;
  node.onDrawBackground = function ultraReferenceDynamicLayoutRepair(...args) {
    const result = originalOnDrawBackground?.apply(this, args);
    if (!repairScheduled && dynamicSectionsNeedRepair(this)) {
      repairScheduled = true;
      const repair = () => {
        repairScheduled = false;
        placeDynamicSections(this);
        resizeNode(this);
      };
      if (typeof requestAnimationFrame === "function") requestAnimationFrame(repair);
      else if (typeof setTimeout === "function") setTimeout(repair, 0);
      else repair();
    }
    return result;
  };
  node.__ultraReferenceDynamicLayoutRepairHook = true;
}

function scheduleLayoutSync(node) {
  const run = () => {
    pruneLegacyExternalInputs(node);
    ensureModelSettingsHeader(node);
    placeDynamicSections(node);
    syncModelVisibility(node);
  };
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(run);
  else if (typeof setTimeout === "function") setTimeout(run, 0);
  else run();
}

function ensureSectionHeader(node) {
  let header = (node.widgets || []).find((widget) => widget?._ultraReferenceLoraHeader);
  if (!header) header = node.addCustomWidget(new UltraLoraSectionHeaderWidget());
  const index = node.widgets.indexOf(header);
  if (index >= 0) node.widgets.splice(index, 1);
  const firstRowIndex = node.widgets.findIndex((widget) => isLoraRow(widget));
  if (firstRowIndex >= 0) node.widgets.splice(firstRowIndex, 0, header);
  else node.widgets.push(header);
  return header;
}

function ensureAddButton(node) {
  ensureSectionHeader(node);
  let button = (node.widgets || []).find((widget) => widget?._ultraReferenceLoraAddButton);
  if (!button) button = node.addCustomWidget(new UltraLoraAddButtonWidget());
  button._node = node;
  const index = node.widgets.indexOf(button);
  if (index >= 0) node.widgets.splice(index, 1);
  node.widgets.push(button);
  return button;
}

function addLoraRow(node, value = {}, name = null, resize = true) {
  const widgetName = name || nextRowName(node);
  ensureCounter(node, widgetName);
  const row = node.addCustomWidget(new UltraLoraRowWidget(widgetName, value, node));
  const button = ensureAddButton(node);
  const rowIndex = node.widgets.indexOf(row);
  const buttonIndex = node.widgets.indexOf(button);
  if (rowIndex > buttonIndex) {
    node.widgets.splice(rowIndex, 1);
    node.widgets.splice(buttonIndex, 0, row);
  }
  // Keep the entire LoRA block contiguous and ahead of every preview widget.
  // ComfyUI may add its core image preview after execution, so merely placing
  // the new row before the add button is not enough.
  placeDynamicSections(node);
  if (resize) resizeNode(node);
  triggerRedraw(node, row);
  if (resize) persistFamilySettings(node, String(row.value.model_variant || currentModelVariant(node)));
  return row;
}

function resizeNode(node) {
  const computed = node.computeSize?.() || [MIN_NODE_WIDTH, node.size?.[1] || 100];
  const previous = node.size || [MIN_NODE_WIDTH, computed[1]];
  node.size = [Math.max(MIN_NODE_WIDTH, previous[0] || 0, computed[0] || 0), Math.max(computed[1] || 0, 100)];
  triggerRedraw(node);
}

function moveRow(node, row, direction) {
  const rows = getVisibleLoraRows(node);
  const current = rows.indexOf(row);
  const target = current + direction;
  if (current < 0 || target < 0 || target >= rows.length) return;
  const other = rows[target];
  const currentWidgetIndex = node.widgets.indexOf(row);
  const otherWidgetIndex = node.widgets.indexOf(other);
  node.widgets[currentWidgetIndex] = other;
  node.widgets[otherWidgetIndex] = row;
  persistFamilySettings(node, String(row.value.model_variant || currentModelVariant(node)));
  triggerRedraw(node, row);
}

function removeRow(node, row) {
  const family = String(row?.value?.model_variant || currentModelVariant(node));
  const index = node.widgets.indexOf(row);
  if (index >= 0) node.widgets.splice(index, 1);
  placeDynamicSections(node);
  resizeNode(node);
  persistFamilySettings(node, family);
}

function getLoraRowMenuItems(node, row) {
  const rows = getVisibleLoraRows(node);
  const index = rows.indexOf(row);
  return [
    {
      content: row.value.on ? tr("Disable this LoRA", "이 LoRA 비활성화") : tr("Enable this LoRA", "이 LoRA 활성화"),
      callback: () => {
        row.value.on = !row.value.on;
        persistFamilySettings(node, String(row.value.model_variant || currentModelVariant(node)));
        triggerRedraw(node, row);
      },
    },
    {
      content: tr("Move LoRA up", "LoRA 위로 이동"),
      disabled: index <= 0,
      callback: () => moveRow(node, row, -1),
    },
    {
      content: tr("Move LoRA down", "LoRA 아래로 이동"),
      disabled: index < 0 || index >= rows.length - 1,
      callback: () => moveRow(node, row, 1),
    },
    {
      content: tr("Remove only this LoRA", "이 LoRA만 삭제"),
      callback: () => removeRow(node, row),
    },
    null,
    {
      content: tr("Refresh LoRA list", "LoRA 목록 새로고침"),
      callback: async () => {
        await getLoraList(true);
        triggerRedraw(node, row);
      },
    },
  ];
}

function rowUnderPointer(node, canvas = app.canvas) {
  const graphMouse = canvas?.graph_mouse || canvas?.graphMouse;
  if (!graphMouse || !node?.pos) return null;
  const localX = graphMouse[0] - node.pos[0];
  const localY = graphMouse[1] - node.pos[1];
  if (localX < 0 || localX > (node.size?.[0] || 0)) return null;
  for (const row of getVisibleLoraRows(node)) {
    const y = Number(row.last_y || 0);
    if (localY >= y && localY <= y + ROW_HEIGHT) return row;
  }
  return null;
}

function ensureLoraContextMenuHook() {
  const canvas = app.canvas;
  const currentGetNodeMenuOptions = canvas?.getNodeMenuOptions;
  if (typeof currentGetNodeMenuOptions !== "function") return false;
  if (currentGetNodeMenuOptions[CONTEXT_MENU_HOOK_FLAG]) return true;

  const wrappedGetNodeMenuOptions = function ultraRefImagesLoraContextMenu(node, ...args) {
    if (isOurNode(node)) {
      const row = rowUnderPointer(node, this);
      if (row) return getLoraRowMenuItems(node, row);
    }
    return currentGetNodeMenuOptions.call(this, node, ...args);
  };
  Object.defineProperty(wrappedGetNodeMenuOptions, CONTEXT_MENU_HOOK_FLAG, { value: true });
  canvas.getNodeMenuOptions = wrappedGetNodeMenuOptions;
  return true;
}

function standardWorkflowWidgets(node) {
  return (node?.widgets || []).filter((widget) => {
    if (!widget || isDynamicWidget(widget)) return false;
    if (PREVIEW_WIDGET_NAMES.has(String(widget.name || ""))) return false;
    if (widget.serialize === false) return false;
    return true;
  });
}

function cleanNamedStandardValues(info, standardWidgets, fallbackValues = []) {
  const named = info?.widgets_values_named;
  const hasNamed = named && typeof named === "object" && !Array.isArray(named);
  const ordered = [];
  const filteredNamed = {};
  for (let index = 0; index < standardWidgets.length; index += 1) {
    const widget = standardWidgets[index];
    const name = String(widget?.name || "");
    let value;
    if (hasNamed && name && Object.prototype.hasOwnProperty.call(named, name)) value = named[name];
    else value = fallbackValues[index];
    ordered.push(value);
    if (name && value !== undefined) filteredNamed[name] = value;
  }
  return { ordered, named: filteredNamed, hasNamed };
}

function currentHasSeedControlWidget(standardWidgets) {
  return standardWidgets.some((widget) => {
    const name = String(widget?.name || "");
    return name === "control_after_generate" || name === "seed_control_after_generate";
  });
}

function normalizeSeedControlLayout(values, standardWidgets) {
  const base = Array.isArray(values) ? [...values] : [];
  const controlValues = new Set(["fixed", "increment", "decrement", "randomize"]);
  const hasSavedControl = controlValues.has(String(base[10] ?? ""));
  const wantsControl = currentHasSeedControlWidget(standardWidgets);
  if (hasSavedControl && !wantsControl) base.splice(10, 1);
  else if (!hasSavedControl && wantsControl) {
    const widget = standardWidgets.find((item) => {
      const name = String(item?.name || "");
      return name === "control_after_generate" || name === "seed_control_after_generate";
    });
    base.splice(10, 0, String(widget?.value || "randomize"));
  }
  return base;
}

function migrateBaseSavedValues(values, standardCount, standardWidgets = []) {
  const source = Array.isArray(values) ? values : [];
  let base = source.filter(
    (value) => !(value && typeof value === "object" && typeof value.lora === "string"),
  );
  base = normalizeSeedControlLayout(base, standardWidgets);

  // Early 2.1/LoRA candidate builds serialized the two read-only preview widgets
  // before a JS-added model-family value: [... base17, "", "", modelVariant].
  // Current builds use a real backend model widget, so normalize that legacy shape.
  if (
    base.length >= 20 &&
    String(base[17] ?? "") === "" &&
    String(base[18] ?? "") === "" &&
    MODEL_VARIANTS.includes(String(base[19] ?? ""))
  ) {
    return [...base.slice(0, 17), String(base[19])].slice(0, Math.max(0, standardCount));
  }

  // v1.6.3 appended two user-facing 2511 CFGNorm values after the current
  // 26 execution widgets. CFGNorm is now an internal fixed 2511 detail, so
  // slicing to 26 also safely discards those legacy bool/strength values.
  // Preview/button widgets are non-execution UI. Never let extra serialized UI
  // placeholders shift dynamic LoRA rows or future backend widgets.
  return base.slice(0, Math.max(0, standardCount));
}

function parseDynamicValues(values) {
  const result = [];
  const source = Array.isArray(values) ? values : [];
  for (const value of source) {
    if (value && typeof value === "object" && typeof value.lora === "string") {
      const parsedOrder = Number(value.order);
      result.push({
        value: normalizeRowValue(value, String(value.model_variant || "Qwen Image Edit 2511")),
        order: Number.isFinite(parsedOrder) ? parsedOrder : result.length,
      });
    }
  }
  result.sort((a, b) => a.order - b.order);
  return result.map((entry) => entry.value);
}


function cloneJsonValue(value) {
  try { return JSON.parse(JSON.stringify(value)); } catch (_) { return value; }
}

function emptySettingsStore() {
  return { version: SETTINGS_STORE_VERSION, globalByFamily: {}, byNode: {} };
}

function normalizeStoredPayload(payload) {
  if (!payload || typeof payload !== "object") return null;
  const family = String(payload.family || "");
  if (!MODEL_VARIANTS.includes(family)) return null;
  const allowed = new Set(VARIANT_WIDGETS[family] || []);
  const standard = {};
  for (const [name, value] of Object.entries(payload.standard || {})) {
    if (allowed.has(name)) standard[name] = cloneJsonValue(value);
  }
  const loras = Array.isArray(payload.loras)
    ? payload.loras.map((value) => normalizeRowValue(value, family)).filter((value) => value.lora)
    : [];
  return { family, standard, loras, updatedAt: Number(payload.updatedAt || 0) || 0 };
}

function normalizeSettingsStore(value) {
  const source = value && typeof value === "object" ? value : {};
  const out = emptySettingsStore();
  for (const [family, payload] of Object.entries(source.globalByFamily || {})) {
    const normalized = normalizeStoredPayload(payload);
    if (normalized) out.globalByFamily[family] = normalized;
  }
  for (const [nodeKey, families] of Object.entries(source.byNode || {})) {
    const item = {};
    for (const [family, payload] of Object.entries(families || {})) {
      const normalized = normalizeStoredPayload(payload);
      if (normalized) item[family] = normalized;
    }
    if (Object.keys(item).length) out.byNode[nodeKey] = item;
  }
  return out;
}

function readSettingsStore() {
  try {
    const raw = globalThis?.localStorage?.getItem?.(SETTINGS_STORAGE_KEY);
    return raw ? normalizeSettingsStore(JSON.parse(raw)) : emptySettingsStore();
  } catch (_) {
    return emptySettingsStore();
  }
}

function storeEntryTimestamp(entry) {
  let latest = 0;
  for (const payload of Object.values(entry || {})) {
    const timestamp = Number(payload?.updatedAt || 0);
    if (Number.isFinite(timestamp)) latest = Math.max(latest, timestamp);
  }
  return latest;
}

function writeSettingsStore(store) {
  try {
    const normalized = normalizeSettingsStore(store);
    const keys = Object.keys(normalized.byNode || {});
    if (keys.length > MAX_STORED_NODE_KEYS) {
      keys.sort((a, b) => storeEntryTimestamp(normalized.byNode[b]) - storeEntryTimestamp(normalized.byNode[a]));
      for (const key of keys.slice(MAX_STORED_NODE_KEYS)) delete normalized.byNode[key];
    }
    globalThis?.localStorage?.setItem?.(SETTINGS_STORAGE_KEY, JSON.stringify(normalized));
  } catch (error) {
    console.warn("[Ultra LoRA Reference Images] 모델별 설정 저장 실패", error);
  }
}

function generateNodeSettingsKey() {
  try {
    const value = globalThis?.crypto?.randomUUID?.();
    if (value) return String(value);
  } catch (_) {}
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

function getNodeStorageId(node) {
  if (!node) return null;
  node.properties = node.properties && typeof node.properties === "object" ? node.properties : {};
  let key = String(node.properties[NODE_SETTINGS_KEY_PROPERTY] || "");
  if (!key) {
    key = generateNodeSettingsKey();
    node.properties[NODE_SETTINGS_KEY_PROPERTY] = key;
  }
  const nodeId = node.id == null ? "new" : String(node.id);
  return `${key}:${nodeId}`;
}

function captureFamilySettings(node, family) {
  const target = String(family || "");
  if (!MODEL_VARIANTS.includes(target)) return null;
  const standard = {};
  for (const name of VARIANT_WIDGETS[target] || []) {
    const widget = widgetByName(node, name);
    if (widget) standard[name] = cloneJsonValue(widget.value);
  }
  const loras = rowsForVariant(node, target)
    .map((row, order) => ({ ...normalizeRowValue(row?.value, target), order }))
    .filter((row) => row.lora);
  return { family: target, standard, loras, updatedAt: Date.now() };
}

function persistFamilySettings(node, family, { globalFallback = true } = {}) {
  if (node?.__ultraRestoringFamilySettings) return false;
  const payload = captureFamilySettings(node, family);
  if (!payload) return false;
  const store = readSettingsStore();
  const storageId = getNodeStorageId(node);
  if (storageId) {
    if (!store.byNode[storageId] || typeof store.byNode[storageId] !== "object") store.byNode[storageId] = {};
    store.byNode[storageId][payload.family] = payload;
  }
  if (globalFallback) store.globalByFamily[payload.family] = payload;
  writeSettingsStore(store);
  return true;
}

function persistAllFamilySettings(node) {
  let changed = false;
  for (const family of MODEL_VARIANTS) changed = persistFamilySettings(node, family) || changed;
  return changed;
}

function seedMissingFamilySettings(node) {
  const store = readSettingsStore();
  const storageId = getNodeStorageId(node);
  let changed = false;
  for (const family of MODEL_VARIANTS) {
    const existing = storageId ? store.byNode?.[storageId]?.[family] : null;
    if (existing) continue;
    const payload = captureFamilySettings(node, family);
    if (!payload) continue;
    if (storageId) {
      if (!store.byNode[storageId] || typeof store.byNode[storageId] !== "object") store.byNode[storageId] = {};
      store.byNode[storageId][family] = payload;
    }
    if (!store.globalByFamily[family]) store.globalByFamily[family] = payload;
    changed = true;
  }
  if (changed) writeSettingsStore(store);
  return changed;
}

function getStoredSettings(node, family) {
  const target = String(family || "");
  if (!MODEL_VARIANTS.includes(target)) return null;
  const store = readSettingsStore();
  const storageId = getNodeStorageId(node);
  const perNode = storageId ? store.byNode?.[storageId]?.[target] : null;
  if (perNode) return perNode;
  return store.globalByFamily?.[target] || null;
}

function widgetOptionValues(widget) {
  const values = widget?.options?.values;
  if (Array.isArray(values)) return values.map((value) => String(value));
  return [];
}

function setWidgetValue(node, widget, nextValue) {
  if (!widget || nextValue === undefined) return false;
  let value = cloneJsonValue(nextValue);
  const options = widgetOptionValues(widget);
  if (options.length && !options.includes(String(value))) return false;
  if (options.length) value = String(value);
  const changed = JSON.stringify(widget.value) !== JSON.stringify(value);
  widget.value = value;
  if (changed && typeof widget.callback === "function") {
    try { widget.callback.call(widget, widget.value, app.canvas, node); }
    catch (error) { console.warn(`[Ultra LoRA Reference Images] '${widget.name}' 복원 callback 실패`, error); }
  }
  return changed;
}

async function prepareStoredLoras(rows, family) {
  const normalized = Array.isArray(rows)
    ? rows.map((row) => normalizeRowValue(row, family)).filter((row) => row.lora)
    : [];
  const available = await getLoraList(false);
  if (loraListLookupFailed) return normalized;
  return normalized.filter((row) => available.includes(String(row.lora)));
}

function replaceLoraRowsForFamily(node, family, rows) {
  for (let index = (node.widgets || []).length - 1; index >= 0; index -= 1) {
    const widget = node.widgets[index];
    if (isLoraRow(widget) && String(widget?.value?.model_variant || "") === String(family)) {
      node.widgets.splice(index, 1);
    }
  }
  for (const row of rows || []) addLoraRow(node, { ...row, model_variant: family }, null, false);
  ensureAddButton(node);
  placeDynamicSections(node);
  resizeNode(node);
}

async function applyStoredSettingsForFamily(node, family, restoreToken) {
  const stored = getStoredSettings(node, family);
  if (!stored) return false;
  const loras = await prepareStoredLoras(stored.loras || [], family);
  const isCurrent = () =>
    Number(node.__ultraSettingsRestoreToken || 0) === Number(restoreToken) &&
    currentModelVariant(node) === String(family);
  if (!isCurrent()) return false;

  node.__ultraRestoringFamilySettings = true;
  try {
    for (const [name, value] of Object.entries(stored.standard || {})) {
      setWidgetValue(node, widgetByName(node, name), value);
    }
    if (!isCurrent()) return false;
    replaceLoraRowsForFamily(node, family, loras);
  } finally {
    node.__ultraRestoringFamilySettings = false;
  }
  if (!isCurrent()) return false;
  syncModelVisibility(node);
  scheduleLayoutSync(node);
  triggerRedraw(node);
  return true;
}

function attachFamilyWidgetPersistenceHooks(node) {
  for (const [family, names] of Object.entries(VARIANT_WIDGETS)) {
    for (const name of names) {
      const widget = widgetByName(node, name);
      if (!widget || widget.__ultraReferencePersistHook) continue;
      const originalCallback = typeof widget.callback === "function" ? widget.callback : null;
      widget.callback = function ultraReferencePersistWidgetCallback(value, ...args) {
        const result = originalCallback ? originalCallback.call(this, value, ...args) : undefined;
        if (!node.__ultraRestoringFamilySettings) persistFamilySettings(node, family);
        return result;
      };
      widget.__ultraReferencePersistHook = true;
    }
  }
}

function ensureQueueSettingsHook() {
  const candidates = [[api, "queuePrompt"], [app, "queuePrompt"]];
  for (const [target, methodName] of candidates) {
    const original = target?.[methodName];
    if (typeof original !== "function") continue;
    if (original[QUEUE_HOOK_FLAG]) return true;
    const wrapped = async function ultraReferenceQueueSettingsHook(...args) {
      for (const node of app.graph?._nodes || []) if (isOurNode(node)) persistAllFamilySettings(node);
      return await original.apply(this, args);
    };
    Object.defineProperty(wrapped, QUEUE_HOOK_FLAG, { value: true });
    target[methodName] = wrapped;
    return true;
  }
  return false;
}

function normalizeSerializedNodeSize(value) {
  if (!Array.isArray(value) || value.length < 2) return null;
  const width = Number(value[0]);
  const height = Number(value[1]);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return null;
  return [width, height];
}

function restoreSerializedNodeGeometry(node) {
  const size = normalizeSerializedNodeSize(node?.__ultraSerializedWorkflowSize);
  if (!size) return false;
  node.size = [...size];
  triggerRedraw(node);
  return true;
}

function syncModelVisibility(node) {
  const current = currentModelVariant(node);
  const expanded = !isModelSettingsCollapsed(node);
  const visible = new Set(expanded ? (VARIANT_WIDGETS[current] || []) : []);
  for (const names of Object.values(VARIANT_WIDGETS)) {
    for (const name of names) {
      const widget = widgetByName(node, name);
      setWidgetHidden(widget, !visible.has(name));
    }
  }
  ensureModelSettingsHeader(node);
  placeDynamicSections(node);
  triggerRedraw(node);
  resizeNode(node);
}

function attachModelVariantHook(node) {
  const widget = node.widgets?.find((item) => item?.name === MODEL_WIDGET);
  if (!widget || widget.__ultraReferenceImagesModelHook) return;
  const originalCallback = typeof widget.callback === "function" ? widget.callback : null;
  node.__ultraCurrentModelVariant = currentModelVariant(node);
  widget.callback = function ultraReferenceImagesModelVariantCallback(value, ...args) {
    const previousFamily = String(node.__ultraCurrentModelVariant || currentModelVariant(node));
    if (!node.__ultraRestoringFamilySettings) persistFamilySettings(node, previousFamily);
    const result = originalCallback ? originalCallback.call(this, value, ...args) : undefined;
    const nextFamily = String(value || widget.value || currentModelVariant(node));
    node.__ultraCurrentModelVariant = nextFamily;
    syncModelVisibility(node);
    scheduleLayoutSync(node);
    const restoreToken = Number(node.__ultraSettingsRestoreToken || 0) + 1;
    node.__ultraSettingsRestoreToken = restoreToken;
    Promise.resolve(applyStoredSettingsForFamily(node, nextFamily, restoreToken)).catch((error) => {
      console.warn("[Ultra LoRA Reference Images] 모델별 설정 복원 실패", error);
    });
    return result;
  };
  widget.__ultraReferenceImagesModelHook = true;
}

app.registerExtension({
  name: "Ultra.LoRA.ReferenceImages.DynamicLoRA",

  setup() {
    ensureRefreshHook();
    ensureLoraContextMenuHook();
    ensureQueueSettingsHook();
  },

  async beforeRegisterNodeDef(nodeType, nodeData) {
    ensureRefreshHook();
    ensureLoraContextMenuHook();
    if (nodeData?.name !== NODE_CLASS && nodeType?.comfyClass !== NODE_CLASS) return;

    const originalConfigure = nodeType.prototype.configure;
    nodeType.prototype.configure = function configureUltraReferenceImages(info) {
      this.__ultraConfiguredFromWorkflow = true;
      this.__ultraSerializedWorkflowSize = normalizeSerializedNodeSize(info?.size);
      removeDynamicWidgets(this);
      const savedValues = Array.isArray(info?.widgets_values) ? [...info.widgets_values] : [];
      const standardWidgets = standardWorkflowWidgets(this);
      const standardCount = standardWidgets.length;
      const migratedBase = migrateBaseSavedValues(savedValues, standardCount, standardWidgets);
      // Current ComfyUI writes widgets_values_named in addition to the legacy positional
      // array. Rebuild the execution widgets by name whenever that map is available.
      // This self-heals workflows previously polluted by UI-only preview/header widgets
      // and avoids seed control_after_generate 25/26-widget layout differences.
      const cleaned = cleanNamedStandardValues(info, standardWidgets, migratedBase);
      const baseInfo = {
        ...info,
        properties: {
          ...(info?.properties || {}),
          [NODE_SCHEMA_PROPERTY]: NODE_SCHEMA_VERSION,
        },
        widgets_values: cleaned.ordered.slice(0, standardCount),
        widgets_values_named: cleaned.named,
      };
      const result = originalConfigure?.call(this, baseInfo);
      this._ultraReferenceLoraCounter = 0;
      this.properties = this.properties && typeof this.properties === "object" ? this.properties : {};
      this.properties[NODE_SCHEMA_PROPERTY] = NODE_SCHEMA_VERSION;
      getNodeStorageId(this);
      if (!(LORA_COLLAPSED_PROPERTY in this.properties)) setLoraSectionCollapsed(this, true);
      if (!(MODEL_SETTINGS_COLLAPSED_PROPERTY in this.properties)) setModelSettingsCollapsed(this, true);
      pruneLegacyExternalInputs(this);
      ensureModelSettingsHeader(this);
      ensureSectionHeader(this);
      ensureAddButton(this);
      const dynamicValues = parseDynamicValues(savedValues);
      for (const value of dynamicValues) addLoraRow(this, value, null, false);
      attachFamilyWidgetPersistenceHooks(this);
      attachModelVariantHook(this);
      attachDynamicLayoutRepairHook(this);
      syncModelVisibility(this);
      scheduleLayoutSync(this);
      this.__ultraCurrentModelVariant = currentModelVariant(this);
      // Preserve true last-used model-family memory across a full ComfyUI restart.
      // A saved workflow initializes a family only when no remembered state exists;
      // it must not overwrite newer unsaved settings that were already used.
      seedMissingFamilySettings(this);
      const restoreToken = Number(this.__ultraSettingsRestoreToken || 0) + 1;
      this.__ultraSettingsRestoreToken = restoreToken;
      Promise.resolve(applyStoredSettingsForFamily(this, this.__ultraCurrentModelVariant, restoreToken)).catch((error) => {
        console.warn("[Ultra LoRA Reference Images] 마지막 사용 설정 복원 실패", error);
      });
      return result;
    };
  },

  loadedGraphNode(node) {
    if (!isOurNode(node)) return;
    node.__ultraLoadedFromGraph = true;
    node.__ultraConfiguredFromWorkflow = true;
    restoreSerializedNodeGeometry(node);
    seedMissingFamilySettings(node);
  },

  nodeCreated(node) {
    if (!isOurNode(node)) return;
    ensureRefreshHook();
    ensureLoraContextMenuHook();
    ensureQueueSettingsHook();
    node.serialize_widgets = true;
    if (node.__ultraConfiguredFromWorkflow !== true) node.__ultraConfiguredFromWorkflow = false;
    if (node.__ultraLoadedFromGraph !== true) node.__ultraLoadedFromGraph = false;
    node._ultraReferenceLoraCounter = node._ultraReferenceLoraCounter || 0;
    node.properties = node.properties && typeof node.properties === "object" ? node.properties : {};
    node.properties[NODE_SCHEMA_PROPERTY] = NODE_SCHEMA_VERSION;
    getNodeStorageId(node);
    if (!(LORA_COLLAPSED_PROPERTY in node.properties)) setLoraSectionCollapsed(node, true);
    if (!(MODEL_SETTINGS_COLLAPSED_PROPERTY in node.properties)) setModelSettingsCollapsed(node, true);
    pruneLegacyExternalInputs(node);
    ensureModelSettingsHeader(node);
    ensureSectionHeader(node);
    ensureAddButton(node);
    attachFamilyWidgetPersistenceHooks(node);
    attachModelVariantHook(node);
    attachDynamicLayoutRepairHook(node);
    syncModelVisibility(node);
    scheduleLayoutSync(node);
    node.__ultraCurrentModelVariant = currentModelVariant(node);
    getLoraList(false);
  },

  getNodeMenuItems(node) {
    if (!isOurNode(node)) return [];
    const row = rowUnderPointer(node);
    if (row) return getLoraRowMenuItems(node, row);
    return [
      {
        content: tr("Refresh LoRA list", "LoRA 목록 새로고침"),
        callback: async () => {
          await getLoraList(true);
          triggerRedraw(node);
        },
      },
    ];
  },
});
