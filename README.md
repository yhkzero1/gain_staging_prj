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
| VU 미터 시뮬레이션 | 300 ms RMS 블록 + 노이즈 게이트 + 99퍼센타일 레벨 측정 |
| 클리핑 경고 | 게인 적용 후 피크가 0 dBFS를 초과할 경우 경고 표시 |
| Studio One 연동 | `.song` 파일 파싱 → 트랙 자동 매칭 → InputFX 게인 일괄 쓰기 |
| 자동 백업 | `.song` 수정 전 `.song.bak` 자동 생성 |
| CSV 내보내기 | 분석 결과를 `gain_staging_report.csv`로 저장 |
| 컬럼 정렬 | 테이블 헤더 클릭으로 오름/내림차순 정렬 |

---

## 측정 원리

```
1. WAV를 모노로 합산
2. 300 ms 블록 단위 RMS 계산
3. 노이즈 게이트 (기본 −35 dBFS) 로 무음 구간 제거
4. 유효 블록 RMS의 99퍼센타일 → 트랙 레벨
5. 오프셋 = 타깃(−18 dBFS) − 측정 레벨
```

99퍼센타일은 VU 미터의 Needle Hold ~2000 ms 동작과 일치하도록 설계됨.
유효 구간이 600 ms 미만이면 "No Signal"로 처리.

---

## 설치

### 소스에서 실행

```bash
pip install -r requirements.txt
python gain_calculator.py
```

### 의존성

```
numpy>=1.21
soundfile>=0.12
scipy>=1.7
```

### Windows 단독 실행 파일 빌드

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
   - 적용 전 미리보기 팝업 표시
   - `.song.bak` 자동 백업 생성
   - `audiomixer.xml`의 `InputFX` gain 값 업데이트

### 결과 테이블 컬럼

| 컬럼 | 설명 |
|------|------|
| S1 Track | Studio One 트랙 레이블 |
| WAV File | 분석된 파일명 |
| Total (s) | 전체 길이 |
| Valid (s) | 노이즈 게이트 통과 구간 |
| Level (dBFS) | 측정된 트랙 레벨 |
| Offset (dB) | 필요 게인 조정량 |
| New Gain | WAV 분석 기준 InputFX에 설정할 절대 게인 값 |
| Status | OK / Peak Clip Risk / No Signal |

**Peak Clip Risk** (주황): 해당 오프셋 적용 시 피크 클리핑 발생 가능.
**No Signal** (회색): 유효 신호 구간이 너무 짧아 레벨 측정 불가.

---

## 파라미터

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| Gate | −35 dBFS | 노이즈 게이트 임계값 |
| Target | −18 dBFS | 목표 레벨 (0 VU, EBU R68) |
| Needle Hold pct | 99 | 레벨 측정 퍼센타일 |

---

## 파일 구조

```
gain_staging_prj/
├── gain_calculator.py    # 전체 소스 (분석 엔진 + GUI)
├── requirements.txt
├── GainStagingCalc.spec  # PyInstaller 빌드 설정
└── README.md
```

---

## 주의사항

- **Write to .song 실행 전 Studio One을 반드시 닫을 것.** 열린 상태에서 쓰면 파일이 손상될 수 있음.
- 백업(`.song.bak`)은 같은 디렉터리에 생성됨. 문제 발생 시 `.bak`을 `.song`으로 복사해 복원.
- Studio One 트랙과 WAV 파일은 **파일명 stem** 으로 매칭됨 (대소문자 구분).
- **New Gain은 항상 WAV 분석 기준 절대값이다.** `.song` 파일에 이미 설정된 기존 게인 값과 무관하게, 측정된 레벨과 타깃만으로 계산된다. Write to .song을 반복 실행해도 값이 누적되지 않음.
