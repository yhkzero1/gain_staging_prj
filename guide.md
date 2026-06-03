# Gain Staging Calculator — 사용 가이드

버전 1.0.0 | Windows 전용

---

## 시작하기

`GainStagingCalc.exe`를 더블클릭해 실행한다. 설치 불필요.

---

## 두 가지 사용 모드

### 1. WAV 폴더 모드 (Studio One 없이)

WAV 파일만 있을 때 빠르게 레벨을 확인하는 용도.

1. **WAV Folder** 버튼 클릭 → WAV 파일이 있는 폴더 선택
2. 하위 폴더까지 보려면 **Subfolders** 체크
3. **Analyze** 클릭

결과 테이블에서 각 파일의 레벨·오프셋을 확인할 수 있다.  
Studio One 없이는 **Write to .song** 버튼이 활성화되지 않는다.

---

### 2. Studio One 연동 모드

Studio One 프로젝트와 WAV를 연결해 게인을 한 번에 적용하는 핵심 기능.

#### 순서

1. **Studio One을 닫는다.**  
   열린 상태로 .song 파일을 수정하면 파일이 손상될 수 있다.

2. **Load .song** 버튼 클릭 → `.song` 프로젝트 파일 선택  
   트랙 목록이 자동으로 로드되고, 연결된 WAV 경로가 매칭된다.

3. **Analyze** 클릭  
   각 트랙의 VU 레벨과 필요 게인을 계산한다.

4. 결과 확인 → **Write to .song** 클릭  
   - 적용 전 트랙별 게인 미리보기 팝업 표시  
   - Peak-capped 트랙은 `[cap]` 표시로 구분  
   - `.song.bak` 자동 백업 생성 후 적용

---

## 결과 테이블 읽는 법

| 컬럼 | 의미 |
| ------ | ------ |
| S1 Track | Studio One 트랙 이름 |
| Ch | Mono / Dual Mono / Stereo |
| Type | 자동 추론된 트랙 종류 (Kick, Snare, Bass, Gtr, Vox 등) |
| WAV File | 매칭된 파일명 |
| Total (s) | 파일 전체 길이 |
| Valid (s) | 게이트 통과한 유효 구간 길이 |
| Gate (dBFS) | 적용된 게이트 임계값 (Auto Gate ON이면 자동 결정) |
| Level (dBFS) | 측정된 VU 레벨 |
| Peak (dBFS) | 파일 내 최고 피크 레벨 |
| VU Offset (dB) | VU 기준 필요 게인 조정량 |
| New Gain (dB) | .song에 쓸 절대 게인 값 |
| Status | 상태 코드 |

### Status 코드

| Status | 색상 | 의미 |
| ------ | ------ | ------ |
| OK | 녹색 | 정상 |
| Peak-capped | 노랑 | VU 게인이 피크 헤드룸 초과 → 자동 제한됨 |
| Peak Clip Risk | 주황 | Peak Headroom 비활성 상태에서 클리핑 위험 |
| No Signal | 회색 | 유효 신호 구간 600 ms 미만 |

---

## 파라미터 설명

### Auto Gate (기본: ON)

2-pass Otsu 알고리즘으로 트랙별 게이트 임계값을 자동 결정한다.

- **1차**: 전체 블록 분포에서 무음/신호 경계 탐색
- **2차**: 신호 구간 안에서 bleed/hit 경계 재탐색 (gap ≥ 5 dB, 양쪽 ≥ 5블록일 때만 적용)

오버헤드·심벌·탐처럼 bleed 구간과 실제 어택 사이에 명확한 갭이 있는 트랙에서 bleed를 자동으로 배제한다.

Auto Gate를 끄면 **Gate** 필드의 수동 임계값을 사용한다.

### Gate (기본: −50 dBFS)

Auto Gate 비활성 시 사용하는 수동 게이트 임계값.

### Target (기본: −18 dBFS)

목표 레벨. 0 VU = −18 dBFS (EBU R68 기준).  
모든 트랙에 동일하게 적용된다.

### Needle Hold pct (기본: 99)

레벨 측정에 사용할 백분위수. 99는 VU 미터의 Needle Hold ~2000 ms 동작과 일치하도록 설계되었다.  
수치를 낮추면 측정 레벨이 낮아지고 (더 큰 게인 오프셋), 높이면 피크에 가까워진다.

### Peak Headroom (기본: −1 dBFS)

Peak-cap 기준. VU 게인을 적용했을 때 파일 피크가 이 값을 초과하면 게인을 자동으로 제한한다.  
**0으로 설정하면 Peak-cap 기능이 비활성화된다.**

---

## 트랙 타입 자동 추론

파일명이 아닌 오디오 특성으로 트랙 종류를 자동 판별한다.  
표시 목적이며, 게인 계산 결과에는 영향을 주지 않는다.

| Type 표시 | 트랙 종류 | 판별 기준 |
| ------ | ------ | ------ |
| Kick | 킥드럼 | 저주파 집중 + 강한 트랜지언트 |
| Snare | 스네어 | 중-높은 트랜지언트 레이트 또는 높은 고주파 |
| Tom | 탐탐 | 트랜지언트 레이트 매우 낮음 |
| OH | 오버헤드 | 밝지 않은 타악기류 |
| Cym | 심벌 | 스펙트럴 센트로이드 > 2500 Hz |
| Hi-Hat | 하이햇 | 트랜지언트 레이트 > 8/s |
| Bass | 베이스 | 저주파 비율 높음 + 타격성 낮음 |
| Gtr | 기타 | 중간 스펙트럴 센트로이드 |
| Vox | 보컬 | 높은 스펙트럴 센트로이드 |

---

## Studio One 트랙 매칭 방식

WAV 파일과 Studio One 트랙은 **파일명 stem** 기준으로 매칭된다.

- 번호 prefix 자동 제거: `01 - Kick.wav` → `kick`
- 번호 suffix 자동 제거: `Snare Bottom 2` → `snare bottom`
- 대소문자 구분 없음

매칭 실패한 트랙은 테이블에 `— WAV not found —` 로 표시된다.

---

## 주의사항

- **Write to .song 전에 Studio One을 반드시 닫을 것.**
- `.song.bak` 백업은 같은 폴더에 생성된다. 문제 발생 시 `.bak`을 `.song`으로 복사해 복원.
- **New Gain은 누적되지 않는다.** 기존 .song 게인값과 무관하게 WAV 측정값 기준의 절대값이다. Write to .song을 반복 실행해도 같은 값이 쓰인다.
- Peak-capped 트랙은 VU 타깃(−18 dBFS)에 도달하지 못한다. 이는 클리핑 방지를 위한 의도적인 제한이다.
