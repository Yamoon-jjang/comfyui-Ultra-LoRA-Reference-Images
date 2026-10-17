# 🎴 Ultra LoRA Reference Images

**인물 사진 한두 장만 넣으면, 같은 인물의 다양한 표정·얼굴 각도·구도·의상·배경을 담은 이미지 24 / 30 / 40장을 한 번에 만들어 주는 ComfyUI 노드입니다.**

이렇게 쓸 수 있습니다.
- **캐릭터 시트 / 표정 모음:** 한 인물의 여러 표정과 각도를 한자리에 모읍니다.
- **포즈·구도 레퍼런스:** 같은 인물의 얼굴 클로즈업부터 상반신, 허벅지 위, 전신까지 고루 만듭니다.
- **인물 LoRA 학습용 데이터셋:** 이미지마다 같은 이름의 TXT 캡션이 자동으로 저장되어, 그대로 학습에 쓸 수 있습니다.

정한 장수만큼 이미지마다 **표정, 얼굴 각도, 시선, 조명, 배경**이 자동으로 바뀝니다. 밸런스 모드에서는 몸이 나오는 컷마다 **의상도 자동으로 바뀝니다.** 이 과정에서 얼굴은 같은 인물로 유지됩니다.

- ✅ **추가 pip 설치 없음**
- ✅ **로더 연결 불필요:** 모델, 텍스트 인코더, VAE, LoRA를 노드 안에서 고릅니다.
- ✅ **한국어 / 영어 UI 자동 전환**
- ✅ **예제 워크플로우와 상세 메뉴얼 포함**

---

## 📦 설치

**ComfyUI Manager (권장)**
Custom Nodes Manager에서 **`Ultra LoRA Reference Images`** 를 검색해 설치합니다.

**수동 설치**
```bash
cd ComfyUI/custom_nodes
git clone https://github.com/Yamoon-jjang/comfyui-Ultra-LoRA-Reference-Images
```
설치 후 ComfyUI를 재시작하세요.

**필요한 모델:** Qwen Image Edit 2511 또는 Qwen Image 2.1 (Diffusion Model / Text Encoder / VAE)

---

## 🚀 빠른 시작

1. `예제/Ultra-LoRA-Reference-Images.json` 워크플로우를 불러옵니다.
2. 얼굴 참조 이미지를 넣습니다. 밸런스 모드라면 같은 인물의 두 번째 이미지도 넣습니다.
3. `모델 계열`(2511 또는 2.1)을 고르고, `모델 설정`을 펼쳐 내 모델 파일을 선택합니다.
4. 생성 모드와 장수(24 / 30 / 40)를 고른 뒤 실행합니다.

결과는 `ComfyUI/output/lora_dataset/<실행 폴더>/`에 **PNG + 같은 이름의 TXT 캡션**으로 저장됩니다.

---

## 🎭 두 가지 생성 모드

### 얼굴전용: 참조 이미지 1장
얼굴 중심 컷만 만듭니다. 이미지마다 **표정, 얼굴 각도, 시선, 조명, 배경**이 바뀝니다. 표정 모음이나 얼굴 LoRA에 적합합니다. 의상은 바꾸지 않습니다.

### 밸런스: 참조 이미지 2장 (같은 인물)
얼굴, 상반신, 허벅지 위, 전신 컷을 섞어 만듭니다. 몸이 나오는 컷마다 **의상과 포즈가 바뀝니다.**

| 장수 | 얼굴 | 상반신 | 허벅지 위 | 전신 |
|---|---|---|---|---|
| 24장 | 9 | 7 | 5 | 3 |
| 30장 | 11 | 9 | 6 | 4 |
| 40장 | 14 | 12 | 8 | 6 |

> 💡 두 번째 이미지는 상반신 사진이 가장 좋습니다. 없으면 같은 인물의 얼굴 사진 2장을 넣어도 정상적으로 동작합니다.

---

## 🎛️ 변화 정도 조절

| 옵션 | 선택지 | 하는 일 |
|---|---|---|
| **표정 변화** | 낮음 / 보통 / 높음 | 웃음, 슬픔, 놀람, 화남 등 표정 후보의 폭 |
| **의상 다양성** | 낮음(4종) / 보통(8종) / 높음(12종) | 밸런스 모드 몸 컷에 쓰는 의상 후보 수 |
| **배경 옵션** | 단순 / 실내실외혼합 / 다양화 | 스튜디오, 실내, 창가, 거리, 공원, 카페 등 배경 범위 |
| **조명 옵션** | 사용안함 / 안정적분산 / 다양한분산 | 자연광, 창가광, 스튜디오, 측광 등 조명 변화 |
| **정체성 보존** | 강함 / 균형 / 자유 | 원본 얼굴을 얼마나 엄격하게 유지할지 |
| **출력 비율** | 원본 비율 / 자동 구도별 비율 | 구도에 맞춰 화면 비율 조정 |

트리거 토큰과 피사체 설명(예: `a korean woman`)을 넣으면 캡션에도 반영됩니다.

---

## ⚙️ 편의 기능

- **노드 안에서 모델 선택:** 바깥에 Loader 노드를 연결할 필요가 없습니다.
- **모델 계열별 LoRA 목록:** LoRA를 추가, 삭제, 순서 이동, ON/OFF, 강도 조절할 수 있습니다. 2511용과 2.1용 목록은 서로 섞이지 않습니다.
- **마지막 설정 자동 기억:** 2511과 2.1 각각의 모델과 LoRA 설정을 따로 기억해서, 계열을 바꾸면 그 계열에서 쓰던 설정이 그대로 돌아옵니다.
- **실행 전 생성 계획 미리보기:** 어떤 컷이 몇 장 만들어질지 먼저 확인할 수 있습니다.
- **모델별 최적 내부 처리:** 2511은 CFGNorm과 멀티 레퍼런스 latent, 2.1은 전용 캐시와 인코더를 노드가 알아서 적용합니다.

---

## ✅ 학습에 쓰기 전에

자동 생성 결과는 한 번 훑어보시길 권장합니다. 얼굴이 같은 인물로 유지됐는지, 손이나 몸이 깨진 컷은 없는지 확인하세요. 문제 있는 PNG/TXT 쌍은 빼거나 캡션을 고치면 됩니다.

자세한 설명은 [`Ultra_LoRA_Reference_Images_메뉴얼.txt`](Ultra_LoRA_Reference_Images_메뉴얼.txt)를 참고하세요.

---

## 📄 라이선스

**GNU GPL v3.0** (`LICENSE`) · Copyright (C) 2026 Yamoon-jjang

---

## 🇺🇸 English Summary

**Ultra LoRA Reference Images** turns one or two photos of a person into **24 / 30 / 40 consistent images of the same person**, each with a different expression, head angle, gaze, lighting, and background. In Balance mode, every body shot also gets a different outfit and pose. Use it for **character sheets, expression and pose references, or as a ready-made LoRA training dataset**: every PNG is saved with a matching TXT caption.

- **Face mode** (1 reference): face close-ups with varied expressions, angles, gaze, lighting, and backgrounds.
- **Balance mode** (2 references of the same person): a mix of face, upper-body, thigh-up, and full-body shots with outfit variety (4 / 8 / 12 outfit pools).
- Controls for expression range, outfit variety, background range, lighting variation, identity strength, and aspect ratio.
- Models, text encoders, VAEs, and a LoRA stack are selected inside the node. Settings are remembered separately for each model family.
- Supports **Qwen Image Edit 2511** and **Qwen Image 2.1**. No extra pip packages are needed.

License: GPL-3.0
