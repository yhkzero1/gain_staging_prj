# Gain Staging Calculator

WAV 파일을 분석해 VU 미터 기준(0 VU = −18 dBFS, EBU R68)으로 게인 오프셋을 계산하고,
Studio One `.song` 파일에 직접 적용하는 데스크탑 툴.

익스트림 메탈 믹싱 전처리에서 수십 개 트랙의 InputFX 게인을 일괄 정규화하는 용도로 설계됨.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)

---

## 주요 기능

| 기능 | 설명 |
|------|------|
| VU 미터 시뮬레이션 | 300 ms RMS 블록 + 자동 노이즈 게이트 + 99퍼센타일 레벨 측정 |
| 자동 게이트 (2-pass Otsu) | 트랙별 무음/bleed/히트 경계를 자동 탐색. OH·심벌·탐 bleed 자동 배제 |
| Peak-cap | VU 게인 적용 시 피크가 헤드룸을 초과하면 게인 자동 제한 |
| 스테레오 탐지 | Mono / Dual Mono / Stereo 자동 판별. 스테레오는 큰 채널 기준으로 측정 |
| 트랙 타입 추론 | 오디오 특성(크레스트·스펙트럴 센트로이드·저주파·트랜지언트)으로 자동 분류 |
| Studio One 연동 | `.song` 파일 파싱 → 트랙 자동 매칭 → InputFX 게인 일괄 쓰기 |
| 자동 백업 | `.song` 수정 전 `.song.bak` 자동 생성 |
| CSV 내보내기 | 분석 결과를 `gain_staging_report.csv`로 저장 |
| 컬럼 정렬 | 테이블 헤더 클릭으로 오름/내림차순 정렬 |

---

## 측정 원리

```
1. WAV 로드: 스테레오는 RMS 기준 큰 채널만 사용 (오버헤드 등 한쪽 무음 트랙 오측 방지)
2. 300 ms 블록 단위 RMS 계산
3. 자동 게이트: 2-pass Otsu로 트랙별 무음/bleed/히트 경계 자동 탐색
4. 유효 블록 RMS의 99퍼센타일 → 트랙 레벨
5. 오프셋 = 타깃(−18 dBFS) − 측정 레벨
6. Peak-cap: 오프셋 적용 시 피크 > Peak Headroom 이면 게인 자동 제한
```

99퍼센타일은 VU 미터의 Needle Hold ~2000 ms 동작과 일치하도록 설계됨.
유효 구간이 600 ms 미만이면 "No Signal"로 처리.

---

## 설치

### Windows 단독 실행 파일 (설치 불필요)

`dist/GainStagingCalc.exe` 를 그대로 실행.

### 소스에서 실행

```bash
pip install -r requirements.txt
python gain_calculator.py
```

### 의존성

```
numpy>=1.21
soundfile>=0.12
```

### Windows 실행 파일 빌드

```bash
pip install pyinstaller
pyinstaller GainStagingCalc.spec
# → dist/GainStagingCalc.exe
```

---

## 사용법

### WAV 폴더 모드 (Studio One 없이)

1. **WAV Folder** → 분석할 WAV 파일이 있는 폴더 선택
2. (선택) **Subfolders** 체크 → 하위 폴더까지 재귀 검색
3. **Analyze** 클릭

### Studio One 연동 모드

1. **Studio One을 닫는다** (`.song` 파일이 잠기지 않아야 함)
2. **Load .song** → Studio One 프로젝트 파일 선택
   - 트랙 목록과 WAV 경로가 자동으로 매칭됨
3. **Analyze** 클릭 → 각 트랙의 필요 게인 오프셋 계산
4. 결과 확인 후 **Write to .song** 클릭
   - 적용 전 미리보기 팝업 표시 (Peak-capped 트랙 별도 표기)
   - `.song.bak` 자동 백업 생성
   - `audiomixer.xml`의 `InputFX` gain 값 업데이트

### 결과 테이블 컬럼

| 컬럼 | 설명 |
|------|------|
| S1 Track | Studio One 트랙 레이블 |
| Ch | Mono / Dual Mono / Stereo |
| Type | 추론된 트랙 타입 (Kick, Snare, Bass, Gtr, Vox 등) |
| WAV File | 분석된 파일명 |
| Total (s) | 전체 길이 |
| Valid (s) | 게이트 통과 구간 |
| Gate (dBFS) | 적용된 게이트 임계값 |
| Level (dBFS) | 측정된 트랙 레벨 |
| Peak (dBFS) | 트랙 피크 레벨 |
| VU Offset (dB) | VU 기준 필요 게인 조정량 |
| New Gain (dB) | `.song`에 쓸 절대 게인 값 (Peak-cap 적용 후) |
| Status | OK / Peak-capped / Peak Clip Risk / No Signal |

**Peak-capped** (노랑): VU 게인이 피크 헤드룸을 초과 → 자동으로 제한됨.
**Peak Clip Risk** (주황): Peak Headroom 비활성 상태에서 피크 클리핑 위험.
**No Signal** (회색): 유효 신호 구간 600 ms 미만.

---

## 파라미터

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| Auto Gate | ON | 2-pass Otsu로 게이트 임계값 자동 결정 |
| Gate | −50 dBFS | Auto Gate 비활성 시 수동 임계값 |
| Target | −18 dBFS | 목표 레벨 (0 VU, EBU R68) |
| Needle Hold pct | 99 | 레벨 측정 퍼센타일 |
| Peak Headroom | −1 dBFS | Peak-cap 기준. 0으로 설정 시 비활성 |

---

## 파일 구조

```
gain_staging_prj/
├── gain_calculator.py    # 전체 소스 (분석 엔진 + GUI)
├── requirements.txt
├── GainStagingCalc.spec  # PyInstaller 빌드 설정
├── dist/
│   └── GainStagingCalc.exe  # Windows 실행 파일
└── README.md
```

---

## 주의사항

- **Write to .song 실행 전 Studio One을 반드시 닫을 것.** 열린 상태에서 쓰면 파일이 손상될 수 있음.
- 백업(`.song.bak`)은 같은 디렉터리에 생성됨. 문제 발생 시 `.bak`을 `.song`으로 복사해 복원.
- Studio One 트랙과 WAV 파일은 파일명 stem으로 매칭됨 (번호 prefix 자동 제거).
- **New Gain은 항상 WAV 분석 기준 절대값이다.** 기존 `.song` 게인과 무관하게, 측정 레벨과 타깃만으로 계산된다. Write to .song을 반복 실행해도 값이 누적되지 않음.
- Peak-capped 트랙은 VU 타깃에 도달하지 못하지만, 피크 클리핑을 방지하기 위해 게인이 제한된 것임.
