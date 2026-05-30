# DR-00c: 대상 회의실 하드웨어 스펙 확인

## 1. 가정 (Assumption)

- 회의실에 전용 PC 1대가 상주하며, 해당 PC에서 STT·분석·UI 렌더링이 모두 실행됨
- 외부 디스플레이(TV 또는 대형 모니터) 연결 가능하고, 팀 전원이 화면을 공유할 수 있음
- 인터넷 유선 또는 무선 접속 가능 (Claude CLI 호출을 위한 외부 네트워크 필수)
- 마이크: 회의실 기본 마이크(단일 채널) 또는 USB 외장 마이크 중 하나
- 의존성: 없음 — 단, DR-03(STT 모델 선택)은 이 DR의 확인 결과를 선행 조건으로 요구함

## 2. 작업 가설 (Working Hypothesis)

회의실 PC 스펙을 단일 하드웨어 프로파일로 확정하여, DR-03(STT 레이턴시 기준)과 DR-05(트리거 쿨다운 설계)의 수치 앵커로 사용한다.

추정 최소 사양: **CPU 8코어 이상 + RAM 16GB 이상 + (권장) NVIDIA GPU VRAM 6GB 이상**. 이 가설은 사용자가 실제 스펙을 확인하기 전까지 잠정 추정이며, Phase 0 스파이크에서 확정 또는 기각된다.

## 3. 이유 (Rationale)

- **faster-whisper large-v3의 GPU 요구량**: 추론 시 약 6GB VRAM 필요. GPU 없이 CPU만으로 실행 시 RTF(Real-Time Factor)가 1.5~3.0 구간으로 추정되어 실시간 처리(RTF ≤ 1.0) 불가능
- **CPU-only 경로의 현실적 한계**: 16-core CPU 환경에서도 large-v3는 평균 RTF 약 1.8 (faster-whisper 벤치마크 기준). DR-03의 STT ≤ 3초 기준을 CPU만으로 달성하려면 medium 이하 모델로 다운그레이드 필요
- **자원 경합 문제**: Whisper 추론(CPU/GPU) + Claude CLI 다중 서브프로세스(네트워크 I/O + 프로세스 오버헤드) + 브라우저 렌더링이 동일 PC에서 동시 실행될 때 메모리 및 CPU 버스트 구간에서 충돌 가능
- **브라우저 렌더링**: 4패널 UI는 integrated graphics로 처리 가능하므로 독립 GPU 의존도 낮음
- **스토리지 필요량**: faster-whisper large-v3 모델 약 3GB + 런타임 환경 합산 5GB 이상 여유 공간 필요

## 4. 검증 방법 (Verification)

### 측정 도구 (OS별 확인 명령, 실행은 사용자가 직접 수행)

**Windows:**
```
systeminfo
wmic path win32_videocontroller get name,AdapterRAM
dxdiag /t dxdiag_output.txt
```

**macOS:**
```
system_profiler SPHardwareDataType
system_profiler SPDisplaysDataType
```

**Linux:**
```
lscpu
free -h
nvidia-smi
df -h
```

### 측정 항목 (필수 확인 6종)

1. CPU 모델, 코어 수, 세대 (예: Intel Core i7-12700, 12코어)
2. RAM 용량 (예: 16GB DDR4)
3. GPU 모델 및 VRAM (예: NVIDIA RTX 3060 12GB) — CUDA 지원 여부 포함
4. 스토리지 여유 공간 (Whisper 모델 ~3GB 저장 가능 여부)
5. 마이크 장치 종류 (내장/외장, USB 여부, 채널 수)
6. 네트워크 종류 (유선 이더넷 / Wi-Fi, 대략적인 속도)

### 통과 기준

| 등급 | 조건 | 결과 |
|------|------|------|
| **필수(Pass)** | CPU 4코어 이상, RAM 8GB 이상, 스토리지 5GB 여유 | DR-03 진행 가능 |
| **권장(Optimal)** | NVIDIA GPU + VRAM 6GB 이상 | large-v3 모델 사용, RTF ≤ 0.5 기대 |
| **회색 영역** | GPU 없음, CPU-only | faster-whisper medium/small + int8 양자화로 전환 필요 (DR-03 가설 수정) |
| **실패(Fail)** | RAM 8GB 미만 | whisper.cpp tiny/base 강제 또는 유료 STT 재검토 |

### 산출물

- `docs/decisions/hardware-profile.md` — 실제 확인된 스펙 1장 (이 DR에 별첨으로 참조). 사용자가 위 명령으로 확인한 결과를 해당 파일에 기록.

## 5. 탈락 시 대안 (Fallback)

- **GPU 없음 (CPU-only)**: faster-whisper medium(~1.5GB RAM) 또는 small 모델 + int8 양자화 적용. STT 지연 허용치를 DR-03에서 3초 → 5초로 완화 재협상 필요
- **RAM < 8GB**: whisper.cpp tiny/base 모델 전환. 한국어 WER 대폭 저하(20%+) 감수, 또는 유료 STT(Clova/Google) 전환 재검토 (CLAUDE.md 비용 제약 예외 처리 필요)
- **스토리지 부족**: 외장 드라이브 또는 네트워크 스토리지에 모델 저장 후 마운트
- **외장 GPU 추가 구매**: 예산 승인 및 프로젝트 범위 초과 여부 사전 판단 필요
- **영향받는 DR**: DR-03(STT 모델 선택 — 스펙에 따라 large/medium/small 분기), DR-05(트리거 쿨다운 — STT 지연 증가 시 쿨다운 연장 공식 재계산)
